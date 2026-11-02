"""Parse the board-side native VART dual-camera JSONL boundary."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Iterable, Mapping, Any

from .contracts import CameraView, Keypoint, PoseFrame, SyncedPosePair
from .pose_adapter import MOVENET_COCO_KEYPOINTS


class NativeVartStreamError(ValueError):
    """Raised when a native VART JSONL record violates the pose contract."""


@dataclass(frozen=True, slots=True)
class NativeVartViewRecord:
    timestamp_ns: int
    width: int
    height: int
    points: tuple[tuple[float, float, float], ...]

    def to_pose_frame(
        self,
        view: CameraView,
        *,
        frame_id: int,
    ) -> PoseFrame:
        keypoints = {
            name: Keypoint(x=point[1], y=point[0], confidence=point[2])
            for name, point in zip(MOVENET_COCO_KEYPOINTS, self.points, strict=True)
        }
        return PoseFrame(
            view=view,
            frame_id=frame_id,
            timestamp_ns=self.timestamp_ns,
            keypoints=keypoints,
            source_width=self.width,
            source_height=self.height,
        )


@dataclass(frozen=True, slots=True)
class NativeVartPairRecord:
    frame_id: int
    front: NativeVartViewRecord
    side: NativeVartViewRecord
    pair_us: int | None = None

    def to_synced_pose_pair(self) -> SyncedPosePair:
        front = self.front.to_pose_frame(CameraView.FRONT, frame_id=self.frame_id)
        side = self.side.to_pose_frame(CameraView.SIDE, frame_id=self.frame_id)
        return SyncedPosePair(
            front=front,
            side=side,
            skew_ns=abs(front.timestamp_ns - side.timestamp_ns),
        )


@dataclass(frozen=True, slots=True)
class NativeVartStats:
    """Pair-level statistics emitted by the native dual-camera probe."""

    frames: int
    pair_avg_ms: float
    pair_fps: float
    dpu_avg_ms: float
    post_avg_ms: float
    threshold: float
    front_preprocess_avg_ms: float | None = None
    side_preprocess_avg_ms: float | None = None
    front_fourcc: str | None = None
    side_fourcc: str | None = None
    front_width: int | None = None
    front_height: int | None = None
    side_width: int | None = None
    side_height: int | None = None
    front_camera_fps: float | None = None
    side_camera_fps: float | None = None
    front_capture_fps: float | None = None
    side_capture_fps: float | None = None
    front_capture_read_mean_ms: float | None = None
    side_capture_read_mean_ms: float | None = None
    front_capture_read_p95_ms: float | None = None
    side_capture_read_p95_ms: float | None = None
    front_capture_read_max_ms: float | None = None
    side_capture_read_max_ms: float | None = None
    capture_timestamp_semantics: str | None = None
    parallel_dpu: bool | None = None
    e2e_pair_avg_ms: float | None = None
    measurement_elapsed_ms: float | None = None
    e2e_pair_fps: float | None = None
    post_distance_evaluated: int | None = None
    post_distance_pruned: int | None = None
    post_distance_prune_percent: float | None = None
    parallel_capture: bool | None = None
    pipeline_capture: bool | None = None
    capture_prefetched_pairs: int | None = None
    capture_fallback_fps: float | None = None
    capture_fallback_used: bool | None = None

    def as_record(self) -> dict[str, int | float | bool | str | None]:
        record: dict[str, int | float | bool | str | None] = {
            "frames": self.frames,
            "pair_avg_ms": self.pair_avg_ms,
            "pair_fps": self.pair_fps,
            "dpu_avg_ms": self.dpu_avg_ms,
            "post_avg_ms": self.post_avg_ms,
            "threshold": self.threshold,
        }
        for name in ("front_width", "front_height", "side_width", "side_height"):
            value = getattr(self, name)
            if value is not None:
                record[name] = value
        for name in ("front_fourcc", "side_fourcc"):
            value = getattr(self, name)
            if value is not None:
                record[name] = value
        if self.capture_timestamp_semantics is not None:
            record["capture_timestamp_semantics"] = self.capture_timestamp_semantics
        for name in (
            "front_camera_fps",
            "side_camera_fps",
            "front_capture_fps",
            "side_capture_fps",
            "front_capture_read_mean_ms",
            "side_capture_read_mean_ms",
            "front_capture_read_p95_ms",
            "side_capture_read_p95_ms",
            "front_capture_read_max_ms",
            "side_capture_read_max_ms",
            "front_preprocess_avg_ms",
            "side_preprocess_avg_ms",
            "e2e_pair_avg_ms",
            "measurement_elapsed_ms",
            "e2e_pair_fps",
            "post_distance_prune_percent",
            "capture_fallback_fps",
        ):
            value = getattr(self, name)
            if value is not None:
                record[name] = value
        for name in ("post_distance_evaluated", "post_distance_pruned"):
            value = getattr(self, name)
            if value is not None:
                record[name] = value
        if self.parallel_dpu is not None:
            record["parallel_dpu"] = self.parallel_dpu
        if self.parallel_capture is not None:
            record["parallel_capture"] = self.parallel_capture
        if self.pipeline_capture is not None:
            record["pipeline_capture"] = self.pipeline_capture
        if self.capture_prefetched_pairs is not None:
            record["capture_prefetched_pairs"] = self.capture_prefetched_pairs
        if self.capture_fallback_used is not None:
            record["capture_fallback_used"] = self.capture_fallback_used
        return record


def parse_native_vart_pair(record: str | bytes | Mapping[str, Any]) -> NativeVartPairRecord | None:
    """Parse one JSONL record; return ``None`` for the final statistics record."""

    if isinstance(record, (str, bytes)):
        try:
            payload = json.loads(record)
        except json.JSONDecodeError as exc:
            raise NativeVartStreamError("native VART record is not valid JSON") from exc
    else:
        payload = record
    if not isinstance(payload, Mapping):
        raise NativeVartStreamError("native VART record must be an object")
    record_type = payload.get("type")
    if record_type == "stats":
        return None
    if record_type != "pair":
        raise NativeVartStreamError(f"unexpected native VART record type: {record_type!r}")
    frame_id = _positive_int(payload, "frame_id", allow_zero=True)
    front = _parse_view(payload.get("front"), "front")
    side = _parse_view(payload.get("side"), "side")
    pair_us = payload.get("pair_us")
    if pair_us is not None:
        pair_us = _positive_int_value(pair_us, "pair_us", allow_zero=True)
    return NativeVartPairRecord(frame_id, front, side, pair_us)


def parse_native_vart_stats(record: str | bytes | Mapping[str, Any]) -> NativeVartStats:
    """Parse the final statistics record emitted by the native probe."""

    if isinstance(record, (str, bytes)):
        try:
            payload = json.loads(record)
        except json.JSONDecodeError as exc:
            raise NativeVartStreamError("native VART stats are not valid JSON") from exc
    else:
        payload = record
    if not isinstance(payload, Mapping) or payload.get("type") != "stats":
        raise NativeVartStreamError("native VART stats must be a stats object")
    return NativeVartStats(
        frames=_positive_int(payload, "frames", allow_zero=True),
        pair_avg_ms=_nonnegative_float(payload, "pair_avg_ms"),
        pair_fps=_nonnegative_float(payload, "pair_fps"),
        dpu_avg_ms=_nonnegative_float(payload, "dpu_avg_ms"),
        post_avg_ms=_nonnegative_float(payload, "post_avg_ms"),
        threshold=_bounded_float(payload, "threshold", 0.0, 1.0),
        front_preprocess_avg_ms=_optional_nonnegative_float(
            payload, "front_preprocess_avg_ms"
        ),
        side_preprocess_avg_ms=_optional_nonnegative_float(
            payload, "side_preprocess_avg_ms"
        ),
        front_fourcc=_optional_fourcc(payload, "front_fourcc"),
        side_fourcc=_optional_fourcc(payload, "side_fourcc"),
        front_width=_optional_positive_int(payload, "front_width"),
        front_height=_optional_positive_int(payload, "front_height"),
        side_width=_optional_positive_int(payload, "side_width"),
        side_height=_optional_positive_int(payload, "side_height"),
        front_camera_fps=_optional_nonnegative_float(payload, "front_camera_fps"),
        side_camera_fps=_optional_nonnegative_float(payload, "side_camera_fps"),
        front_capture_fps=_optional_nonnegative_float(payload, "front_capture_fps"),
        side_capture_fps=_optional_nonnegative_float(payload, "side_capture_fps"),
        front_capture_read_mean_ms=_optional_nonnegative_float(
            payload, "front_capture_read_mean_ms"
        ),
        side_capture_read_mean_ms=_optional_nonnegative_float(
            payload, "side_capture_read_mean_ms"
        ),
        front_capture_read_p95_ms=_optional_nonnegative_float(
            payload, "front_capture_read_p95_ms"
        ),
        side_capture_read_p95_ms=_optional_nonnegative_float(
            payload, "side_capture_read_p95_ms"
        ),
        front_capture_read_max_ms=_optional_nonnegative_float(
            payload, "front_capture_read_max_ms"
        ),
        side_capture_read_max_ms=_optional_nonnegative_float(
            payload, "side_capture_read_max_ms"
        ),
        capture_timestamp_semantics=_optional_capture_timestamp_semantics(payload),
        parallel_dpu=_optional_bool(payload, "parallel_dpu"),
        e2e_pair_avg_ms=_optional_nonnegative_float(payload, "e2e_pair_avg_ms"),
        measurement_elapsed_ms=_optional_nonnegative_float(
            payload, "measurement_elapsed_ms"
        ),
        e2e_pair_fps=_optional_nonnegative_float(payload, "e2e_pair_fps"),
        post_distance_evaluated=_optional_nonnegative_int(
            payload, "post_distance_evaluated"
        ),
        post_distance_pruned=_optional_nonnegative_int(
            payload, "post_distance_pruned"
        ),
        post_distance_prune_percent=_optional_percentage(
            payload, "post_distance_prune_percent"
        ),
        parallel_capture=_optional_bool(payload, "parallel_capture"),
        pipeline_capture=_optional_bool(payload, "pipeline_capture"),
        capture_prefetched_pairs=_optional_nonnegative_int(
            payload, "capture_prefetched_pairs"
        ),
        capture_fallback_fps=_optional_nonnegative_float(payload, "capture_fallback_fps"),
        capture_fallback_used=_optional_bool(payload, "capture_fallback_used"),
    )


def iter_native_vart_pairs(lines: Iterable[str]) -> Iterable[NativeVartPairRecord]:
    """Yield pair records and stop naturally when the probe emits its stats record."""

    for line in lines:
        if not line.strip():
            continue
        pair = parse_native_vart_pair(line)
        if pair is None:
            return
        yield pair


def _parse_view(value: Any, name: str) -> NativeVartViewRecord:
    if not isinstance(value, Mapping):
        raise NativeVartStreamError(f"native VART {name} view must be an object")
    timestamp_ns = _positive_int(value, "timestamp_ns", allow_zero=False)
    width = _positive_int(value, "width", allow_zero=False)
    height = _positive_int(value, "height", allow_zero=False)
    raw_points = value.get("points")
    if not isinstance(raw_points, list) or len(raw_points) != len(MOVENET_COCO_KEYPOINTS):
        raise NativeVartStreamError(f"native VART {name} view must contain 17 points")
    points: list[tuple[float, float, float]] = []
    for index, raw_point in enumerate(raw_points):
        if not isinstance(raw_point, (list, tuple)) or len(raw_point) != 3:
            raise NativeVartStreamError(f"native VART {name} point {index} is malformed")
        point = tuple(_finite_float(item, f"{name}.points[{index}]") for item in raw_point)
        if not 0.0 <= point[2] <= 1.0:
            raise NativeVartStreamError(f"native VART {name} point {index} confidence is outside [0,1]")
        points.append(point)  # type: ignore[arg-type]
    return NativeVartViewRecord(timestamp_ns, width, height, tuple(points))


def _positive_int(payload: Mapping[str, Any], name: str, *, allow_zero: bool) -> int:
    if name not in payload:
        raise NativeVartStreamError(f"native VART record is missing {name}")
    return _positive_int_value(payload[name], name, allow_zero=allow_zero)


def _optional_positive_int(payload: Mapping[str, Any], name: str) -> int | None:
    if name not in payload or payload[name] is None:
        return None
    return _positive_int_value(payload[name], name, allow_zero=False)


def _optional_nonnegative_int(payload: Mapping[str, Any], name: str) -> int | None:
    if name not in payload or payload[name] is None:
        return None
    return _positive_int_value(payload[name], name, allow_zero=True)


def _optional_nonnegative_float(payload: Mapping[str, Any], name: str) -> float | None:
    if name not in payload or payload[name] is None:
        return None
    return _nonnegative_float(payload, name)


def _optional_percentage(payload: Mapping[str, Any], name: str) -> float | None:
    if name not in payload or payload[name] is None:
        return None
    return _bounded_float(payload, name, 0.0, 100.0)


def _optional_bool(payload: Mapping[str, Any], name: str) -> bool | None:
    if name not in payload or payload[name] is None:
        return None
    value = payload[name]
    if not isinstance(value, bool):
        raise NativeVartStreamError(f"native VART {name} must be boolean")
    return value


def _optional_fourcc(payload: Mapping[str, Any], name: str) -> str | None:
    if name not in payload or payload[name] is None:
        return None
    value = payload[name]
    if value == "":
        return None
    if not isinstance(value, str) or len(value) != 4:
        raise NativeVartStreamError(
            f"native VART {name} must contain exactly four characters"
        )
    return value


def _optional_capture_timestamp_semantics(payload: Mapping[str, Any]) -> str | None:
    name = "capture_timestamp_semantics"
    if name not in payload or payload[name] is None:
        return None
    value = payload[name]
    if value != "host_monotonic_after_retrieve":
        raise NativeVartStreamError(
            "native VART capture_timestamp_semantics is unsupported"
        )
    return value


def _positive_int_value(value: Any, name: str, *, allow_zero: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or (not allow_zero and value <= 0) or (allow_zero and value < 0):
        raise NativeVartStreamError(f"native VART {name} must be a non-negative integer")
    return value


def _finite_float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise NativeVartStreamError(f"native VART {name} must be finite")
    try:
        converted = float(value)
    except (TypeError, ValueError) as exc:
        raise NativeVartStreamError(f"native VART {name} must be finite") from exc
    if not math.isfinite(converted):
        raise NativeVartStreamError(f"native VART {name} must be finite")
    return converted


def _nonnegative_float(payload: Mapping[str, Any], name: str) -> float:
    value = _finite_float(payload.get(name), name)
    if value < 0.0:
        raise NativeVartStreamError(f"native VART {name} must be non-negative")
    return value


def _bounded_float(
    payload: Mapping[str, Any], name: str, lower: float, upper: float
) -> float:
    value = _finite_float(payload.get(name), name)
    if not lower <= value <= upper:
        raise NativeVartStreamError(
            f"native VART {name} must be within [{lower},{upper}]"
        )
    return value


__all__ = [
    "NativeVartPairRecord",
    "NativeVartStats",
    "NativeVartStreamError",
    "NativeVartViewRecord",
    "iter_native_vart_pairs",
    "parse_native_vart_pair",
    "parse_native_vart_stats",
]
