"""显式、可关闭的GTK RGB565输出支路；不改姿态/同步/评分或默认窗口。"""

from __future__ import annotations

import ctypes
from pathlib import Path
from threading import get_ident
from typing import Any

from .skeleton_display import SkeletonDisplayError


class NativeGtkRgb565:
    """只附着到调用者新建的项目GTK窗口；imshow/waitKey仍由调用者执行。

    attach必须在首次imshow创建窗口后、事件泵前执行。只支持同一GUI
    线程的一个owner；close只卸除本候选，不关闭窗口或全局清理HighGUI。
    窗口缩放/非RGB565目标由原draw回退，实际计数可用counts核对。
    """

    def __init__(self, library_path: str | Path, *, cv2_module: Any = None,
                 numpy_module: Any = None) -> None:
        if cv2_module is None:
            import cv2 as cv2_module
        if numpy_module is None:
            import numpy as numpy_module
        self._numpy = numpy_module
        self.library_path = Path(library_path).resolve(strict=True)
        if not self.library_path.is_file():
            raise SkeletonDisplayError("RGB565 requires an explicit library file")
        self._owner = get_ident()
        self._handle = None
        self._window_name = None
        self._closed = self._failed = False
        try:
            self._library = ctypes.CDLL(str(self.library_path))
            abi = self._library.posepriv_gtk_rgb565_abi
            abi.argtypes, abi.restype = [], ctypes.c_uint32
            build = self._library.posepriv_gtk_rgb565_build_version
            runtime = self._library.posepriv_gtk_rgb565_runtime_version
            for fn in (build, runtime):
                fn.argtypes, fn.restype = [], ctypes.c_char_p
            if abi() != 1:
                raise SkeletonDisplayError("unsupported RGB565 C ABI")
            expected = cv2_module.__version__.encode("ascii")
            if build() != expected or runtime() != expected:
                raise SkeletonDisplayError("RGB565 OpenCV build/runtime version mismatch")
            self._open = self._library.posepriv_gtk_rgb565_open_v1
            self._open.argtypes, self._open.restype = [ctypes.c_char_p], ctypes.c_void_p
            self._submit = self._library.posepriv_gtk_rgb565_submit_v1
            self._submit.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_size_t]
            self._submit.restype = ctypes.c_int
            self._counts = self._library.posepriv_gtk_rgb565_counts_v1
            self._counts.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)]
            self._counts.restype = ctypes.c_int
            self._close = self._library.posepriv_gtk_rgb565_close_v1
            self._close.argtypes, self._close.restype = [ctypes.c_void_p], ctypes.c_int
        except (AttributeError, OSError) as exc:
            raise SkeletonDisplayError("cannot load RGB565 C ABI v1") from exc

    def _check_owner(self) -> None:
        if get_ident() != self._owner:
            raise SkeletonDisplayError("RGB565 GUI operations require the owner thread")

    def attach(self, window_name: str) -> None:
        self._check_owner()
        if self._closed or self._failed or self._handle is not None:
            raise SkeletonDisplayError("RGB565 attach lifecycle is invalid")
        if not isinstance(window_name, str) or not window_name or "\x00" in window_name:
            raise SkeletonDisplayError("RGB565 requires a nonempty project window name")
        self._handle = self._open(window_name.encode("utf-8"))
        if not self._handle:
            self._failed = True
            raise SkeletonDisplayError("RGB565 public GTK attach failed; no silent fallback")
        self._window_name = window_name

    def present(self, window_name: str, canvas: Any) -> None:
        """调用者执行原imshow后调用；只首次attach，不接管waitKey/鼠标。"""
        self._check_owner()
        if self._handle is None:
            self.attach(window_name)
        elif window_name != self._window_name:
            raise SkeletonDisplayError("RGB565 owner cannot switch project windows")
        self.submit(canvas)

    def submit(self, canvas: Any) -> None:
        self._check_owner()
        if self._closed or self._failed or self._handle is None:
            raise SkeletonDisplayError("RGB565 submit lifecycle is invalid; no retry after failure")
        np = self._numpy
        if (not isinstance(canvas, np.ndarray) or canvas.dtype != np.dtype(np.uint8)
                or canvas.ndim != 3 or canvas.shape[2] != 3
                or not 0 < canvas.shape[0] <= 4096 or not 0 < canvas.shape[1] <= 4096
                or canvas.strides[2] != 1 or canvas.strides[1] != 3
                or canvas.strides[0] < canvas.shape[1] * 3):
            raise SkeletonDisplayError("RGB565 canvas requires HxWx3 uint8 with positive BGR row stride")
        # Native submit copies/converts before return. No Python frame retained
        # by the callback; read-only input and positive-stride ROI are permitted.
        result = self._submit(self._handle, canvas.ctypes.data, canvas.shape[1],
                              canvas.shape[0], canvas.strides[0])
        if result:
            self._failed = True
            raise SkeletonDisplayError(f"RGB565 submit failed ({result}); no retry or redraw")

    def counts(self) -> dict[str, int | bool]:
        self._check_owner()
        if self._handle is None or self._closed:
            raise SkeletonDisplayError("RGB565 counters require an attached owner")
        values = (ctypes.c_uint64 * 5)()
        if self._counts(self._handle, values):
            self._failed = True
            raise SkeletonDisplayError("RGB565 counters failed")
        return dict(submits=values[0], candidate_draws=values[1], fallback_draws=values[2],
                    failures=values[3], widget_destroyed=bool(values[4]))

    def close(self) -> None:
        self._check_owner()
        if self._closed:
            return
        if self._handle is not None:
            if self._close(self._handle):
                self._failed = True
                raise SkeletonDisplayError("RGB565 detach failed; owner retained")
            self._handle = None
        self._closed = True

    def stats(self) -> dict[str, Any]:
        """关闭前记录实际绘制/回退；零输入不等于候选已经执行。"""
        self._check_owner()
        if self._closed:
            raise SkeletonDisplayError("RGB565 stats must be collected before detach")
        counters = self.counts() if self._handle is not None else dict(
            submits=0, candidate_draws=0, fallback_draws=0, failures=0, widget_destroyed=False)
        return dict(scope="CPU_GTK_output_NOT_PL_or_physical_scanout",
                    library=str(self.library_path), attached=self._handle is not None,
                    adapter_failed=self._failed, **counters)
