# SPDX-License-Identifier: GPL-2.0-only
"""Explicit fixed-mesh cold upload and matrix-only frames; no automatic fallback."""
from __future__ import annotations

import ctypes
import errno

from .whole_frame_dma import WholeFrameLinuxSharedPoseDma, WHOLE_RX_BYTES, V1_RX_BYTES
from .linux_shared_pose_dma import SHARED_XFER_STRUCT
from .avatar_skinning import (
    PackedDmaAvatarSkinningBackend, PackedSkinningOutputBatch,
    pack_matrix_packet_record, pack_vertex_skinning_request,
)

STATIC_UPLOAD_IOCTL = 0x40205033
STATIC_RUN_IOCTL = 0x40205034


class StaticMeshLinuxSharedPoseDma(WholeFrameLinuxSharedPoseDma):
    def __init__(self, *args, **kwargs):
        self._static_output_bytes = 0
        super().__init__(*args, **kwargs)

    def upload_static_mesh(self, vertices: bytes):
        # Shape only. Kernel snapshot is the single trusted vertex byte boundary.
        if type(vertices) is not bytes or not 16 <= len(vertices) <= WHOLE_RX_BYTES or len(vertices) % 16:
            raise ValueError("static vertices require native bytes, 1..16384 complete records")
        with self._lock:
            if self._fd is None:
                raise RuntimeError("shared DMA transport is closed")
            if self._faulted:
                raise RuntimeError("shared DMA transport faulted; explicit recovery required")
            source = ctypes.cast(ctypes.c_char_p(vertices), ctypes.c_void_p).value
            SHARED_XFER_STRUCT.pack_into(self._whole_command, 0, source, 0, 5, len(vertices), 0, 0)
            try:
                result = self._ioctl(self._fd, STATIC_UPLOAD_IOCTL, self._whole_command)
                if result != 0:
                    raise OSError("static upload returned a nonzero result")
            except OSError as error:
                if error.errno not in (errno.EINVAL, errno.EBUSY, errno.EFAULT, errno.ENOMEM,
                                       errno.ENOTTY, errno.EOPNOTSUPP):
                    self._faulted = True
                raise
            except BaseException:
                self._faulted = True
                raise
            self._static_output_bytes = len(vertices)

    def run_static_matrices(self, header: bytes) -> bytes:
        if type(header) is not bytes or len(header) != 512:
            raise ValueError("static skinning frame requires exactly 512 matrix bytes")
        with self._lock:
            if self._fd is None:
                raise RuntimeError("shared DMA transport is closed")
            if self._faulted:
                raise RuntimeError("shared DMA transport faulted; explicit recovery required")
            if not self._static_output_bytes:
                raise RuntimeError("static mesh has not been uploaded")
            tx, rx = ctypes.addressof(self._whole_tx), ctypes.addressof(self._whole_rx)
            ctypes.memmove(tx, header, 512)
            SHARED_XFER_STRUCT.pack_into(self._whole_command, 0, tx, rx, 5, 512, self._timeout_ms, 0)
            try:
                result = self._ioctl(self._fd, STATIC_RUN_IOCTL, self._whole_command)
            except OSError as error:
                if error.errno == errno.ENODATA:
                    self._static_output_bytes = 0
                elif error.errno not in (errno.EINVAL, errno.ENOMEM, errno.ENOTTY, errno.EOPNOTSUPP):
                    self._faulted = True
                raise
            except BaseException:
                self._faulted = True
                raise
            try:
                if result != 0:
                    raise OSError("static run returned a nonzero result")
                for offset in range(0, self._static_output_bytes, V1_RX_BYTES):
                    size = min(V1_RX_BYTES, self._static_output_bytes - offset)
                    if callable(self._skinning_buffer_check):
                        self._skinning_buffer_check(self._whole_rx, size // 16, offset=offset)
                    else:
                        self._skinning_output_check(ctypes.string_at(rx + offset, size), size // 16)
                return ctypes.string_at(rx, self._static_output_bytes)
            except BaseException:
                self._faulted = True
                raise

    def close(self):
        super().close()
        self._static_output_bytes = 0


class StaticMeshAvatarSkinningBackend(PackedDmaAvatarSkinningBackend):
    """Same matrix/output API; fixed vertex tuple is uploaded exactly once."""

    def __init__(self, dma, requests, *, upper64_check=None):
        if type(requests) is not tuple or not 1 <= len(requests) <= WHOLE_RX_BYTES // 16:
            raise ValueError("static backend needs a fixed tuple of 1..16384 requests")
        super().__init__(dma.run_static_matrices, upper64_check=upper64_check)
        vertices = b"".join(pack_vertex_skinning_request(r).data for r in requests)
        dma.upload_static_mesh(vertices)
        self._fixed_requests = requests
        self._fixed_output_bytes = len(vertices)

    def exchange(self, requests):
        if self._matrix_writes is None:
            raise ValueError("program_matrices must be called before exchange")
        if requests is not self._fixed_requests:
            raise ValueError("static backend mesh changed; reopen and explicitly upload")
        header = b"".join(pack_matrix_packet_record(w) for w in self._matrix_writes)
        self._matrix_writes = None
        output = self.transfer(header)
        if len(output) != self._fixed_output_bytes:
            raise ValueError("DMA output length does not match static avatar")
        return PackedSkinningOutputBatch(output, upper64_check=self.upper64_check)
