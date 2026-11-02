"""显式选择的有序 OpenCV CPU 绘制；不参与 PL、姿态或时间戳处理。"""

from __future__ import annotations

import ctypes
from pathlib import Path
from threading import get_ident
from typing import Any

from .skeleton_display import SkeletonDisplayError


class NativeOrderedTrianglePainter:
    """一次 CDLL 调用完成原有逐面绘制；默认渲染器不加载此库。"""

    # Nx3 integer arrays still pass shape/range/alias checks before uint8.
    accepts_numpy_colors = True

    def __init__(self, library_path: str | Path, *, cv2_module: Any = None,
                 numpy_module: Any = None, reuse_identical_input: bool = False,
                 reuse_local_backdrop: bool = False) -> None:
        if reuse_local_backdrop and not reuse_identical_input:
            raise SkeletonDisplayError("local backdrop requires explicit identical-input reuse")
        if cv2_module is None:
            import cv2 as cv2_module
        if numpy_module is None:
            import numpy as numpy_module
        self._numpy = numpy_module
        self.library_path = Path(library_path).resolve(strict=True)
        if not self.library_path.is_file():
            raise SkeletonDisplayError("triangle painter requires an explicit library file")
        try:
            self._library = ctypes.CDLL(str(self.library_path))
            abi = self._library.posepriv_triangle_painter_abi
            abi.argtypes, abi.restype = [], ctypes.c_uint32
            build = self._library.posepriv_triangle_painter_build_version
            runtime = self._library.posepriv_triangle_painter_runtime_version
            for function in (build, runtime):
                function.argtypes, function.restype = [], ctypes.c_char_p
            if abi() != 1:
                raise SkeletonDisplayError("unsupported triangle painter ABI")
            expected = cv2_module.__version__.encode("ascii")
            if build() != expected or runtime() != expected:
                raise SkeletonDisplayError("triangle painter OpenCV build/runtime version mismatch")
            self._paint = self._library.posepriv_paint_triangles_v1
            self._paint.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_size_t,
                                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
            self._paint.restype = ctypes.c_int
            self._equal = None
            if reuse_identical_input:
                self._equal = self._library.posepriv_canvas_equal_v1
                self._equal.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
                self._equal.restype = ctypes.c_int
        except (OSError, AttributeError) as exc:
            raise SkeletonDisplayError("cannot load triangle painter C ABI v1") from exc
        self._failed = False
        self._reuse_enabled = bool(reuse_identical_input)
        # Only this source-reviewed version has the bounded write-footprint proof.
        # Other valid OpenCV versions keep the exact whole-canvas cache.
        self._local_backdrop_enabled = bool(reuse_local_backdrop)
        self._local_backdrop_supported = cv2_module.__version__ == "4.5.4"
        self._reuse_context: tuple[Any, Any] | None = None
        self._local_backdrop_calls = self._full_backdrop_calls = 0
        self._reuse_owner: int | None = None
        self._reuse_snapshot: tuple[Any, Any, Any, Any] | None = None
        self._reuse_probe: tuple[Any, Any, Any] | None = None
        self._reuse_backdrop_probe: tuple[Any, Any, Any] | None = None
        self._cache_admissions = self._deferred_admissions = 0
        self._backdrop_deferred_admissions = 0
        self._calls = self._native_calls = self._reuse_hits = self._empty_calls = 0

    def stats(self) -> dict[str, Any]:
        """精确输入复用只减少 CPU 绘制，不表示新姿态、PL 或实际窗口 draw。"""
        return {
            "scope": "CPU_triangle_paint_NOT_PL_or_window_draw",
            "reuse_identical_input": self._reuse_enabled,
            "reuse_compare_backend": ("bounded_numpy_or_full_native_memcmp"
                if self._local_backdrop_enabled else "native_memcmp") if self._reuse_enabled else None,
            "reuse_local_backdrop": self._local_backdrop_enabled,
            "local_backdrop_version_supported": self._local_backdrop_supported,
            "local_backdrop_calls": self._local_backdrop_calls,
            "full_backdrop_calls": self._full_backdrop_calls,
            "reuse_cache_policy": "geometry_admission_exact_backdrop_readmission" if self._reuse_enabled else None,
            "calls": self._calls, "native_calls": self._native_calls,
            "reuse_hits": self._reuse_hits, "empty_calls": self._empty_calls,
            "failed": self._failed,
            "cache_admissions": self._cache_admissions,
            "deferred_admissions": self._deferred_admissions,
            "backdrop_deferred_admissions": self._backdrop_deferred_admissions,
            "cache_bytes": (0 if self._reuse_snapshot is None else sum(
                item.nbytes for item in self._reuse_snapshot)) + (0 if self._reuse_probe is None
                else self._reuse_probe[1].nbytes + self._reuse_probe[2].nbytes) + (
                    0 if self._reuse_backdrop_probe is None else sum(
                        item.nbytes for item in self._reuse_backdrop_probe)),
            "cache_limit_bytes": 8 * 1024 * 1024,
        }

    def _local_backdrop_bounds(self, canvas: Any, polygons: Any) -> tuple[int, int, int, int] | None:
        """Comparison/replay only; native paint still receives the original canvas.

        See docs/triangle_reuse_local_backdrop_2026-10-09.md for the versioned
        LINE_AA/FillConvexPoly footprint proof. Clipping and large/extreme inputs
        deliberately keep the old full-canvas path, not an estimated crop.
        """
        height, width, _ = canvas.shape
        if not self._local_backdrop_supported or max(width, height) > 65536:
            return None
        xs, ys = polygons[:, :, 0], polygons[:, :, 1]
        xmin, xmax, ymin, ymax = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
        if xmin < 8 or ymin < 8 or xmax >= width - 8 or ymax >= height - 8:
            return None
        return xmin - 8, ymin - 8, xmax + 9, ymax + 9

    def __call__(self, canvas: Any, polygons: Any, colors: Any) -> None:
        if self._failed:
            raise SkeletonDisplayError("triangle painter failed; no retry or canvas fallback")
        np = self._numpy
        if (not isinstance(canvas, np.ndarray) or canvas.dtype != np.dtype(np.uint8)
                or canvas.ndim != 3 or canvas.shape[2] != 3
                or not canvas.flags.c_contiguous or not canvas.flags.writeable
                or not canvas.flags.aligned):
            raise SkeletonDisplayError("triangle painter canvas must be writable contiguous HxWx3 uint8")
        height, width, _ = canvas.shape
        if not 0 < width <= 2147483647 or not 0 < height <= 2147483647:
            raise SkeletonDisplayError("triangle painter canvas dimensions must fit positive int32")
        if (not isinstance(polygons, np.ndarray) or polygons.dtype != np.dtype(np.int32)
                or polygons.ndim != 3 or polygons.shape[1:] != (3, 2)
                or not polygons.flags.c_contiguous or not polygons.flags.aligned):
            raise SkeletonDisplayError("triangle painter polygons must be native contiguous Nx3x2 int32")
        count = len(polygons)
        if count > 2147483647 // 6:
            raise SkeletonDisplayError("triangle painter polygon count exceeds C ABI range")
        try:
            bgr = np.asarray(colors)
        except (ValueError, TypeError, OverflowError) as exc:
            raise SkeletonDisplayError("triangle painter colors must be Nx3 BGR8 integers") from exc
        if count == 0 and bgr.shape == (0,):
            bgr = bgr.reshape((0, 3))
        if (bgr.shape != (count, 3) or (count and (
                bgr.dtype.kind not in "iu" or bgr.min() < 0 or bgr.max() > 255))):
            raise SkeletonDisplayError("triangle painter colors must be Nx3 BGR8 integers")
        if np.shares_memory(canvas, polygons) or np.shares_memory(canvas, bgr):
            raise SkeletonDisplayError("triangle painter input buffers must not alias the canvas")
        if self._reuse_enabled:
            owner = get_ident()
            if self._reuse_owner is not None and self._reuse_owner != owner:
                raise SkeletonDisplayError("triangle painter reuse requires its single owner thread")
            self._reuse_owner = owner
        self._calls += 1
        if not count:
            self._empty_calls += 1
            self._reuse_snapshot = None
            self._reuse_probe = None
            self._reuse_backdrop_probe = None
            return
        # Validation precedes conversion: out-of-range colors never wrap uint8.
        bgr = np.ascontiguousarray(bgr, dtype=np.uint8)
        before = None
        admit_output = True
        comparison = canvas
        if self._reuse_enabled:
            bounds = self._local_backdrop_bounds(canvas, polygons) if self._local_backdrop_enabled else None
            if bounds is not None:
                x0, y0, x1, y1 = bounds
                comparison = canvas[y0:y1, x0:x1]
                self._local_backdrop_calls += 1
            else:
                self._full_backdrop_calls += 1
            context = (canvas.shape, bounds)
            has_output = self._reuse_snapshot is not None
            previous = self._reuse_snapshot if has_output else self._reuse_backdrop_probe
            equal = 0
            stable = (previous is not None and np.array_equal(polygons, previous[0])
                      and np.array_equal(bgr, previous[1]) and context == self._reuse_context)
            if stable:
                try:
                    # A rectangular view can be strided. Never hand its nbytes
                    # to whole-buffer memcmp or include unrelated live pixels.
                    equal = (int(np.array_equal(comparison, previous[2])) if bounds is not None
                        else self._equal(canvas.ctypes.data, previous[2].ctypes.data, canvas.nbytes))
                except Exception:
                    self._failed = True
                    self._reuse_snapshot = None
                    self._reuse_probe = None
                    self._reuse_backdrop_probe = None
                    raise
                if equal not in (0, 1):
                    self._failed = True
                    self._reuse_snapshot = None
                    self._reuse_probe = None
                    self._reuse_backdrop_probe = None
                    raise SkeletonDisplayError("triangle painter exact comparison failed; no reuse or retry")
            if equal == 1 and has_output:
                # Equality covers the proven read/write footprint, or the whole
                # canvas on the unchanged fallback path (including clipped faces).
                # Inputs/output are owned copies: retaining or mutating a sink's
                # canvas cannot poison a future frame. Dynamic text follows paint.
                try:
                    comparison[...] = previous[3]
                except Exception:
                    self._failed = True  # A copy exception may leave a partial canvas too.
                    self._reuse_snapshot = None
                    self._reuse_probe = None
                    self._reuse_backdrop_probe = None
                    raise
                self._reuse_hits += 1
                return
            if stable:
                # A changing relevant backdrop cannot reuse an old painted output.
                # Retain only the next private input until its backdrop repeats.
                admit_output = equal == 1
                if admit_output:
                    before = previous[:3]  # Already owned, byte-identical input.
            if not stable and self._reuse_probe is not None:
                shape, probe_polygons, probe_bgr = self._reuse_probe
                stable = (context == self._reuse_context and canvas.shape == shape
                          and np.array_equal(polygons, probe_polygons)
                          and np.array_equal(bgr, probe_bgr))
                del probe_polygons, probe_bgr
            self._reuse_snapshot = None
            self._reuse_probe = None
            self._reuse_backdrop_probe = None
            previous = None  # Release the old entry before allocating its replacement.
            self._reuse_context = context
            if 2 * comparison.nbytes + polygons.nbytes + bgr.nbytes <= 8 * 1024 * 1024:
                try:
                    if stable:
                        if before is None:
                            before = (polygons.copy(), bgr.copy(), comparison.copy())
                    else:
                        # Geometry admits a cache only; it never authorizes replay.
                        # Changing meshes retain tiny owned keys, not two canvases.
                        self._reuse_probe = (canvas.shape, polygons.copy(), bgr.copy())
                        self._deferred_admissions += 1
                except Exception:
                    self._failed = True
                    self._reuse_probe = None
                    self._reuse_backdrop_probe = None
                    raise
        # Local arrays stay alive throughout CDLL (not PyDLL): native drawing
        # runs without per-face Python/native transitions. Caller owns canvas.
        self._native_calls += 1
        status = self._paint(canvas.ctypes.data, width, height, canvas.strides[0],
                             polygons.ctypes.data, bgr.ctypes.data, count)
        if status != 0:
            self._failed = True
            self._reuse_snapshot = None
            self._reuse_probe = None
            self._reuse_backdrop_probe = None
            raise SkeletonDisplayError(f"triangle painter failed with status {status}; canvas may be partial")
        if before is not None:
            try:
                if admit_output:
                    self._reuse_snapshot = (*before, comparison.copy())
                    self._cache_admissions += 1
                else:
                    self._reuse_backdrop_probe = before
                    self._backdrop_deferred_admissions += 1
            except Exception:
                self._failed = True  # Paint already touched the caller's canvas.
                self._reuse_probe = None
                self._reuse_backdrop_probe = None
                raise
