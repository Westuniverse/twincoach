# SPDX-License-Identifier: GPL-2.0-only
"""Explicit private whole-frame op5 transport; every original v1 method stays v1."""
from __future__ import annotations

import ctypes
import errno

from .linux_shared_pose_dma import LinuxSharedPoseDma, SHARED_XFER_STRUCT

WHOLE_SKINNING_IOCTL = 0x40205032  # _IOW('P', 0x32, the unchanged 32-byte struct)
WHOLE_RX_BYTES = 262144
WHOLE_MAX_VERTICES = WHOLE_RX_BYTES // 16
HEADER_BYTES = 512
V1_RX_BYTES = 131072


class WholeFrameLinuxSharedPoseDma(LinuxSharedPoseDma):
    """Select this class and its new method explicitly; no driver/info probing.

    allow_v1_fallback only accepts the new command's pre-DMA ENOTTY/ENOMEM.
    A remembered rejection uses the inherited v1 frame method thereafter.
    """

    def __init__(self, *args, allow_v1_fallback=False, **kwargs):
        if type(allow_v1_fallback) is not bool:
            raise TypeError("allow_v1_fallback must be bool")
        self._whole_tx = ctypes.create_string_buffer(HEADER_BYTES + WHOLE_RX_BYTES)
        self._whole_rx = ctypes.create_string_buffer(WHOLE_RX_BYTES)
        self._whole_command = bytearray(SHARED_XFER_STRUCT.size)
        self._allow_v1_fallback = allow_v1_fallback
        self._whole_use_v1 = False
        super().__init__(*args, **kwargs)

    def exchange_skinning_frame_whole(self, payload: bytes) -> bytes:
        if type(payload) is not bytes:
            raise ValueError("whole skinning frame must be native immutable bytes")
        if len(payload) > HEADER_BYTES + WHOLE_RX_BYTES:
            raise ValueError("whole skinning frame exceeds 16384 vertices")
        if self._whole_use_v1:
            return super().exchange_skinning_frame(payload)
        count = self._skinning_packet_check(payload, max_vertices=WHOLE_MAX_VERTICES)
        output_bytes = count * 16
        fallback = False
        with self._lock:
            if self._fd is None:
                raise RuntimeError("shared DMA transport is closed")
            if self._faulted:
                raise RuntimeError("shared DMA transport faulted; explicit device/PL recovery required")
            if self._whole_use_v1:
                fallback = True
            else:
                tx = ctypes.addressof(self._whole_tx)
                rx = ctypes.addressof(self._whole_rx)
                ctypes.memmove(tx, payload, len(payload))
                SHARED_XFER_STRUCT.pack_into(self._whole_command, 0, tx, rx, 5,
                                            len(payload), self._timeout_ms, 0)
                try:
                    result = self._ioctl(self._fd, WHOLE_SKINNING_IOCTL, self._whole_command)
                except OSError as error:
                    if error.errno in (errno.ENOTTY, errno.ENOMEM):
                        if not self._allow_v1_fallback:
                            raise
                        self._whole_use_v1 = True
                        fallback = True
                    else:
                        self._faulted = True
                        raise
                except BaseException:
                    self._faulted = True
                    raise
                if not fallback:
                    try:
                        if result != 0:
                            raise OSError("whole skinning ioctl returned a nonzero result")
                        # Same installed v1 native byte predicate in <=8192
                        # slices: validation slices, not DMA packets.
                        for offset in range(0, output_bytes, V1_RX_BYTES):
                            size = min(V1_RX_BYTES, output_bytes - offset)
                            if callable(self._skinning_buffer_check):
                                self._skinning_buffer_check(self._whole_rx, size // 16, offset=offset)
                            else:
                                self._skinning_output_check(ctypes.string_at(rx + offset, size), size // 16)
                        return ctypes.string_at(rx, output_bytes)
                    except BaseException:
                        self._faulted = True
                        raise
        # The inherited frame method acquires the same non-reentrant lock.
        # Rejection above performed no DMA; release before entering old v1.
        if fallback:
            return super().exchange_skinning_frame(payload)

    def close(self):
        super().close()
        self._whole_tx = self._whole_rx = None
