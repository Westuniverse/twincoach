"""显式CPU绘制输入实验；不改默认renderer或姿态/评分/时间戳。"""
from __future__ import annotations

import ctypes
import math
from pathlib import Path
import struct
from threading import RLock

from .avatar_display import ProjectedTriangle
from .skeleton_display import SkeletonDisplayError


class NativeTriangleInputs:
    """一次C调用准备坐标/面色；输出独立，不缓存网格或画面。"""
    def __init__(self, library_path):
        self._lock = RLock()
        self._calls = self._native_calls = self._bypasses = self._failures = 0
        self._failed = False
        try:
            self.library_path = Path(library_path).resolve(strict=True)
            if not self.library_path.is_file():
                raise OSError("explicit library must be a file")
            self._library = ctypes.CDLL(str(self.library_path))
            abi = self._library.posepriv_triangle_inputs_abi
            abi.argtypes, abi.restype = [], ctypes.c_uint32
            if abi() != 1:
                raise SkeletonDisplayError("unsupported triangle inputs ABI")
            self._run = self._library.posepriv_triangle_inputs_v1
            self._run.argtypes = [ctypes.c_void_p, ctypes.c_size_t] * 4 + [ctypes.c_size_t]
            self._run.restype = ctypes.c_int
        except (OSError, AttributeError) as error:
            raise SkeletonDisplayError("cannot load triangle inputs C ABI v1") from error

    def stats(self):
        with self._lock:
            return dict(scope="CPU_triangle_inputs_NOT_PL_or_window_draw", calls=self._calls,
                native_calls=self._native_calls, bypasses=self._bypasses,
                failures=self._failures, failed=self._failed, output_cache_bytes=0)

    def prepare(self, renderer, triangles, sx, sy, x, y):
        with self._lock:
            if self._failed:
                raise SkeletonDisplayError("triangle inputs failed; no retry or hidden fallback")
            self._calls += 1
            # Standard, immutable face fields only. Other objects retain the
            # original validation/conversion and exception order before any C.
            eligible = (type(triangles) in (tuple, list) and 0 < len(triangles) <= 2000000
                and type(sx) is float and type(sy) is float and math.isfinite(sx) and math.isfinite(sy)
                and type(x) is int and type(y) is int and abs(x) <= 2**53 and abs(y) <= 2**53)
            packed = []
            if eligible:
                exact_type, finite = type, math.isfinite
                integer_limit = 2**53
                for triangle in triangles:
                    if exact_type(triangle) is not ProjectedTriangle:
                        eligible = False
                        break
                    vertices = triangle.vertices
                    if exact_type(vertices) is not tuple or len(vertices) != 3:
                        eligible = False
                        break
                    rgb = triangle.base_color_rgb
                    if exact_type(rgb) is not tuple or len(rgb) != 3:
                        eligible = False
                        break
                    a, b, c = vertices
                    if (exact_type(a) is not tuple or len(a) != 2
                        or exact_type(b) is not tuple or len(b) != 2
                        or exact_type(c) is not tuple or len(c) != 2):
                        eligible = False
                        break
                    ax, ay = a
                    bx, by = b
                    cx, cy = c
                    # The normal projected face has six built-in integers.
                    # Fixed arity avoids six inner generators plus all() while
                    # preserving the generic mixed-float input contract.
                    if (exact_type(ax) is int and exact_type(ay) is int
                        and exact_type(bx) is int and exact_type(by) is int
                        and exact_type(cx) is int and exact_type(cy) is int):
                        coordinates_ok = (-integer_limit <= ax <= integer_limit
                            and -integer_limit <= ay <= integer_limit
                            and -integer_limit <= bx <= integer_limit
                            and -integer_limit <= by <= integer_limit
                            and -integer_limit <= cx <= integer_limit
                            and -integer_limit <= cy <= integer_limit)
                    else:
                        coordinates_ok = all((exact_type(v) is int and abs(v) <= integer_limit)
                            or (exact_type(v) is float and finite(v))
                            for v in (ax, ay, bx, by, cx, cy))
                    if not coordinates_ok:
                        eligible = False
                        break
                    depth = triangle.depth
                    if not (exact_type(depth) is float and finite(depth)):
                        eligible = False
                        break
                    light = triangle.light_intensity
                    if not (exact_type(light) is float and finite(light)):
                        eligible = False
                        break
                    red, green, blue = rgb
                    if not (exact_type(red) is int and 0 <= red <= 255
                        and exact_type(green) is int and 0 <= green <= 255
                        and exact_type(blue) is int and 0 <= blue <= 255):
                        eligible = False
                        break
                    packed.extend((ax, ay, bx, by, cx, cy, depth, light, blue, green, red))
            if not eligible:
                self._bypasses += 1
                pixels = renderer._project_triangle_polygons(triangles, sx, sy, x, y)
                return pixels, renderer._triangle_fill_colors(triangles)
            try:
                data = (ctypes.c_double * len(packed)).from_buffer_copy(
                    struct.pack(f"={len(packed)}d", *packed))
                params = (ctypes.c_double * 4)(sx, sy, x, y)
                np = renderer._numpy
                pixels = np.empty((len(triangles), 3, 2), dtype=np.int32)
                colors = np.empty((len(triangles), 3), dtype=np.int32)
                self._native_calls += 1
                status = self._run(ctypes.addressof(data), len(data), ctypes.addressof(params), 4,
                    pixels.ctypes.data, pixels.size, colors.ctypes.data, colors.size, len(triangles))
                if status != 0:
                    raise SkeletonDisplayError(f"triangle inputs C ABI failed: {status}")
                return pixels, (colors if getattr(renderer._triangle_painter, "accepts_numpy_colors", False)
                    is True else colors.tolist())
            except BaseException:
                self._failures += 1
                self._failed = True
                raise
