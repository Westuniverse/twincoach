# SPDX-License-Identifier: MIT
"""Explicit CPU private-pyramid C ABI; never auto-load, retry or fall back."""

from __future__ import annotations

import ctypes
from pathlib import Path
from threading import get_ident
from typing import Any
import weakref


class NativeObjectTrackingError(RuntimeError):
    pass


class NativePyramidLKFlow:
    """One owner thread, one native context; no Python image/point history."""

    def __init__(self, library_path: str | Path, width: int, height: int) -> None:
        if (type(width) is not int or not 1 <= width <= 4096
                or type(height) is not int or not 1 <= height <= 2160):
            raise ValueError("native tracking dimensions must be within 4096x2160")
        import cv2
        import numpy as np
        self._numpy = np
        self._width, self._height = width, height
        self._owner = get_ident()
        self._closed = self._failed = False
        self._handle = None
        self._finalizer = None
        self._bytes_address = ctypes.pythonapi.PyBytes_AsString
        self._bytes_address.argtypes, self._bytes_address.restype = [ctypes.py_object], ctypes.c_void_p
        try:
            self.library_path = Path(library_path).resolve(strict=True)
            if not self.library_path.is_file():
                raise NativeObjectTrackingError("native tracking requires an explicit library file")
            self._library = ctypes.CDLL(str(self.library_path))
            abi = self._library.posepriv_object_tracking_abi
            abi.argtypes, abi.restype = [], ctypes.c_uint32
            if abi() != 1:
                raise NativeObjectTrackingError("unsupported object tracking ABI")
            expected = cv2.__version__.encode("ascii")
            for name in ("build", "runtime"):
                version = getattr(self._library, f"posepriv_object_tracking_{name}_version")
                version.argtypes, version.restype = [], ctypes.c_char_p
                if version() != expected:
                    raise NativeObjectTrackingError("tracking OpenCV build/runtime/Python versions differ")
            pointer = ctypes.c_void_p
            size = ctypes.c_size_t
            specifications = {
                "create": [ctypes.c_int, ctypes.c_int, ctypes.POINTER(pointer)],
                "seed": [pointer, pointer, size, size],
                "forward": [pointer, pointer, size, size, pointer, pointer, pointer, pointer],
                "backward": [pointer, pointer, pointer, pointer, pointer],
                "commit": [pointer], "reset": [pointer],
                "retained_bytes": [pointer], "destroy": [pointer],
            }
            for name, arguments in specifications.items():
                function = getattr(self._library, f"posepriv_object_tracking_{name}_v1")
                function.argtypes = arguments
                function.restype = (None if name == "destroy" else
                                    ctypes.c_uint64 if name == "retained_bytes" else ctypes.c_int)
                setattr(self, f"_{name}", function)
            handle = ctypes.c_void_p()
            try:
                status = self._create(width, height, ctypes.byref(handle))
                if status != 0 or not handle.value:
                    raise NativeObjectTrackingError(f"native tracking creation failed ({status})")
            except BaseException:
                if handle.value:
                    self._destroy(handle.value)
                raise
            try:
                self._handle = handle.value
                self._finalizer = weakref.finalize(self, self._destroy, self._handle)
            except BaseException:
                self._handle = None
                self._destroy(handle.value)
                raise
        except (OSError, AttributeError) as exc:
            raise NativeObjectTrackingError("cannot load object tracking C ABI v1") from exc

    def _check_owner(self) -> None:
        if get_ident() != self._owner:
            raise NativeObjectTrackingError("native tracking requires its creating owner thread")

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    def _usable(self) -> None:
        self._check_owner()
        if self._closed or self._failed:
            raise NativeObjectTrackingError("native tracking closed or failed; reconstruct, no retry/fallback")

    def ensure_usable(self) -> None:
        """Check owner/lifecycle/fault only; no native call, reset or retry."""
        self._usable()

    def _prove_allocation(self, image: Any) -> None:
        """Prove a real allocation BEFORE any copy; nbytes/as_strided is not proof."""
        np = self._numpy
        maximum = ctypes.c_size_t(-1).value
        if type(image) is not np.ndarray or not image.dtype.isnative or not image.flags.aligned:
            raise ValueError("native tracking requires aligned native-endian NumPy storage")
        if any(type(extent) is not int or extent < 1 for extent in image.shape):
            raise ValueError("native tracking invalid array shape")
        # Positive strides only: reject zero/negative/as-strided aliases before
        # np.copy can read bytes outside a provable owner allocation.
        span = image.dtype.itemsize
        for extent, stride in zip(image.shape, image.strides, strict=True):
            if stride <= 0 or stride > maximum or (extent - 1) > (maximum - span) // stride:
                raise ValueError("native tracking invalid or overflowing array strides")
            span += (extent - 1) * stride
        address = int(image.ctypes.data)
        if address <= 0 or span - 1 > maximum - address:
            raise ValueError("native tracking overflowing address span")
        owner = image
        while isinstance(owner.base, np.ndarray):
            owner = owner.base
            if type(owner) is not np.ndarray:
                raise ValueError("native tracking does not accept subclass-owned storage")
        if not (owner.flags.c_contiguous or owner.flags.f_contiguous):
            raise ValueError("native tracking requires provable contiguous owner capacity")
        if owner.flags.owndata:
            allocation, capacity = int(owner.ctypes.data), int(owner.nbytes)
        elif type(owner.base) is bytes and not owner.flags.writeable:
            allocation, capacity = int(self._bytes_address(owner.base)), len(owner.base)
        else:
            raise ValueError("native tracking storage must be NumPy-owned or private readonly bytes")
        if address < allocation or address - allocation + span > capacity:
            raise ValueError("native tracking span exceeds proven allocation capacity")

    def _gray(self, gray: Any):
        np = self._numpy
        if (type(gray) is not np.ndarray or gray.dtype != np.dtype(np.uint8)
                or gray.shape != (self.height, self.width)):
            raise ValueError("native tracking requires exact uint8 HxW gray")
        self._prove_allocation(gray)
        return gray.copy(order="C")

    def _points(self, points: Any):
        np = self._numpy
        if (type(points) is not np.ndarray or points.dtype != np.dtype(np.float32)
                or not points.dtype.isnative or points.shape != (4, 1, 2)):
            raise ValueError("native tracking requires exact native float32 4x1x2 points")
        self._prove_allocation(points)
        private = points.copy(order="C")
        if (not np.all(np.isfinite(private)) or np.any(private[:, 0, 0] < 0)
                or np.any(private[:, 0, 0] >= self.width) or np.any(private[:, 0, 1] < 0)
                or np.any(private[:, 0, 1] >= self.height)):
            raise ValueError("native tracking points must be finite original-image coordinates")
        return private

    def _invoke(self, function, *arguments) -> None:
        try:
            status = function(self._handle, *arguments)
            if status != 0:
                raise NativeObjectTrackingError(f"native tracking execution failed ({status})")
        except BaseException:
            self._failed = True
            # Reset is resource cleanup, never an algorithm retry. Keep fault sticky.
            try:
                self._reset(self._handle)
            except BaseException:
                pass
            raise

    def _clear_after_exception(self) -> None:
        # Cleanup cannot mask the original input/library exception. A reset
        # error still sets the sticky fault through _invoke; close owns teardown.
        try:
            self.clear()
        except BaseException:
            pass

    def _outputs(self, function, *arguments):
        np = self._numpy
        points = np.zeros((4, 1, 2), dtype=np.float32)
        status = np.zeros((4, 1), dtype=np.uint8)
        errors = np.zeros((4, 1), dtype=np.float32)
        self._invoke(function, *arguments, points.ctypes.data, status.ctypes.data, errors.ctypes.data)
        if np.any((status != 0) & (status != 1)):
            self._failed = True
            self._clear_after_exception()
            raise NativeObjectTrackingError("malformed native tracking status")
        # C sanitizes failed outputs. Still do not read ANY point/error values
        # before the entire direction succeeds; tracker performs qualification.
        if np.all(status == 1) and (not np.all(np.isfinite(points))
                or not np.all(np.isfinite(errors)) or np.any(errors < 0)):
            self._failed = True
            self._clear_after_exception()
            raise NativeObjectTrackingError("malformed native tracking successful outputs")
        return points, status, errors

    def seed(self, gray: Any) -> None:
        self._usable()
        try:
            private = self._gray(gray)
            self._invoke(self._seed, private.ctypes.data, private.strides[0], private.nbytes)
        except BaseException:
            self._clear_after_exception()
            raise

    def forward(self, gray: Any, points: Any):
        self._usable()
        try:
            private_gray, private_points = self._gray(gray), self._points(points)
            return self._outputs(self._forward, private_gray.ctypes.data,
                private_gray.strides[0], private_gray.nbytes, private_points.ctypes.data)
        except BaseException:
            self._clear_after_exception()
            raise

    def _gray_from_bgr(self, image: Any):
        import cv2
        from .relative_object import _prove_bgr_storage
        np = self._numpy
        if (type(image) is not np.ndarray or image.dtype != np.dtype(np.uint8)
                or image.shape != (self.height, self.width, 3)):
            raise ValueError("native tracking requires exact uint8 HxWx3 BGR")
        _prove_bgr_storage(image, np)
        # No dst: OpenCV creates the private gray buffer. It stays local and
        # alive until synchronous C returns; C retains only its own pyramid.
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    def seed_bgr(self, image: Any) -> None:
        self._usable()
        try:
            gray = self._gray_from_bgr(image)
            self._invoke(self._seed, gray.ctypes.data, gray.strides[0], gray.nbytes)
        except BaseException:
            self._clear_after_exception()
            raise

    def forward_bgr(self, image: Any, points: Any):
        self._usable()
        try:
            gray, private_points = self._gray_from_bgr(image), self._points(points)
            return self._outputs(self._forward, gray.ctypes.data,
                gray.strides[0], gray.nbytes, private_points.ctypes.data)
        except BaseException:
            self._clear_after_exception()
            raise

    def _mat_frame_function(self, name: str):
        attribute = f"_{name}_mat_frame"
        function = getattr(self, attribute, None)
        if function is None:
            try:
                function = getattr(self._library, f"posepriv_object_tracking_{name}_mat_frame_v1")
            except AttributeError as exc:
                raise NativeObjectTrackingError("library has no explicit Mat frame input v1") from exc
            pointer = ctypes.c_void_p
            function.argtypes = [pointer, pointer, ctypes.c_size_t] + (
                [pointer] * 4 if name == "forward" else [])
            function.restype = ctypes.c_int
            setattr(self, attribute, function)
        return function

    def _mat_frame_payload(self, frame: bytes) -> bytes:
        from .tracking_pyramid_reference import tracking_pyramid_layout
        if self.width > 1280 or self.height > 720:
            raise ValueError("Mat frame input v1 requires dimensions within 1280x720")
        if type(frame) is not bytes or len(frame) != tracking_pyramid_layout(
                self.width, self.height)["output_frame_bytes"]:
            raise ValueError("exact immutable complete Mat frame bytes required")
        return frame

    def seed_mat_frame(self, frame: bytes) -> None:
        """Explicit packed-pyramid input; caller must finish DMA/cache visibility first."""
        self._usable()
        function = self._mat_frame_function("seed")
        try:
            frame = self._mat_frame_payload(frame)
            self._invoke(function, self._bytes_address(frame), len(frame))
        except BaseException:
            self._clear_after_exception()
            raise

    def forward_mat_frame(self, frame: bytes, points: Any):
        self._usable()
        function = self._mat_frame_function("forward")
        try:
            frame, private_points = self._mat_frame_payload(frame), self._points(points)
            return self._outputs(function, self._bytes_address(frame), len(frame),
                                 private_points.ctypes.data)
        except BaseException:
            self._clear_after_exception()
            raise

    def backward(self, points: Any):
        self._usable()
        try:
            private = self._points(points)
            return self._outputs(self._backward, private.ctypes.data)
        except BaseException:
            self._clear_after_exception()
            raise

    def commit(self) -> None:
        self._usable()
        self._invoke(self._commit)

    @property
    def retained_bytes(self) -> int:
        self._check_owner()
        if self._closed or self._failed:
            return 0
        try:
            value = self._retained_bytes(self._handle)
            if type(value) is not int or not 0 <= value < 2**64:
                raise NativeObjectTrackingError("malformed native retained-byte count")
            return value
        except BaseException:
            self._failed = True
            self._clear_after_exception()
            raise

    def clear(self) -> None:
        self._check_owner()
        if not self._closed and self._handle is not None:
            self._invoke(self._reset)  # Does not reset self._failed.

    def close(self) -> None:
        self._check_owner()
        if self._closed:
            return
        self._closed = True
        if self._finalizer is not None:
            self._finalizer.detach()
        handle, self._handle = self._handle, None
        if handle is not None:
            self._destroy(handle)

    def __enter__(self) -> NativePyramidLKFlow:
        self._usable()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
