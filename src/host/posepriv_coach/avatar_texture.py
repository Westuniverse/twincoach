"""Private photographic atlas and CPU orthographic texture painter, ABI v1."""
from __future__ import annotations

import base64
import ctypes
from dataclasses import dataclass
from pathlib import Path
from threading import Lock


@dataclass(frozen=True, slots=True)
class AvatarTexture:
    width: int
    height: int
    pixels_rgb8: bytes

    def __post_init__(self):
        if (type(self.width) is not int or type(self.height) is not int
                or not 1 <= self.width <= 2048 or not 1 <= self.height <= 2048
                or type(self.pixels_rgb8) is not bytes
                or len(self.pixels_rgb8) != self.width * self.height * 3):
            raise ValueError("RGB8 texture dimensions/data must match, maximum 2048 square")

    def to_mapping(self):
        return {"schema_version": 1, "width": self.width, "height": self.height,
                "pixels_rgb8_base64": base64.b64encode(self.pixels_rgb8).decode("ascii")}

    @classmethod
    def from_mapping(cls, data):
        if data.get("schema_version") != 1:
            raise ValueError("unsupported avatar texture schema")
        encoded = data["pixels_rgb8_base64"]
        if type(encoded) is not str or len(encoded) > 2048 * 2048 * 4:
            raise ValueError("texture encoding exceeds bounds")
        return cls(data["width"], data["height"], base64.b64decode(encoded, validate=True))


class NativeAvatarTexturePainter:
    """Synchronous CPU rasterizer; never touches pose, scoring or PL."""
    def __init__(self, library_path):
        import numpy as np
        self._numpy = np
        self._lock = Lock()
        self._texture = self._pixels = None
        self.library_path = Path(library_path).resolve(strict=True)
        self._library = ctypes.CDLL(str(self.library_path))
        self._library.posepriv_avatar_texture_abi.restype = ctypes.c_int
        if self._library.posepriv_avatar_texture_abi() != 1:
            raise ValueError("unsupported avatar texture C ABI")
        self._draw = self._library.posepriv_avatar_texture_v1
        self._draw.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_size_t,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        self._draw.restype = ctypes.c_int

    def __call__(self, canvas, polygons, uvs, shades, texture):
        np = self._numpy
        if (type(texture) is not AvatarTexture or not isinstance(canvas, np.ndarray)
                or canvas.dtype != np.uint8 or canvas.ndim != 3 or canvas.shape[2] != 3
                or not canvas.flags.c_contiguous or not canvas.flags.writeable):
            raise ValueError("writable contiguous BGR8 canvas and immutable texture required")
        if (not isinstance(polygons, np.ndarray) or polygons.dtype != np.int32
                or polygons.ndim != 3 or polygons.shape[1:] != (3, 2) or len(polygons) > 100000):
            raise ValueError("Nx3x2 int32 polygons required")
        uvs, shades = np.asarray(uvs), np.asarray(shades)
        if any(np.shares_memory(canvas, value) for value in (polygons, uvs, shades)):
            raise ValueError("texture painter inputs must not alias its canvas")
        if (uvs.shape != polygons.shape or shades.shape != (len(polygons),)
                or not np.isfinite(uvs).all() or not np.isfinite(shades).all()
                or (uvs < 0).any() or (uvs > 1).any() or (shades < 0).any() or (shades > 1).any()):
            raise ValueError("UVs and shades must be finite and in 0..1")
        polygons = np.ascontiguousarray(polygons)
        uvs, shades = np.ascontiguousarray(uvs, dtype=np.float64), np.ascontiguousarray(shades, dtype=np.float64)
        with self._lock:
            if self._texture is not texture:
                self._pixels = ctypes.create_string_buffer(texture.pixels_rgb8)
                self._texture = texture
            result = self._draw(canvas.ctypes.data, canvas.shape[1], canvas.shape[0], canvas.strides[0],
                polygons.ctypes.data, uvs.ctypes.data, shades.ctypes.data, len(polygons),
                self._pixels, texture.width, texture.height)
            if result != 0:
                raise RuntimeError(f"avatar texture rasterizer failed: {result}")
