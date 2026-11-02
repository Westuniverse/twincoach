"""Explicit immutable face representation; no model/device ownership or cached poses."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import operator
import struct


_RECORD = struct.Struct('<6qdd3Bx')


def _dtype(np):
    return np.dtype([('pixels', '<i8', (3, 2)), ('depth', '<f8'),
                     ('light', '<f8'), ('rgb', 'u1', (3,)), ('padding', 'u1')])


@dataclass(frozen=True, slots=True)
class ProjectedTriangleBatch(Sequence):
    """Canonical RGB8, finite float64 faces; all array views borrow immutable bytes.

    Reifying one element still uses the public ProjectedTriangle constructor.
    Batch validation replaces repeated per-face validation, not its constraints.
    """
    data: bytes

    def __post_init__(self):
        import numpy as np
        if type(self.data) is not bytes or len(self.data) % _RECORD.size:
            raise ValueError('complete immutable 68-byte projected face records required')
        rows = self.array_view(np)
        if (not np.isfinite(rows['depth']).all() or not np.isfinite(rows['light']).all()
                or (rows['light'] < 0).any() or (rows['light'] > 1).any()
                or (rows['pixels'] < -(1 << 30)).any() or (rows['pixels'] >= (1 << 30)).any()
                or rows['padding'].any()):
            raise ValueError('invalid projected face depth/light/XY/padding')

    def __len__(self):
        return len(self.data) // _RECORD.size

    def array_view(self, np):
        return np.frombuffer(self.data, dtype=_dtype(np))

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(len(self))))
        index = operator.index(index)
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        from .avatar_display import ProjectedTriangle
        values = _RECORD.unpack_from(self.data, index * _RECORD.size)
        return ProjectedTriangle(tuple(zip(values[:6:2], values[1:6:2])),
                                 values[6], values[7], tuple(values[8:11]))


def project_triangle_batch(mesh, camera_vertices, pixels_with_depth, vertex_valid, soft_lights):
    """Select original faces and retain operation order; no alternate pose or lighting."""
    import numpy as np
    from .avatar_display import _triangle_light_intensity
    pixels = np.asarray([(p[0], p[1]) for p in pixels_with_depth], dtype=object).reshape(-1, 2)
    if any(not -(1 << 30) <= v < (1 << 30) for v in pixels.flat):
        raise ValueError('batch projected XY must fit [-2^30,2^30)')
    pixels = pixels.astype(np.int64)
    faces = np.asarray(mesh.triangles, dtype=np.int64).reshape(-1, 3)
    selected = (faces < len(pixels)).all(axis=1)
    indices = np.flatnonzero(selected)
    if vertex_valid is not None:
        selected[indices] &= np.asarray(vertex_valid, dtype=bool)[faces[indices]].all(axis=1)
        indices = np.flatnonzero(selected)
    if mesh.triangle_double_sided:
        points = pixels[faces[indices]]
        ab, ac = points[:, 1] - points[:, 0], points[:, 2] - points[:, 0]
        front = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0] < 0
        selected[indices] &= np.asarray(mesh.triangle_double_sided, dtype=bool)[indices] | front
        indices = np.flatnonzero(selected)
    active = faces[indices]
    camera = np.asarray(camera_vertices, dtype=np.float64).reshape(-1, 3)
    face_depths = camera[active, 2]
    depths = (face_depths[:, 0] + face_depths[:, 1] + face_depths[:, 2]) / 3.
    lights = (np.asarray(soft_lights, dtype=np.float64)[indices] if soft_lights is not None else
              np.fromiter((_triangle_light_intensity(tuple(camera_vertices[int(i)] for i in face))
                           for face in active), dtype=np.float64, count=len(active)))
    rows = np.zeros(len(active), dtype=_dtype(np))
    rows['pixels'] = pixels[active]
    rows['depth'], rows['light'] = depths, lights
    if mesh.triangle_colors_rgb:
        rows['rgb'] = np.asarray(mesh.triangle_colors_rgb, dtype=np.uint8)[indices]
    else:
        rows['rgb'] = (116, 154, 38)
    return ProjectedTriangleBatch(rows.tobytes())
