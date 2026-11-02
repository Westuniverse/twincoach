"""Atomic copy-based shared pose DMA ioctl, independent of legacy proxy ABI.

User pointers refer to ctypes buffers, NOT DMA addresses. The kernel owns the
aligned coherent DMA memory. No /dev/mem, mmap, or userspace mode-switch ioctl.
"""
from __future__ import annotations

import ctypes
import errno
import os
import struct
import threading
from typing import Callable

from .pl_metrics import AxisBeat
from .pl_shared_pose_accel import SharedPoseOperator, validate_shared_pose_payload
from .pl_motion_summary import MOTION_SUMMARY_MAX_INPUT_BEATS

SHARED_XFER_STRUCT = struct.Struct("<QQIIII")
SHARED_XFER_IOCTL = 0x40205031  # _IOW('P', 0x31, 32), arm64 Linux


def _linux_ioctl(fd: int, command: int, argument: bytearray) -> int:
    import fcntl  # Linux-only import deferred so host contract tests run on Windows.
    return fcntl.ioctl(fd, command, argument, True)


class LinuxSharedPoseDma:
    def __init__(self, device: str = "/dev/posepriv_shared_pose_dma", *, timeout_ms: int = 2000,
                 opener: Callable = os.open, closer: Callable = os.close,
                 ioctl: Callable = _linux_ioctl, raster_request_validator: Callable | None = None,
                 skinning_validator=None):
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= 2000:
            raise ValueError("shared DMA timeout must be integer 1..2000 ms")
        self._lock = threading.Lock()
        self._timeout_ms = timeout_ms
        self._closer = closer
        self._ioctl = ioctl
        if raster_request_validator is not None and not callable(raster_request_validator):
            raise TypeError('raster request validator must be callable')
        self._raster_request_validator=raster_request_validator
        from .shared_skinning_batches import skinning_validation_functions
        self._skinning_packet_check,self._skinning_output_check=skinning_validation_functions(skinning_validator)
        self._skinning_frame_check=getattr(skinning_validator,'validate_frame_into',None)
        self._skinning_buffer_check=getattr(skinning_validator,'validate_output_buffer',None)
        self._frame_tx=self._frame_rx=None
        self._frame_tx_capacity=self._frame_rx_capacity=0
        self._frame_command=bytearray(SHARED_XFER_STRUCT.size)
        self._faulted = False
        self._fd: int | None = opener(device, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))

    def exchange(self, operator: SharedPoseOperator | int,
                 input_beats: tuple[AxisBeat, ...]) -> tuple[AxisBeat, ...]:
        # Snapshot sequence, data and markers before any syscall. Do not rebuild
        # input AxisBeat objects: packed validation checks the same reserved bits.
        beats = tuple(input_beats)
        if not 1 <= len(beats) <= MOTION_SUMMARY_MAX_INPUT_BEATS:
            raise ValueError("shared pose batch must contain 1..8192 beats")
        chunks = []
        for index, beat in enumerate(beats):
            data, last = bytes(beat.data), beat.last
            if len(data) != 16:
                raise ValueError("shared pose input must contain complete 16-byte beats")
            if not isinstance(last, bool) or last != (index == len(beats) - 1):
                raise ValueError("shared pose TLAST must occur only on the final beat")
            chunks.append(data)
        output = self.exchange_bytes(operator, b"".join(chunks))
        return tuple(AxisBeat(output[index:index + 16], index + 16 == len(output))
                     for index in range(0, len(output), 16))

    def exchange_bytes(self, operator: SharedPoseOperator | int, payload: bytes) -> bytes:
        """Transfer one immutable packed batch, without per-beat objects.

        Input length defines packet TLAST. This remains a copy-based ioctl, not
        zero-copy: the kernel owns and synchronizes its coherent DMA buffers.
        Both APIs share the fd, lock, sticky fault state and close lifecycle.
        """
        selected = validate_shared_pose_payload(operator, payload)
        output_bytes = 16 if selected is SharedPoseOperator.MOTION_SUMMARY else len(payload)
        if selected is SharedPoseOperator.TRIANGLE_LIGHTING:
            output_bytes //= 2
        return self._exchange_validated(int(selected), payload, output_bytes)

    def exchange_raster_packet(self, payload: bytes) -> bytes:
        """Explicit PPS3 op4; same fd/lock and RX-first atomic kernel ioctl.

        No device probing or retry. Only documented kernel preflight rejections
        leave this transport usable; interrupted/partial transfers latch fault.
        """
        from .avatar_raster_packet import validate_dma_request, unpack_response
        check=self._raster_request_validator if self._raster_request_validator is not None else validate_dma_request
        wire_op, output_bytes = check(payload)
        def validate(output):
            response = unpack_response(output)
            if response.op != wire_op or response.status:
                raise RuntimeError("PPS3 raster response failed or mismatched request")
        return self._exchange_validated(4, payload, output_bytes, validate,
                                       (errno.EINVAL, errno.ENODATA, errno.EOPNOTSUPP))

    def exchange_skinning_packet(self, payload: bytes) -> bytes:
        """Explicit PPS4 op5, at most 8192 vertices; borrow this fd for frame batches."""
        if type(payload) is not bytes:
            raise ValueError("shared skinning input must be native immutable bytes")
        count = self._skinning_packet_check(payload)
        return self._exchange_validated(5, payload, count*16,
            lambda output: self._skinning_output_check(output, count),
            (errno.EINVAL, errno.EOPNOTSUPP))

    def _exchange_validated(self, selected: int, payload: bytes, output_bytes: int,
                            validator=None, preflight_errnos=()) -> bytes:
        tx = ctypes.create_string_buffer(payload, len(payload))
        rx = ctypes.create_string_buffer(output_bytes)
        command = bytearray(SHARED_XFER_STRUCT.pack(ctypes.addressof(tx), ctypes.addressof(rx),
                                                   int(selected), len(payload), self._timeout_ms, 0))
        with self._lock:
            if self._fd is None:
                raise RuntimeError("shared DMA transport is closed")
            if self._faulted:
                raise RuntimeError("shared DMA transport faulted; explicit device/PL recovery required")
            try:
                result = self._ioctl(self._fd, SHARED_XFER_IOCTL, command)
                if result != 0:
                    raise OSError("shared DMA ioctl returned a nonzero result")
                output = rx.raw
                if validator is not None:
                    validator(output)
            except OSError as error:
                if error.errno not in preflight_errnos:
                    self._faulted = True
                raise
            except BaseException:
                # No retry of interrupted/partial operation or poisoned output.
                self._faulted = True
                raise
        return output

    def exchange_skinning_frame(self,payload:bytes,*,rx_capacity:int=131072)->bytes:
        """Explicit candidate: one validated logical frame, reused owned buffers.

        Whole input preflight precedes every ioctl. Each completed RX batch is
        checked once before one final immutable copy; no partial frame escapes.
        This does not change the existing packet/other-operator APIs or driver.
        """
        from .shared_skinning_batches import MATRIX_HEADER_BYTES,AXIS_BEAT_BYTES,RX_CAPACITY
        if type(payload)is not bytes or len(payload)<MATRIX_HEADER_BYTES+AXIS_BEAT_BYTES or len(payload)%AXIS_BEAT_BYTES:
            raise ValueError('shared skinning frame must be native immutable header and complete vertices')
        if(type(rx_capacity)is not int or not AXIS_BEAT_BYTES<=rx_capacity<=RX_CAPACITY
                or rx_capacity%AXIS_BEAT_BYTES):
            raise ValueError('skinning RX capacity must be16-byte aligned within16..131072')
        output_bytes=len(payload)-MATRIX_HEADER_BYTES
        with self._lock:
            if self._fd is None:raise RuntimeError('shared DMA transport is closed')
            if self._faulted:raise RuntimeError('shared DMA transport faulted; explicit device/PL recovery required')
            tx_bytes=MATRIX_HEADER_BYTES+rx_capacity
            if self._frame_tx_capacity<tx_bytes:
                self._frame_tx=ctypes.create_string_buffer(tx_bytes);self._frame_tx_capacity=tx_bytes
            if self._frame_rx_capacity<output_bytes:
                self._frame_rx=ctypes.create_string_buffer(output_bytes);self._frame_rx_capacity=output_bytes
            if callable(self._skinning_frame_check):
                count=self._skinning_frame_check(payload,self._frame_tx,rx_capacity=rx_capacity)
            else:count=self._skinning_packet_check(payload,max_vertices=None)
            if count*AXIS_BEAT_BYTES!=output_bytes:raise ValueError('skinning frame vertex count mismatch')
            source=ctypes.cast(ctypes.c_char_p(payload),ctypes.c_void_p).value
            tx,rx=ctypes.addressof(self._frame_tx),ctypes.addressof(self._frame_rx)
            ctypes.memmove(tx,source,MATRIX_HEADER_BYTES)
            try:
                for offset in range(0,output_bytes,rx_capacity):
                    size=min(rx_capacity,output_bytes-offset)
                    ctypes.memmove(tx+MATRIX_HEADER_BYTES,source+MATRIX_HEADER_BYTES+offset,size)
                    SHARED_XFER_STRUCT.pack_into(self._frame_command,0,tx,rx+offset,5,
                        MATRIX_HEADER_BYTES+size,self._timeout_ms,0)
                    result=self._ioctl(self._fd,SHARED_XFER_IOCTL,self._frame_command)
                    if result!=0:raise OSError('shared DMA ioctl returned a nonzero result')
                    if callable(self._skinning_buffer_check):
                        self._skinning_buffer_check(self._frame_rx,size//AXIS_BEAT_BYTES,offset=offset)
                    else:self._skinning_output_check(ctypes.string_at(rx+offset,size),size//AXIS_BEAT_BYTES)
                return ctypes.string_at(rx,output_bytes)
            except BaseException:
                self._faulted=True
                raise

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                fd, self._fd = self._fd, None
                self._closer(fd)
            self._frame_tx=self._frame_rx=None
            self._frame_tx_capacity=self._frame_rx_capacity=0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
