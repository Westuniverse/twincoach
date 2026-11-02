"""默认不加载的显示CPU缩放；只缓存系数，不缓存摄像头或姿态帧。"""

from __future__ import annotations

from collections import OrderedDict
import ctypes
from pathlib import Path
from threading import Lock, get_ident
from typing import Any

from .skeleton_display import SkeletonDisplayError


class NativePreviewResize:
    """同步写入调用方ROI；创建、调用和close均限创建线程。"""

    def __init__(self, library_path: str | Path, *, cv2_module: Any = None,
                 numpy_module: Any = None) -> None:
        if cv2_module is None:
            import cv2 as cv2_module
        if numpy_module is None:
            import numpy as numpy_module
        self._numpy = numpy_module
        # The actual preview ring returns immutable private bytes, not an
        # owning ndarray. Read its real CPython storage address without making
        # a string/c_char_p conversion (which would copy/truncate at NUL).
        self._bytes_address = ctypes.pythonapi.PyBytes_AsString
        self._bytes_address.argtypes, self._bytes_address.restype = [ctypes.py_object], ctypes.c_void_p
        if cv2_module.__version__ != "4.5.4":
            raise SkeletonDisplayError("preview resize only validated for OpenCV 4.5.4")
        try:
            self.library_path = Path(library_path).resolve(strict=True)
            if not self.library_path.is_file():
                raise SkeletonDisplayError("preview resize requires an explicit library file")
            self._library = ctypes.CDLL(str(self.library_path))
            abi = self._library.posepriv_preview_resize_abi
            abi.argtypes, abi.restype = [], ctypes.c_uint32
            if abi() != 1:
                raise SkeletonDisplayError("unsupported preview resize ABI")
            for name in ("build", "runtime"):
                function = getattr(self._library, f"posepriv_preview_resize_{name}_version")
                function.argtypes, function.restype = [], ctypes.c_char_p
                if function() != b"4.5.4":
                    raise SkeletonDisplayError("preview resize OpenCV build/runtime version mismatch")
            neon = self._library.posepriv_preview_resize_neon
            neon.argtypes, neon.restype = [], ctypes.c_int
            neon_value = neon()
            if neon_value not in (0, 1):
                raise SkeletonDisplayError("invalid preview resize NEON declaration")
            self._neon = bool(neon_value)
            self._create = self._library.posepriv_preview_resize_create_v1
            self._create.argtypes = [ctypes.c_int] * 4 + [ctypes.POINTER(ctypes.c_void_p)]
            self._create.restype = ctypes.c_int
            self._run = self._library.posepriv_preview_resize_run_v1
            self._run.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                 ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
                                 ctypes.c_size_t]
            self._run.restype = ctypes.c_int
            self._destroy = self._library.posepriv_preview_resize_destroy_v1
            self._destroy.argtypes, self._destroy.restype = [ctypes.c_void_p], None
        except (OSError, AttributeError) as exc:
            raise SkeletonDisplayError("cannot load preview resize C ABI v1") from exc
        self._owner = get_ident()
        self._plans: OrderedDict[tuple[int, ...], int] = OrderedDict()
        self._closed = self._failed = False
        self._calls = self._native_calls = self._created = self._hits = self._evicted = 0

    def _check_owner(self) -> None:
        if get_ident() != self._owner:
            raise SkeletonDisplayError("preview resize requires its creating owner thread")

    def _buffer(self, image: Any, *, writable: bool) -> tuple[int, int, int]:
        np = self._numpy
        if (not isinstance(image, np.ndarray) or image.dtype != np.dtype(np.uint8)
                or image.ndim != 3 or image.shape[2] != 3
                or not all(1 <= extent <= 4096 for extent in image.shape[:2])
                or (writable and not image.flags.writeable)
                or image.strides[2] != 1 or image.strides[1] != 3):
            raise SkeletonDisplayError("preview resize requires interleaved HxWx3 uint8 (writable output)")
        height, width, _ = image.shape
        step, active = int(image.strides[0]), int(width) * 3
        maximum = ctypes.c_size_t(-1).value
        if step < active or step > maximum or step > (maximum - active) // max(1, height - 1):
            raise SkeletonDisplayError("preview resize invalid or overflowing row stride")
        span = (height - 1) * step + active
        address = int(image.ctypes.data)
        if address <= 0 or span - 1 > maximum - address:
            raise SkeletonDisplayError("preview resize overflowing address span")
        # Only a known allocation proves actual storage, not
        # view.nbytes or byte_bounds of a potentially unsafe as_strided view.
        owner = image
        while isinstance(owner.base, np.ndarray):
            owner = owner.base
        if not (owner.flags.c_contiguous or owner.flags.f_contiguous):
            raise SkeletonDisplayError("preview resize requires provable contiguous allocation capacity")
        if owner.flags.owndata:
            allocation, capacity = int(owner.ctypes.data), int(owner.nbytes)
        elif not writable and type(owner.base) is bytes and not owner.flags.writeable:
            # ndarray/base keeps these private bytes alive throughout run.
            # Exact builtin bytes only: no borrowed mmap/mutable memoryview.
            allocation, capacity = int(self._bytes_address(owner.base)), len(owner.base)
        else:
            raise SkeletonDisplayError("preview resize requires provable NumPy-owned or private bytes source capacity")
        if address < allocation or address - allocation + span > capacity:
            raise SkeletonDisplayError("preview resize span exceeds proven allocation capacity")
        return address, step, span

    def __call__(self, source: Any, destination: Any) -> None:
        self._check_owner()
        self._resize(source, destination)

    def _resize(self, source: Any, destination: Any, *, rotation=None, create=None) -> None:
        if self._closed or self._failed:
            raise SkeletonDisplayError("preview resize closed or failed; no retry or canvas fallback")
        src, src_step, src_span = self._buffer(source, writable=False)
        dst, dst_step, dst_span = self._buffer(destination, writable=True)
        if src <= dst < src + src_span or dst <= src < dst + dst_span:
            raise SkeletonDisplayError("preview resize input/output spans must not overlap")
        key = (source.shape[1], source.shape[0], destination.shape[1], destination.shape[0])
        if rotation is not None:
            key += (rotation,)
        self._calls += 1
        handle = self._plans.get(key)
        if handle is None:
            pointer = ctypes.c_void_p()
            try:
                status = (self._create if create is None else create)(*key, ctypes.byref(pointer))
                if status != 0 or not pointer.value:
                    raise SkeletonDisplayError(f"preview resize plan creation failed ({status})")
            except Exception:
                # Defensive cleanup if a malformed library returns an allocated
                # handle with a failure status. No plan is admitted or retried.
                if pointer.value:
                    self._destroy(pointer.value)
                self._failed = True
                raise
            handle = pointer.value
            if len(self._plans) == 2:
                _, old = self._plans.popitem(last=False)
                self._destroy(old)
                self._evicted += 1
            self._plans[key] = handle
            self._created += 1
        else:
            self._plans.move_to_end(key)
            self._hits += 1
        self._native_calls += 1
        try:
            status = self._run(handle, src, src_step, src_span, dst, dst_step, dst_span)
            if status != 0:
                raise SkeletonDisplayError(f"preview resize run failed ({status}); no canvas retry")
        except Exception:
            self._failed = True
            raise

    def stats(self) -> dict[str, Any]:
        self._check_owner()
        return {"scope": "CPU_preview_resize_NOT_DPU_PL_or_window_draw", "neon": self._neon,
                "calls": self._calls, "native_calls": self._native_calls,
                "plans_created": self._created, "plan_hits": self._hits,
                "plans_evicted": self._evicted, "plans_cached": len(self._plans),
                "plan_limit": 2, "cached_frame_bytes": 0,
                "failed": self._failed, "closed": self._closed}

    def close(self) -> None:
        self._check_owner()
        if not self._closed:
            for handle in self._plans.values():
                self._destroy(handle)
            self._plans.clear()
            self._closed = True

    def __enter__(self) -> NativePreviewResize:
        self._check_owner()
        if self._closed or self._failed:
            raise SkeletonDisplayError("preview resize closed or failed")
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


class SerializedRotatedPreviewResize(NativePreviewResize):
    """One view's bounded plans; calls may move between workers.

    A lock spans each native borrow. The creating thread owns stats and close;
    callers join their workers before closing. Separate views use separate locks.
    """

    def __init__(self, library_path: str | Path, **kwargs) -> None:
        super().__init__(library_path, **kwargs)
        self._lock = Lock()
        try:
            self._create_rotated = self._library.posepriv_preview_resize_create_rotated_v1
            self._create_rotated.argtypes = [ctypes.c_int] * 5 + [ctypes.POINTER(ctypes.c_void_p)]
            self._create_rotated.restype = ctypes.c_int
        except AttributeError as exc:
            raise SkeletonDisplayError("preview resize library lacks rotated ABI1 entry") from exc

    def __call__(self, source: Any, destination: Any, *, rotation: int = 0) -> None:
        if type(rotation) is not int or rotation not in (0, 90, 180, 270):
            raise SkeletonDisplayError("preview rotation must be 0, 90, 180 or 270")
        with self._lock:
            self._resize(source, destination, rotation=rotation, create=self._create_rotated)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return super().stats()

    def close(self) -> None:
        with self._lock:
            super().close()
