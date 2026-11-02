"""可关闭的 CPU 快照实验：仅复用完全相同几何的单槽投影输出。

不缓存输入帧、置信度或三角测量质量；不改变生产 snapshot 的默认路径。
仅支持项目的普通模型、相机和不可变网格，其余输入原样走生产快照。
"""
from __future__ import annotations

from dataclasses import replace
import math
import struct
from threading import RLock

from .avatar_display import AvatarDisplayModel, AvatarDisplayState
from .avatar_pipeline import AvatarFramePlan
from .avatar_view import OrbitCamera
from .mesh_asset import AvatarMeshAsset


class SnapshotGeometryReuse:
    """绑定一个模型的有界候选；snapshot/clear/stats 均串行化。

    缓存只保存一次成功输出、IEEE 几何键和该不可变资产的强引用。
    不跨模型共享，不用于冻结异步请求，也不宣称是 PL 加速。
    """

    def __init__(self, model: AvatarDisplayModel) -> None:
        if not isinstance(model, AvatarDisplayModel):
            raise TypeError("snapshot reuse requires an AvatarDisplayModel")
        self._model = model
        self._lock = RLock()
        self._key = None
        self._mesh = None
        self._method = None
        self._state = None
        self._hits = self._misses = self._bypasses = self._failures = 0

    def _clear_unlocked(self) -> None:
        self._key = self._mesh = self._method = self._state = None

    def clear(self) -> None:
        """释放单槽，保留累积计数；不修改模型或相机。"""
        with self._lock:
            self._clear_unlocked()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "hits": self._hits, "misses": self._misses,
                "bypasses": self._bypasses, "failures": self._failures,
                "entries": int(self._state is not None),
                "geometry_bytes": len(self._key[2]) if self._key is not None else 0,
            }

    def snapshot(self, *, width: int, height: int) -> AvatarDisplayState:
        model = self._model
        with self._lock, model._lock:
            try:
                frame = model._frame
                method = getattr(model._snapshot_unlocked, "__func__", None)
                eligible = (
                    type(model) is AvatarDisplayModel
                    and type(model.camera) is OrbitCamera
                    and type(frame) is AvatarFramePlan
                    and type(model._mesh_asset) is AvatarMeshAsset
                    and type(width) is int and type(height) is int
                    and width > 0 and height > 0 and method is not None
                )
                segments = tuple(segment for segment in frame.segments
                    if segment.valid and segment.confidence >= model.minimum_confidence
                ) if eligible else ()
                key = _geometry_key(model, frame, segments, width, height) if eligible else None
                if key is None:
                    self._bypasses += 1
                    self._clear_unlocked()
                    return model._snapshot_unlocked(width=width, height=height)
                if (self._state is not None and self._mesh is model._mesh_asset
                        and self._method is method and self._key == key):
                    # Fresh metadata, with the same filtering and reduction order
                    # as the production path. Never return the old frame's state.
                    result = _refresh_metadata(self._state, model, frame, segments)
                    self._hits += 1
                    return result
                self._misses += 1
                self._clear_unlocked()
                result = model._snapshot_unlocked(width=width, height=height)
                self._key, self._mesh, self._method, self._state = (
                    key, model._mesh_asset, method, result
                )
                return result
            except BaseException:
                self._failures += 1
                self._clear_unlocked()
                raise


def _geometry_key(model, frame, segments, width, height):
    """IEEE bytes avoid signed-zero/approximate equality and use no hashes.

    Only ordinary Python float vectors are eligible: conversion of arbitrary
    integers, NumPy scalars or user numeric types could merge distinct inputs.
    Unsupported shapes/types fall back to the original validation, not a retry.
    """
    camera = model.camera
    if type(model._skinned_points) is not tuple:
        return None
    vectors = (frame.model_origin,) + tuple(
        point for segment in segments for point in (segment.start, segment.end)
    ) + model._skinned_points
    if any(type(point) is not tuple or len(point) != 3 for point in vectors):
        return None
    names = tuple(segment.name for segment in segments)
    if any(type(name) is not str for name in names):
        return None
    values = (
        camera.yaw_degrees, camera.pitch_degrees, camera.zoom, camera.pan_x, camera.pan_y,
        frame.model_scale,
    ) + tuple(value for point in vectors for value in point)
    if any(type(value) is not float for value in values):
        return None
    return width, height, struct.pack(f"!{len(values)}d", *values), names


def _refresh_metadata(state, model, frame, segments):
    joints = tuple(joint for joint in frame.joints.values()
        if joint.valid and joint.confidence >= model.minimum_confidence)
    gaps = tuple(point.ray_gap for point in frame.points.values() if math.isfinite(point.ray_gap))
    angles = tuple(point.ray_angle_degrees for point in frame.points.values()
        if point.ray_angle_degrees > 0.0)
    return replace(
        state,
        valid_joint_count=len(joints), total_joint_count=len(frame.joints),
        valid_bone_count=len(segments), total_bone_count=len(frame.segments),
        confidence_percent=sum(joint.confidence for joint in joints) / len(joints) * 100.0
            if joints else 0.0,
        skin_vertex_count=model._skin_vertex_count, frame_id=frame.pair.front.frame_id,
        valid_triangulated_point_count=sum(point.valid for point in frame.points.values()),
        total_triangulated_point_count=len(frame.points), finite_ray_gap_count=len(gaps),
        mean_ray_gap_world_units=sum(gaps) / len(gaps) if gaps else None,
        mean_ray_angle_degrees=sum(angles) / len(angles) if angles else None,
    )
