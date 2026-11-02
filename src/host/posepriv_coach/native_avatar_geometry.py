"""显式CPU几何实验适配器；不改默认模型，不参与DPU/PL/同步/评分。"""
from __future__ import annotations

import ctypes
import math
import struct
from pathlib import Path
from threading import RLock

from .mesh_asset import AvatarMeshAsset
from .avatar_view import PreparedOrbitProjection
from .skeleton_display import SkeletonDisplayError


class NativeAvatarGeometry:
    """一次原生调用执行投影与光照；每次返回独立Python不可变几何。

    一个深度冻结资产的uint32面索引可复用，不缓存姿态、输出或源帧。
    未支持输入在调用C之前走传入的生产projector；原生失败后永不重试。
    """
    def __init__(self, library_path):
        self._lock = RLock()
        self._mesh = self._indices = None
        self._calls = self._native_calls = self._bypasses = self._failures = 0
        self._failed = False
        try:
            self.library_path = Path(library_path).resolve(strict=True)
            if not self.library_path.is_file():
                raise OSError("explicit library must be a file")
            self._library = ctypes.CDLL(str(self.library_path))
            abi = self._library.posepriv_avatar_geometry_abi
            abi.argtypes, abi.restype = [], ctypes.c_uint32
            if abi() != 1:
                raise SkeletonDisplayError("unsupported avatar geometry ABI")
            self._run = self._library.posepriv_avatar_geometry_v1
            self._run.argtypes = [ctypes.c_void_p, ctypes.c_size_t] * 5 + [ctypes.c_size_t] * 2
            self._run.restype = ctypes.c_int
        except (OSError, AttributeError) as error:
            raise SkeletonDisplayError("cannot load avatar geometry C ABI v1") from error

    def clear(self):
        with self._lock:
            self._mesh = self._indices = None

    def stats(self):
        with self._lock:
            return {"scope": "CPU_geometry_NOT_PL_or_window_draw",
                "calls": self._calls, "native_calls": self._native_calls,
                "bypasses": self._bypasses, "failures": self._failures, "failed": self._failed,
                "mesh_entries": int(self._indices is not None),
                "index_cache_bytes": 0 if self._indices is None else ctypes.sizeof(self._indices),
                "output_cache_bytes": 0}

    def project(self, points, *, mesh, center, fit_scale, projection, original_projector):
        with self._lock:
            if self._failed:
                raise SkeletonDisplayError("avatar geometry failed; no retry or hidden fallback")
            self._calls += 1
            values = ()
            if type(center) is tuple and len(center) == 3 and type(projection) is PreparedOrbitProjection:
                values = (*center, fit_scale, projection.cos_yaw, projection.sin_yaw,
                    projection.cos_pitch, projection.sin_pitch, projection.scale,
                    projection.center_x, projection.center_y, projection.pan_x, projection.pan_y)
            eligible = (type(points) is tuple and len(points) <= 1000000
                and type(center) is tuple and len(center) == 3
                and len(values) == 13
                and type(mesh) is AvatarMeshAsset and len(mesh.triangles) <= 2000000
                and all(type(value) is float and math.isfinite(value) for value in values)
                and all(type(point) is tuple and len(point) == 3
                    and all(type(value) is float and math.isfinite(value) for value in point)
                    for point in points))
            if eligible and self._mesh is not mesh:
                eligible = all(type(index) is int and 0 <= index <= 0xffffffff
                    for face in mesh.triangles for index in face)
            if not eligible:
                self._bypasses += 1
                self._mesh = self._indices = None
                camera, pixels = original_projector(points, center=center,
                    fit_scale=fit_scale, projection=projection)
                return camera, pixels, None
            try:
                if self._mesh is not mesh:
                    indices = tuple(index for face in mesh.triangles for index in face)
                    self._indices = (ctypes.c_uint32 * len(indices)).from_buffer_copy(
                        struct.pack(f"={len(indices)}I", *indices))
                    self._mesh = mesh
                flat_points = tuple(value for point in points for value in point)
                point_array = (ctypes.c_double * len(flat_points)).from_buffer_copy(
                    struct.pack(f"={len(flat_points)}d", *flat_points))
                parameters = (ctypes.c_double * 13).from_buffer_copy(struct.pack("=13d", *values))
                geometry = (ctypes.c_double * (len(points) * 5))()
                lights = (ctypes.c_double * len(mesh.triangles))()
                self._native_calls += 1
                status = self._run(ctypes.addressof(point_array), len(point_array),
                    ctypes.addressof(self._indices), len(self._indices),
                    ctypes.addressof(parameters), len(parameters),
                    ctypes.addressof(geometry), len(geometry), ctypes.addressof(lights), len(lights),
                    len(points), len(mesh.triangles))
                if status != 0:
                    raise SkeletonDisplayError(f"avatar geometry C ABI failed: {status}")
                output = struct.unpack(f"={len(geometry)}d", geometry)
                depths = output[2::5]
                camera = tuple(zip(output[0::5], output[1::5], depths))
                pixels = tuple(zip(map(int, output[3::5]), map(int, output[4::5]), depths))
                return camera, pixels, struct.unpack(f"={len(lights)}d", lights)
            except BaseException:
                self._failures += 1
                self._failed = True
                self._mesh = self._indices = None
                raise
