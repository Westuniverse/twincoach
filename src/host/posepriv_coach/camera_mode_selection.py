"""Deterministic camera-mode candidates for the dual C920e acceptance probe."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Mapping


_MODE_SPEC = re.compile(r"^(?P<width>[1-9][0-9]*)x(?P<height>[1-9][0-9]*)@(?P<fps>[0-9]+(?:\.[0-9]+)?)$")


@dataclass(frozen=True, slots=True)
class CameraModeCandidate:
    """One requested capture mode; fourcc is selected by the caller."""

    width: int
    height: int
    fps: float

    def __post_init__(self) -> None:
        if self.width < 1 or self.height < 1:
            raise ValueError("camera mode dimensions must be positive")
        if not math.isfinite(self.fps) or self.fps <= 0.0:
            raise ValueError("camera mode fps must be finite and positive")

    @property
    def spec(self) -> str:
        fps = f"{self.fps:g}"
        return f"{self.width}x{self.height}@{fps}"

    def to_record(self) -> dict[str, int | float | str]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "spec": self.spec,
        }


@dataclass(frozen=True, slots=True)
class CameraModePlan:
    """A capture order plus the small amount of preflight evidence behind it."""

    modes: tuple[CameraModeCandidate, ...]
    source: str
    reason_code: str
    reason_zh: str
    shared_root_bus: bool | None = None
    camera_count: int | None = None

    def to_record(self) -> dict[str, object]:
        return {
            "source": self.source,
            "reason_code": self.reason_code,
            "reason_zh": self.reason_zh,
            "shared_root_bus": self.shared_root_bus,
            "camera_count": self.camera_count,
            "modes": [mode.to_record() for mode in self.modes],
        }


def parse_camera_mode_spec(value: str) -> CameraModeCandidate:
    """Parse ``WIDTHxHEIGHT@FPS`` without accepting ambiguous shell values."""

    match = _MODE_SPEC.fullmatch(value.strip())
    if match is None:
        raise ValueError(
            f"invalid camera mode {value!r}; expected WIDTHxHEIGHT@FPS"
        )
    return CameraModeCandidate(
        width=int(match.group("width")),
        height=int(match.group("height")),
        fps=float(match.group("fps")),
    )


def default_c920e_mode_plan() -> tuple[CameraModeCandidate, ...]:
    """Prefer the stable 720p30 baseline, then offer 1080p15 as an option.

    1080p30 remains available through an explicit ``--mode`` request only. It
    must not become an implicit default because the KV260 USB2 topology can
    reject the pair at STREAMON before any application-level measurement.
    """

    return (
        CameraModeCandidate(1280, 720, 30.0),
        CameraModeCandidate(1920, 1080, 15.0),
    )


def plan_c920e_modes(
    topology_record: Mapping[str, object] | None = None,
) -> CameraModePlan:
    """Choose a low-risk order from ``lsusb -t`` evidence.

    The decision is conservative only for the known failure mode: two UVC
    cameras sharing one USB root bus.  It does not prove that a mode will work;
    V4L2 negotiation and measured pair FPS remain authoritative.  Missing or
    incomplete topology keeps the historical order for compatibility.
    """

    if topology_record is None:
        return CameraModePlan(
            modes=default_c920e_mode_plan(),
            source="default",
            reason_code="topology_not_provided",
            reason_zh="未提供 USB 拓扑，保留原有模式顺序；最终以 V4L2 实测为准。",
        )

    payload: Mapping[str, object] = topology_record
    nested = payload.get("topology")
    if isinstance(nested, Mapping):
        payload = nested

    cameras = payload.get("cameras")
    camera_count = len(cameras) if isinstance(cameras, list) else None
    shared = payload.get("shared_root_bus")
    if not isinstance(shared, bool) or camera_count is None or camera_count < 2:
        return CameraModePlan(
            modes=default_c920e_mode_plan(),
            source="default",
            reason_code="topology_incomplete",
            reason_zh="USB 拓扑信息不完整，保留原有模式顺序；请以实际 STREAMON 结果判断。",
            shared_root_bus=shared if isinstance(shared, bool) else None,
            camera_count=camera_count,
        )

    if shared:
        # A shared USB2 root bus is the observed cause of the dual 1080p30
        # STREAMON failure. Keep the stable baseline first and do not spend a
        # probe interval on 1080p30 unless the user explicitly requests it.
        return CameraModePlan(
            modes=default_c920e_mode_plan(),
            source="usb_topology",
            reason_code="shared_root_bus",
            reason_zh=(
                "两路摄像头共享 USB 根总线，先试稳定的 720p30，再提供 1080p15；"
                "1080p30 只接受显式模式请求。"
            ),
            shared_root_bus=True,
            camera_count=camera_count,
        )

    return CameraModePlan(
        modes=default_c920e_mode_plan(),
        source="usb_topology",
        reason_code="distinct_root_buses",
        reason_zh=(
            "两路摄像头至少位于不同 USB 根总线，但仍先用 720p30 稳定基线，"
            "再提供 1080p15；1080p30 只接受显式模式请求。"
        ),
        shared_root_bus=False,
        camera_count=camera_count,
    )


def load_camera_mode_plan(
    path: str | Path,
) -> tuple[CameraModeCandidate, str | None]:
    """Load the selected mode and its optional capture format.

    The manifest is intentionally small and contains only capture metadata;
    this helper never reads or exposes camera pixels.  A missing selection is
    treated as a failed probe instead of silently falling back to 720p.
    """

    manifest_path = Path(path)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取摄像头模式清单: {manifest_path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("摄像头模式清单必须是 JSON 对象")
    if raw.get("schema") != "posepriv.dual_camera_mode_plan.v1":
        raise ValueError("摄像头模式清单 schema 不匹配")
    selected = raw.get("selected_mode")
    if not isinstance(selected, dict):
        raise ValueError("摄像头模式搜索没有 selected_mode")

    width = selected.get("width")
    height = selected.get("height")
    fps = selected.get("fps")
    if isinstance(width, bool) or not isinstance(width, int):
        raise ValueError("selected_mode.width 必须是正整数")
    if isinstance(height, bool) or not isinstance(height, int):
        raise ValueError("selected_mode.height 必须是正整数")
    if isinstance(fps, bool) or not isinstance(fps, (int, float)):
        raise ValueError("selected_mode.fps 必须是正数")

    mode = CameraModeCandidate(width=width, height=height, fps=float(fps))
    declared_spec = selected.get("spec")
    if declared_spec is not None and declared_spec != mode.spec:
        raise ValueError(
            "selected_mode.spec 与 width/height/fps 不一致"
        )
    attempts = raw.get("attempts")
    if isinstance(attempts, list):
        selected_passed = any(
            isinstance(attempt, Mapping)
            and attempt.get("passed") is True
            and _attempt_matches_mode(attempt.get("mode"), mode)
            for attempt in attempts
        )
        if not selected_passed:
            raise ValueError(
                "selected_mode 未对应 attempts 中通过的实际采集模式"
            )
    fourcc = raw.get("fourcc")
    if fourcc is not None and (
        not isinstance(fourcc, str) or len(fourcc) != 4
    ):
        raise ValueError("camera mode manifest fourcc must contain exactly four characters")
    return mode, fourcc


def load_selected_camera_mode(path: str | Path) -> CameraModeCandidate:
    """Load and validate only the selected mode from a probe manifest.

    Keep this compatibility wrapper for callers that do not need the format;
    new runtime paths should use :func:`load_camera_mode_plan` so the tested
    FourCC is not silently replaced by an environment or CLI default.
    """

    mode, _ = load_camera_mode_plan(path)
    return mode


def _attempt_matches_mode(
    record: object,
    mode: CameraModeCandidate,
) -> bool:
    if not isinstance(record, Mapping):
        return False
    declared_spec = record.get("spec")
    if declared_spec is not None:
        return declared_spec == mode.spec
    return (
        record.get("width") == mode.width
        and record.get("height") == mode.height
        and record.get("fps") == mode.fps
    )


__all__ = [
    "CameraModePlan",
    "CameraModeCandidate",
    "default_c920e_mode_plan",
    "load_camera_mode_plan",
    "load_selected_camera_mode",
    "plan_c920e_modes",
    "parse_camera_mode_spec",
]
