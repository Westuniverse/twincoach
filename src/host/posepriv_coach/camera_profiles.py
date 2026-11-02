"""Versioned, hardware-independent camera deployment profiles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
from typing import Any

from .contracts import CameraView


class CameraTransport(str, Enum):
    MEDIA_CONTROLLER = "media-controller"
    UVC = "uvc"
    RECORDING = "recording"


class SelectorKind(str, Enum):
    MEDIA_DRIVER = "media-driver"
    BY_ID = "by-id"
    BY_PATH = "by-path"
    EXPLICIT = "explicit"


@dataclass(frozen=True, slots=True)
class CameraSelector:
    kind: SelectorKind
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("camera selector value must not be empty")


@dataclass(frozen=True, slots=True)
class CameraMode:
    width: int
    height: int
    fps: float
    pixel_format: str

    def __post_init__(self) -> None:
        if self.width < 1 or self.height < 1:
            raise ValueError("camera mode dimensions must be positive")
        if self.fps <= 0.0:
            raise ValueError("camera mode fps must be positive")
        if not self.pixel_format.strip():
            raise ValueError("camera mode pixel format must not be empty")


@dataclass(frozen=True, slots=True)
class ImageTransform:
    rotation_degrees: int = 0
    mirror_horizontal: bool = False

    def __post_init__(self) -> None:
        if self.rotation_degrees not in (0, 90, 180, 270):
            raise ValueError("rotation_degrees must be 0, 90, 180, or 270")


@dataclass(frozen=True, slots=True)
class CameraProfile:
    camera_id: str
    view: CameraView
    transport: CameraTransport
    selectors: tuple[CameraSelector, ...]
    preferred_modes: tuple[CameraMode, ...]
    transform: ImageTransform
    synchronization_offset_ms: float = 0.0

    def __post_init__(self) -> None:
        if not self.camera_id.strip():
            raise ValueError("camera_id must not be empty")
        if not self.selectors:
            raise ValueError("camera profile requires at least one selector")
        if not self.preferred_modes:
            raise ValueError("camera profile requires at least one preferred mode")


@dataclass(frozen=True, slots=True)
class CameraAcceptancePolicy:
    duration_seconds: float
    minimum_capture_fps_ratio: float
    minimum_pair_fps_ratio: float
    maximum_p95_skew_ms: float

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0.0:
            raise ValueError("acceptance duration must be positive")
        for name, value in (
            ("minimum_capture_fps_ratio", self.minimum_capture_fps_ratio),
            ("minimum_pair_fps_ratio", self.minimum_pair_fps_ratio),
        ):
            if not 0.0 < value <= 1.0:
                raise ValueError(f"{name} must be within (0, 1]")
        if self.maximum_p95_skew_ms < 0.0:
            raise ValueError("maximum_p95_skew_ms must be non-negative")


@dataclass(frozen=True, slots=True)
class CameraDeploymentProfile:
    name: str
    cameras: tuple[CameraProfile, ...]
    acceptance: CameraAcceptancePolicy
    retain_raw_frames: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("deployment profile name must not be empty")
        views = tuple(camera.view for camera in self.cameras)
        if len(set(views)) != len(views):
            raise ValueError("deployment profile contains duplicate camera views")
        if set(views) != {CameraView.FRONT, CameraView.SIDE}:
            raise ValueError("deployment profile requires exactly front and side cameras")

    def camera(self, view: CameraView) -> CameraProfile:
        return next(camera for camera in self.cameras if camera.view is view)


@dataclass(frozen=True, slots=True)
class ResolvedCameraSelection:
    """一个配置条目解析成 OpenCV 可打开的稳定设备和模式。"""

    camera_id: str
    view: CameraView
    device: str
    mode: CameraMode
    transform: ImageTransform
    synchronization_offset_ms: float
    selector_kind: SelectorKind

    def to_record(self) -> dict[str, str | float | int | bool]:
        return {
            "camera_id": self.camera_id,
            "view": self.view.value,
            "device": self.device,
            "selector_kind": self.selector_kind.value,
            "width": self.mode.width,
            "height": self.mode.height,
            "fps": self.mode.fps,
            "pixel_format": self.mode.pixel_format,
            "rotation_degrees": self.transform.rotation_degrees,
            "mirror_horizontal": self.transform.mirror_horizontal,
            "synchronization_offset_ms": self.synchronization_offset_ms,
        }


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def parse_camera_deployment_profile(record: Any) -> CameraDeploymentProfile:
    """Validate a decoded schema-1 deployment record."""

    raw = _require_mapping(record, "root")
    if raw.get("schema_version") != 1:
        raise ValueError("camera deployment profile requires schema_version=1")

    camera_items = raw.get("cameras")
    if not isinstance(camera_items, list):
        raise ValueError("cameras must be a list")
    cameras = []
    for index, item in enumerate(camera_items):
        record = _require_mapping(item, f"cameras[{index}]")
        selectors = tuple(
            CameraSelector(
                kind=SelectorKind(_require_mapping(selector, "selector")["kind"]),
                value=str(_require_mapping(selector, "selector")["value"]),
            )
            for selector in record.get("selectors", [])
        )
        modes = tuple(
            CameraMode(
                width=int(_require_mapping(mode, "mode")["width"]),
                height=int(_require_mapping(mode, "mode")["height"]),
                fps=float(_require_mapping(mode, "mode")["fps"]),
                pixel_format=str(_require_mapping(mode, "mode")["pixel_format"]),
            )
            for mode in record.get("preferred_modes", [])
        )
        transform_record = _require_mapping(record.get("transform", {}), "transform")
        cameras.append(
            CameraProfile(
                camera_id=str(record["camera_id"]),
                view=CameraView(record["view"]),
                transport=CameraTransport(record["transport"]),
                selectors=selectors,
                preferred_modes=modes,
                transform=ImageTransform(
                    rotation_degrees=int(transform_record.get("rotation_degrees", 0)),
                    mirror_horizontal=bool(transform_record.get("mirror_horizontal", False)),
                ),
                synchronization_offset_ms=float(record.get("synchronization_offset_ms", 0.0)),
            )
        )

    acceptance_record = _require_mapping(raw.get("acceptance"), "acceptance")
    privacy_record = _require_mapping(raw.get("privacy", {}), "privacy")
    return CameraDeploymentProfile(
        name=str(raw["name"]),
        cameras=tuple(cameras),
        acceptance=CameraAcceptancePolicy(
            duration_seconds=float(acceptance_record["duration_seconds"]),
            minimum_capture_fps_ratio=float(
                acceptance_record["minimum_capture_fps_ratio"]
            ),
            minimum_pair_fps_ratio=float(acceptance_record["minimum_pair_fps_ratio"]),
            maximum_p95_skew_ms=float(acceptance_record["maximum_p95_skew_ms"]),
        ),
        retain_raw_frames=bool(privacy_record.get("retain_raw_frames", False)),
    )


def load_camera_deployment_profile(path: str | Path) -> CameraDeploymentProfile:
    """Load and validate schema 1 without requiring optional camera libraries."""

    source = Path(path)
    return parse_camera_deployment_profile(
        json.loads(source.read_text(encoding="utf-8"))
    )


def resolve_camera_profile(
    profile: CameraProfile,
    *,
    require_existing: bool = True,
) -> ResolvedCameraSelection:
    """解析一个 UVC/录制条目，并选择第一个可用的稳定设备路径。

    media-controller 需要独立的 V4L2 media-pipeline 适配器；这里显式拒绝，
    避免把 ``/dev/media*`` 节点静默传给 OpenCV。``require_existing=False``
    只用于生成计划或在板卡未连接时检查配置结构。
    """

    if profile.transport is CameraTransport.MEDIA_CONTROLLER:
        raise ValueError(
            f"camera profile {profile.camera_id!r} uses media-controller transport; "
            "run_live_coach currently accepts UVC/recording device paths only"
        )
    mode = profile.preferred_modes[0]
    if len(mode.pixel_format) != 4:
        raise ValueError(
            f"camera profile {profile.camera_id!r} pixel_format must be a fourcc: "
            f"{mode.pixel_format!r}"
        )
    supported_kinds = {
        SelectorKind.BY_ID,
        SelectorKind.BY_PATH,
        SelectorKind.EXPLICIT,
    }
    for selector in profile.selectors:
        if selector.kind not in supported_kinds:
            continue
        candidate = Path(selector.value)
        if require_existing and not candidate.exists():
            continue
        return ResolvedCameraSelection(
            camera_id=profile.camera_id,
            view=profile.view,
            device=selector.value,
            mode=mode,
            transform=profile.transform,
            synchronization_offset_ms=profile.synchronization_offset_ms,
            selector_kind=selector.kind,
        )
    candidates = ", ".join(
        f"{selector.kind.value}:{selector.value}" for selector in profile.selectors
    )
    suffix = "（路径不存在）" if require_existing else ""
    raise ValueError(
        f"camera profile {profile.camera_id!r} 没有可用的设备选择器{suffix}：{candidates}"
    )


def resolve_camera_deployment_profile(
    profile: CameraDeploymentProfile,
    *,
    require_existing: bool = True,
) -> tuple[ResolvedCameraSelection, ResolvedCameraSelection]:
    """按正面、侧面顺序解析部署配置。"""

    front = resolve_camera_profile(
        profile.camera(CameraView.FRONT),
        require_existing=require_existing,
    )
    side = resolve_camera_profile(
        profile.camera(CameraView.SIDE),
        require_existing=require_existing,
    )
    return front, side
