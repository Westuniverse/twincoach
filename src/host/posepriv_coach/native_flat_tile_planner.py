"""Explicit CPU-only flat-material PXT1 planner ABI1; no device I/O or fallback."""
from __future__ import annotations

import ctypes as ct
from pathlib import Path

from .avatar_tile_plan import TilePacketPlan


class NativeFlatTilePlanner:
    def __init__(self, library_path, *, maximum_frame_bytes=64 * 1024 * 1024):
        import numpy as np
        if type(maximum_frame_bytes) is not int or not 1 <= maximum_frame_bytes <= (1 << 63) - 1:
            raise ValueError("maximum frame packet bytes must be a positive int64")
        self._np = np
        self.maximum_frame_bytes = maximum_frame_bytes
        self.library_path = Path(library_path).resolve(strict=True)
        self._library = lib = ct.CDLL(str(self.library_path))
        lib.posepriv_flat_tile_abi.argtypes = []
        lib.posepriv_flat_tile_abi.restype = ct.c_uint32
        if lib.posepriv_flat_tile_abi() != 1:
            raise ValueError("unsupported flat tile planner ABI")
        self._prepare = lib.posepriv_flat_tile_prepare_v1
        self._prepare.argtypes = [ct.c_uint32, ct.c_uint32, ct.POINTER(ct.c_int32),
                                  ct.c_uint32, ct.c_uint32, ct.c_uint64, ct.POINTER(ct.c_void_p)]
        self._prepare.restype = ct.c_int
        self._bytes = lib.posepriv_flat_tile_bytes_v1
        self._tiles = lib.posepriv_flat_tile_count_v1
        self._commands = lib.posepriv_flat_tile_commands_v1
        for function, result in ((self._bytes, ct.c_uint64), (self._tiles, ct.c_uint32),
                                 (self._commands, ct.c_uint64)):
            function.argtypes = [ct.c_void_p]
            function.restype = result
        self._copy = lib.posepriv_flat_tile_copy_v1
        self._copy.argtypes = [ct.c_void_p, ct.POINTER(ct.c_uint8), ct.c_uint64,
                              ct.POINTER(ct.c_uint64), ct.c_uint32]
        self._copy.restype = ct.c_int
        self._free = lib.posepriv_flat_tile_free_v1
        self._free.argtypes = [ct.c_void_p]
        self._free.restype = None

    def __call__(self, state, *, palette, background_rgb) -> TilePacketPlan:
        if state.texture is not None or palette is None:
            raise ValueError("native flat tile planner requires untextured materials")
        ordered = sorted(state.projected_triangles, key=lambda triangle: triangle.depth, reverse=True)
        if len(ordered) > 100000:
            raise ValueError("native flat planner supports at most 100000 triangles")

        def values():
            for triangle in ordered:
                if len(triangle.vertices) != 3:
                    raise ValueError("three XY vertices required")
                for point in triangle.vertices:
                    if len(point) != 2:
                        raise ValueError("XY pairs required")
                    for value in point:
                        if type(value) is not int or not -32768 <= value <= 32767:
                            raise ValueError("flat planner XY must be signed16 integers")
                        yield value
                px, py = palette[triangle.base_color_rgb]
                if (type(px) is not int or type(py) is not int or
                        not 0 <= px <= 255 or not 0 <= py <= 511):
                    raise ValueError("material texel coordinates out of range")
                yield px
                yield py
                yield round(triangle.light_intensity * 256)

        # Private storage remains alive for both C passes; another caller cannot
        # mutate it while ctypes releases the GIL and invalidate counted sizes.
        rows = self._np.fromiter(values(), dtype=self._np.int32, count=len(ordered) * 9).reshape(-1, 9)
        return self._plan_private_rows(rows, state.viewport_width, state.viewport_height, background_rgb)

    def plan_rows(self, rows, width, height, background_rgb) -> TilePacketPlan:
        np = self._np
        if (not isinstance(rows, np.ndarray) or rows.dtype != np.dtype(np.int32) or
                rows.ndim != 2 or rows.shape[1] != 9 or not rows.flags.c_contiguous or
                len(rows) > 100000):
            raise ValueError("contiguous native int32 Nx9 rows required, N<=100000")
        return self._plan_private_rows(rows.copy(), width, height, background_rgb)

    def _plan_private_rows(self, rows, width, height, background_rgb):
        if (type(width) is not int or type(height) is not int or not 1 <= width <= 4096 or
                not 1 <= height <= 4096 or type(background_rgb) is not int or
                not 0 <= background_rgb < 1 << 24):
            raise ValueError("valid viewport and RGB24 background required")
        handle = ct.c_void_p()
        try:
            error = self._prepare(width, height, rows.ctypes.data_as(ct.POINTER(ct.c_int32)),
                                  len(rows), background_rgb, self.maximum_frame_bytes, ct.byref(handle))
            if error:
                if error == 4:
                    raise MemoryError("flat tile planner allocation failed")
                raise ValueError(f"flat tile planner rejected shape/range/capacity (code {error})")
            count, size = self._tiles(handle), self._bytes(handle)
            columns = (width + 31) // 32
            if count != columns * ((height + 31) // 32) or not count * 16 <= size <= self.maximum_frame_bytes:
                raise RuntimeError("flat tile planner returned invalid job metadata")
            output = (ct.c_uint8 * size)()
            offsets = (ct.c_uint64 * (count + 1))()
            if self._copy(handle, output, size, offsets, count + 1):
                raise RuntimeError("flat tile planner failed to copy its complete job")
            blob = bytes(output)
            packets = tuple(((index % columns * 32, index // columns * 32),
                             blob[offsets[index]:offsets[index + 1]]) for index in range(count))
            return TilePacketPlan(packets, int(self._commands(handle)))
        finally:
            if handle.value:
                self._free(handle)
