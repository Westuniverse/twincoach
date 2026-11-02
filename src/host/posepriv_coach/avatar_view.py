"""数字人观察相机和骨骼段的轻量正交投影参考。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

from .skeleton_pose import BoneSegment3D


@dataclass(slots=True)
class OrbitCamera:
    """以世界坐标 +Y 为上方向的可交互观察相机。"""

    yaw_degrees: float = 0.0
    pitch_degrees: float = 0.0
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0

    def __post_init__(self) -> None:
        self._validate()

    def rotate(self, delta_yaw_degrees: float, delta_pitch_degrees: float) -> None:
        if not all(math.isfinite(value) for value in (delta_yaw_degrees, delta_pitch_degrees)):
            raise ValueError("camera rotation deltas must be finite")
        self.yaw_degrees += delta_yaw_degrees
        self.pitch_degrees = max(-89.0, min(89.0, self.pitch_degrees + delta_pitch_degrees))
        self._validate()

    def zoom_by(self, factor: float) -> None:
        if not math.isfinite(factor) or factor <= 0.0:
            raise ValueError("zoom factor must be finite and positive")
        self.zoom = max(0.2, min(5.0, self.zoom * factor))

    def pan(self, delta_x: float, delta_y: float) -> None:
        if not all(math.isfinite(value) for value in (delta_x, delta_y)):
            raise ValueError("camera pan deltas must be finite")
        self.pan_x += delta_x
        self.pan_y += delta_y
        self._validate()

    def reset(self) -> None:
        self.yaw_degrees = 0.0
        self.pitch_degrees = 0.0
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0

    def _validate(self) -> None:
        if not all(
            math.isfinite(value)
            for value in (
                self.yaw_degrees,
                self.pitch_degrees,
                self.zoom,
                self.pan_x,
                self.pan_y,
            )
        ):
            raise ValueError("camera state must be finite")
        if self.zoom <= 0.0 or not -89.0 <= self.pitch_degrees <= 89.0:
            raise ValueError("camera zoom or pitch is outside the supported range")


@dataclass(frozen=True, slots=True)
class ProjectedBoneSegment:
    name: str
    start: tuple[int, int]
    end: tuple[int, int]


@dataclass(frozen=True, slots=True)
class PreparedOrbitProjection:
    """一帧内复用的相机三角函数与视口缩放参数。"""

    cos_yaw: float
    sin_yaw: float
    cos_pitch: float
    sin_pitch: float
    scale: float
    center_x: float
    center_y: float
    pan_x: float
    pan_y: float

    @classmethod
    def from_camera(
        cls,
        camera: OrbitCamera,
        *,
        width: int,
        height: int,
    ) -> "PreparedOrbitProjection":
        if width <= 0 or height <= 0:
            raise ValueError("viewport dimensions must be positive")
        yaw = math.radians(camera.yaw_degrees)
        pitch = math.radians(camera.pitch_degrees)
        return cls(
            cos_yaw=math.cos(yaw),
            sin_yaw=math.sin(yaw),
            cos_pitch=math.cos(pitch),
            sin_pitch=math.sin(pitch),
            scale=min(width, height) * 0.5 * camera.zoom,
            center_x=width * 0.5,
            center_y=height * 0.5,
            pan_x=camera.pan_x,
            pan_y=camera.pan_y,
        )

    def camera_space(
        self, point: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        x, y, z = point
        yaw_x = self.cos_yaw * x - self.sin_yaw * z
        yaw_z = self.sin_yaw * x + self.cos_yaw * z
        return (
            yaw_x,
            self.cos_pitch * y - self.sin_pitch * yaw_z,
            self.sin_pitch * y + self.cos_pitch * yaw_z,
        )

    def project_camera_point(
        self, point: tuple[float, float, float]
    ) -> tuple[int, int, float]:
        x, y, z = point
        return (
            round(self.center_x + x * self.scale + self.pan_x),
            round(self.center_y - y * self.scale + self.pan_y),
            z,
        )

    def project(
        self, point: tuple[float, float, float]
    ) -> tuple[int, int, float]:
        return self.project_camera_point(self.camera_space(point))


def project_point(
    point: tuple[float, float, float],
    camera: OrbitCamera,
    *,
    width: int,
    height: int,
) -> tuple[int, int]:
    """将世界坐标投影到视口像素；采用稳定的正交投影。"""

    screen_x, screen_y, _depth = project_point_with_depth(
        point, camera, width=width, height=height
    )
    return screen_x, screen_y


def project_point_with_depth(
    point: tuple[float, float, float],
    camera: OrbitCamera,
    *,
    width: int,
    height: int,
) -> tuple[int, int, float]:
    """投影一个点并返回完整相机空间深度，供正交 painter 排序。"""

    if width <= 0 or height <= 0:
        raise ValueError("viewport dimensions must be positive")
    if len(point) != 3 or not all(math.isfinite(value) for value in point):
        raise ValueError("world point must be finite")
    return PreparedOrbitProjection.from_camera(
        camera, width=width, height=height
    ).project(point)


def point_in_camera_space(
    point: tuple[float, float, float],
    camera: OrbitCamera,
) -> tuple[float, float, float]:
    """只执行观察相机旋转，供投影、深度排序和表面光照共用。"""

    if len(point) != 3 or not all(math.isfinite(value) for value in point):
        raise ValueError("world point must be finite")
    yaw = math.radians(camera.yaw_degrees)
    pitch = math.radians(camera.pitch_degrees)
    x, y, z = point
    yaw_x = math.cos(yaw) * x - math.sin(yaw) * z
    yaw_z = math.sin(yaw) * x + math.cos(yaw) * z
    return (
        yaw_x,
        math.cos(pitch) * y - math.sin(pitch) * yaw_z,
        math.sin(pitch) * y + math.cos(pitch) * yaw_z,
    )


def project_bone_segments(
    segments: Iterable[BoneSegment3D],
    camera: OrbitCamera,
    *,
    width: int,
    height: int,
) -> tuple[ProjectedBoneSegment, ...]:
    """只投影有效骨段，保持骨段名称便于 UI 关联质量和颜色。"""

    return tuple(
        ProjectedBoneSegment(
            name=segment.name,
            start=project_point(segment.start, camera, width=width, height=height),
            end=project_point(segment.end, camera, width=width, height=height),
        )
        for segment in segments
        if segment.valid
    )
