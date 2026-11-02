"""Standalone whole-frame PixelShuffle DMA v1, not part of pose inference.

Copy-based arm64 Linux ioctl; coherent DMA buffers belong to the kernel's
actual DMA device. ctypes pointers are USER addresses, never DDR addresses.
No mmap, /dev/mem, automatic PL load, reset or retry.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
import struct
import threading
import time
from typing import Callable

from .video_enhancement import pixel_shuffle2x_rgb8_reference

INPUT_WIDTH, INPUT_HEIGHT = 640, 360
INPUT_BYTES = INPUT_WIDTH * INPUT_HEIGHT * 16
OUTPUT_BYTES = INPUT_WIDTH * INPUT_HEIGHT * 12
FRAME_XFER_STRUCT = struct.Struct("<QQIIII")
FRAME_STATUS_STRUCT = struct.Struct("<QQQIIIIIIIIiI")
FRAME_XFER_IOCTL = 0x40205041
FRAME_STATUS_IOCTL = 0x80405042
DMA_STATUS_ERROR_MASK = 0x4770


def _linux_ioctl(fd: int, command: int, argument: bytearray) -> int:
    import fcntl
    return fcntl.ioctl(fd, command, argument, True)


def _dimensions(width: int, height: int) -> None:
    if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in (width, height)):
        raise ValueError("positive integer dimensions required")


def validate_pixel_frame(payload: bytes, *, width: int = INPUT_WIDTH,
                         height: int = INPUT_HEIGHT) -> None:
    _dimensions(width, height)
    if not isinstance(payload, bytes) or len(payload) != width * height * 16:
        raise ValueError("PixelShuffle input must be one complete immutable 16-byte/pixel tensor")
    if any(any(payload[channel::16]) for channel in range(12, 16)):
        raise ValueError("PixelShuffle high four padding bytes must be zero")


def pixel_frame_reference(payload: bytes, *, width: int = INPUT_WIDTH,
                          height: int = INPUT_HEIGHT) -> bytes:
    """Remove only the ABI padding, then reuse the existing 12-channel oracle."""
    validate_pixel_frame(payload, width=width, height=height)
    compact = bytearray(width * height * 12)
    for channel in range(12):
        compact[channel::12] = payload[channel::16]
    return pixel_shuffle2x_rgb8_reference(compact, width=width, height=height)


def pixel_frame_fixture(frame_id: int, *, width: int = INPUT_WIDTH,
                        height: int = INPUT_HEIGHT) -> bytes:
    """Deterministic varying x/y/subpixel/channel/frame pattern, padding=0."""
    _dimensions(width, height)
    if isinstance(frame_id, bool) or not isinstance(frame_id, int) or not 0 <= frame_id <= 0xffffffff:
        raise ValueError("frame_id must be an unsigned 32-bit integer")
    payload = bytearray(width * height * 16)
    for y in range(height):
        for x in range(width):
            base = (y * width + x) * 16
            for channel in range(12):
                payload[base + channel] = (x * 17 + y * 29 + channel * 43 +
                                           frame_id * 71 + (x >> 8) * 19 + (y >> 8) * 23) & 255
    return bytes(payload)


@dataclass(frozen=True)
class PixelFrameStatus:
    tx_dma: int
    rx_dma: int
    elapsed_ns: int
    input_bytes: int
    output_bytes: int
    mm2s_status: int
    s2mm_status: int
    protocol_error: int
    faulted: int
    started: int
    completed: int
    last_errno: int
    reserved: int

    def validate_completed(self) -> None:
        regions = ((self.tx_dma, INPUT_BYTES), (self.rx_dma, OUTPUT_BYTES))
        if any(address < 0 or address & 15 or address > 0x80000000 - size for address, size in regions):
            raise OSError("frame DMA address outside aligned HP0 DDR_LOW region")
        if not (self.tx_dma + INPUT_BYTES <= self.rx_dma or self.rx_dma + OUTPUT_BYTES <= self.tx_dma):
            raise OSError("frame DMA regions overlap")
        if (self.input_bytes != INPUT_BYTES or self.output_bytes != OUTPUT_BYTES or
                self.started != 3 or self.completed != 3 or self.elapsed_ns <= 0 or
                self.protocol_error or self.faulted or self.last_errno or self.reserved or
                (self.mm2s_status | self.s2mm_status) & DMA_STATUS_ERROR_MASK):
            raise OSError("frame DMA did not complete cleanly; inspect cached status")


@dataclass(frozen=True)
class PixelFrameResult:
    output: bytes
    status: PixelFrameStatus
    exchange_ioctl_ns: int  # copy-in + DMA wait + copy-out; excludes oracle/GET_STATUS


class LinuxPixelFrameDma:
    def __init__(self, device: str = "/dev/posepriv_pixel_shuffle_frame_dma", *, timeout_ms: int = 2000,
                 opener: Callable = os.open, closer: Callable = os.close, ioctl: Callable = _linux_ioctl):
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= 2000:
            raise ValueError("frame DMA timeout must be integer 1..2000 ms")
        self._lock = threading.Lock()
        self._timeout_ms, self._closer, self._ioctl = timeout_ms, closer, ioctl
        self._faulted = False
        self._fd: int | None = opener(device, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))

    def _status_locked(self) -> PixelFrameStatus:
        raw = bytearray(FRAME_STATUS_STRUCT.size)
        if self._ioctl(self._fd, FRAME_STATUS_IOCTL, raw) != 0:
            raise OSError("frame DMA GET_STATUS returned a nonzero result")
        return PixelFrameStatus(*FRAME_STATUS_STRUCT.unpack(raw))

    def status(self) -> PixelFrameStatus:
        """Read cached diagnostics even after a failed exchange; does not retry."""
        with self._lock:
            if self._fd is None:
                raise RuntimeError("frame DMA transport is closed")
            return self._status_locked()

    def exchange(self, payload: bytes) -> PixelFrameResult:
        validate_pixel_frame(payload)
        tx = ctypes.create_string_buffer(payload, INPUT_BYTES)
        rx = ctypes.create_string_buffer(OUTPUT_BYTES)
        header = bytearray(FRAME_XFER_STRUCT.pack(ctypes.addressof(tx), ctypes.addressof(rx),
                                                 INPUT_BYTES, OUTPUT_BYTES, self._timeout_ms, 0))
        with self._lock:
            if self._fd is None:
                raise RuntimeError("frame DMA transport is closed")
            if self._faulted:
                raise RuntimeError("frame DMA transport faulted; explicit device/PL recovery required")
            try:
                begin = time.monotonic_ns()
                if self._ioctl(self._fd, FRAME_XFER_IOCTL, header) != 0:
                    raise OSError("frame DMA XFER returned a nonzero result")
                elapsed = time.monotonic_ns() - begin
                status = self._status_locked()
                status.validate_completed()
                return PixelFrameResult(rx.raw, status, elapsed)
            except BaseException:
                self._faulted = True
                raise  # no automatic retry, no publication of partial/poisoned output

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                fd, self._fd = self._fd, None
                self._closer(fd)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
