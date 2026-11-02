"""RGB565 Linux framebuffer sink for the KV260 HDMI path."""

from __future__ import annotations

import mmap
from pathlib import Path
from typing import Any

from .linux_devices import FramebufferDevice


class LinuxFramebufferError(RuntimeError):
    pass


class LinuxFramebufferSink:
    """Scale a BGR888 canvas and copy it into one visible RGB565 framebuffer page."""

    def __init__(
        self,
        device: FramebufferDevice,
        *,
        restore_on_close: bool = False,
        numpy_module: Any | None = None,
        cv2_module: Any | None = None,
    ) -> None:
        if not device.rgb565_compatible:
            raise LinuxFramebufferError("当前仅支持 stride 合法的 16 bit RGB565 framebuffer")
        if device.width <= 0 or device.height <= 0:
            raise LinuxFramebufferError("framebuffer 分辨率无效")
        if numpy_module is None:
            try:
                import numpy as numpy_module  # type: ignore[import-not-found, no-redef]
            except ImportError as exc:
                raise LinuxFramebufferError("framebuffer 输出需要 NumPy") from exc
        self.device = device
        self._numpy = numpy_module
        if cv2_module is None:
            try:
                import cv2 as cv2_module  # type: ignore[import-not-found, no-redef]
            except ImportError:
                cv2_module = None
        self._cv2 = cv2_module
        self._resized_bgr = None
        self._packed_bgr565 = (
            self._numpy.empty(
                (device.height, device.width, 2),
                dtype=self._numpy.uint8,
            )
            if self._cv2 is not None
            else None
        )
        self._file = Path(device.path).open("r+b", buffering=0)
        self._mapped_size = device.stride_bytes * device.height
        try:
            self._memory = mmap.mmap(
                self._file.fileno(),
                self._mapped_size,
                access=mmap.ACCESS_WRITE,
            )
        except Exception:
            self._file.close()
            raise
        self._snapshot = self._memory[:] if restore_on_close else None
        self._closed = False

    def write_bgr(self, canvas: Any) -> None:
        if self._closed:
            raise LinuxFramebufferError("framebuffer 已关闭")
        shape = tuple(getattr(canvas, "shape", ()))
        if len(shape) != 3 or shape[0] <= 0 or shape[1] <= 0 or shape[2] != 3:
            raise ValueError("画布必须是非空 HxWx3 BGR 图像")
        target_shape = (self.device.height, self.device.width, 3)
        if shape != target_shape:
            if self._cv2 is None:
                raise LinuxFramebufferError(
                    "画布尺寸与 framebuffer 不同时需要 OpenCV 完成缩放"
                )
            if self._resized_bgr is None:
                self._resized_bgr = self._numpy.empty(
                    target_shape,
                    dtype=self._numpy.uint8,
                )
            canvas = self._cv2.resize(
                canvas,
                (self.device.width, self.device.height),
                dst=self._resized_bgr,
                interpolation=self._cv2.INTER_LINEAR,
            )
        if self._cv2 is not None:
            # Reuse the fixed-size OpenCV output and expose its buffer directly.
            # This avoids both a full-frame array allocation and a Python bytes
            # copy before the framebuffer write.
            packed = memoryview(
                self._cv2.cvtColor(
                    canvas,
                    self._cv2.COLOR_BGR2BGR565,
                    dst=self._packed_bgr565,
                )
            ).cast("B")
        else:
            np = self._numpy
            blue = canvas[:, :, 0].astype(np.uint16)
            green = canvas[:, :, 1].astype(np.uint16)
            red = canvas[:, :, 2].astype(np.uint16)
            rgb565 = ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)
            packed = rgb565.astype("<u2", copy=False).tobytes()
        row_bytes = self.device.width * 2
        if self.device.stride_bytes == row_bytes:
            self._memory[: len(packed)] = packed
        else:
            for row in range(self.device.height):
                source_start = row * row_bytes
                target_start = row * self.device.stride_bytes
                self._memory[target_start : target_start + row_bytes] = packed[
                    source_start : source_start + row_bytes
                ]

    def close(self) -> None:
        if self._closed:
            return
        if self._snapshot is not None:
            self._memory[:] = self._snapshot
        try:
            self._memory.flush()
        except OSError:
            # Some framebuffer drivers expose mmap but not msync/flush.
            pass
        self._memory.close()
        self._file.close()
        self._closed = True

    def __enter__(self) -> "LinuxFramebufferSink":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
