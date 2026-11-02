"""Build a reviewable record for one static-image pose inference."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Mapping

from .contracts import CameraView, Keypoint, PoseFrame
from .pose_adapter import ResizeWithPadTransform


@dataclass(frozen=True, slots=True)
class PoseProbeKeypointDelta:
    name: str
    comparable: bool
    position_distance: float | None
    confidence_abs_delta: float


@dataclass(frozen=True, slots=True)
class PoseProbeComparison:
    baseline_backend: str
    candidate_backend: str
    confidence_threshold: float
    maximum_position_distance: float
    minimum_comparable_keypoints: int
    comparable_keypoints: int
    mean_position_distance: float | None
    p95_position_distance: float | None
    max_position_distance: float | None
    mean_confidence_abs_delta: float
    within_tolerance: bool
    keypoints: tuple[PoseProbeKeypointDelta, ...]

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = asdict(self)
        record["comparison"] = "movenet_static_probe"
        return record


def build_pose_probe_record(
    pose: PoseFrame,
    transform: ResizeWithPadTransform,
    *,
    backend: str,
    confidence_threshold: float = 0.3,
) -> dict[str, object]:
    """Describe decoded source coordinates without retaining image pixels."""

    if not backend:
        raise ValueError("pose probe backend must not be empty")
    if not 0.0 <= confidence_threshold <= 1.0:
        raise ValueError("confidence_threshold must be within [0, 1]")
    keypoints = {
        name: {
            "x": point.x,
            "y": point.y,
            "confidence": point.confidence,
        }
        for name, point in pose.keypoints.items()
    }
    confident_count = sum(
        point.confidence >= confidence_threshold for point in pose.keypoints.values()
    )
    return {
        "schema_version": 1,
        "probe": "movenet_static_image",
        "backend": backend,
        "view": pose.view.value,
        "source_width": transform.source_width,
        "source_height": transform.source_height,
        "model_width": transform.target_width,
        "model_height": transform.target_height,
        "resize_with_pad": {
            "resized_width": transform.resized_width,
            "resized_height": transform.resized_height,
            "pad_left": transform.pad_left,
            "pad_right": transform.pad_right,
            "pad_top": transform.pad_top,
            "pad_bottom": transform.pad_bottom,
        },
        "confidence_threshold": confidence_threshold,
        "keypoint_count": len(keypoints),
        "confident_keypoint_count": confident_count,
        "keypoints_source_normalized": keypoints,
        "contains_raw_pixels": False,
        "performance_measurement": False,
    }


def compare_pose_probe_records(
    baseline: Mapping[str, object],
    candidate: Mapping[str, object],
    *,
    confidence_threshold: float = 0.3,
    maximum_position_distance: float = 0.03,
    minimum_comparable_keypoints: int = 8,
) -> PoseProbeComparison:
    """Compare decoded source coordinates from the same manually selected image."""

    if not 0.0 <= confidence_threshold <= 1.0:
        raise ValueError("confidence_threshold must be within [0, 1]")
    if not math.isfinite(maximum_position_distance) or maximum_position_distance < 0.0:
        raise ValueError("maximum_position_distance must be finite and non-negative")
    if minimum_comparable_keypoints < 1:
        raise ValueError("minimum_comparable_keypoints must be positive")
    for record in (baseline, candidate):
        if record.get("schema_version") != 1 or record.get("probe") != "movenet_static_image":
            raise ValueError("unsupported pose probe record")
    geometry_keys = ("view", "source_width", "source_height", "model_width", "model_height")
    if any(baseline.get(key) != candidate.get(key) for key in geometry_keys):
        raise ValueError("pose probe geometry does not match")

    baseline_points = _probe_points(baseline)
    candidate_points = _probe_points(candidate)
    if set(baseline_points) != set(candidate_points):
        raise ValueError("pose probe keypoint names do not match")

    deltas = []
    position_distances = []
    confidence_deltas = []
    for name in baseline_points:
        base_x, base_y, base_confidence = baseline_points[name]
        candidate_x, candidate_y, candidate_confidence = candidate_points[name]
        confidence_delta = abs(candidate_confidence - base_confidence)
        confidence_deltas.append(confidence_delta)
        comparable = min(base_confidence, candidate_confidence) >= confidence_threshold
        distance = (
            math.hypot(candidate_x - base_x, candidate_y - base_y)
            if comparable
            else None
        )
        if distance is not None:
            position_distances.append(distance)
        deltas.append(
            PoseProbeKeypointDelta(name, comparable, distance, confidence_delta)
        )

    ordered = sorted(position_distances)
    mean_distance = (
        sum(position_distances) / len(position_distances)
        if position_distances
        else None
    )
    p95_distance = (
        ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)] if ordered else None
    )
    max_distance = ordered[-1] if ordered else None
    comparable_count = len(position_distances)
    return PoseProbeComparison(
        baseline_backend=str(baseline.get("backend", "unknown")),
        candidate_backend=str(candidate.get("backend", "unknown")),
        confidence_threshold=confidence_threshold,
        maximum_position_distance=maximum_position_distance,
        minimum_comparable_keypoints=minimum_comparable_keypoints,
        comparable_keypoints=comparable_count,
        mean_position_distance=mean_distance,
        p95_position_distance=p95_distance,
        max_position_distance=max_distance,
        mean_confidence_abs_delta=sum(confidence_deltas) / len(confidence_deltas),
        within_tolerance=(
            comparable_count >= minimum_comparable_keypoints
            and max_distance is not None
            and max_distance <= maximum_position_distance
        ),
        keypoints=tuple(deltas),
    )


def pose_frame_from_probe_record(
    record: Mapping[str, object],
    *,
    frame_id: int = 0,
    timestamp_ns: int = 0,
) -> PoseFrame:
    """Restore a pixel-free static probe as a PoseFrame for downstream checks."""

    if record.get("schema_version") != 1 or record.get("probe") != "movenet_static_image":
        raise ValueError("unsupported pose probe record")
    try:
        view = CameraView(str(record["view"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("invalid pose probe view") from exc
    points = {
        name: Keypoint(x, y, confidence)
        for name, (x, y, confidence) in _probe_points(record).items()
    }
    try:
        source_width = int(record["source_width"])
        source_height = int(record["source_height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid pose probe source dimensions") from exc
    return PoseFrame(
        view,
        frame_id,
        timestamp_ns,
        points,
        source_width,
        source_height,
    )


def _probe_points(record: Mapping[str, object]) -> dict[str, tuple[float, float, float]]:
    raw_points = record.get("keypoints_source_normalized")
    if not isinstance(raw_points, Mapping) or not raw_points:
        raise ValueError("pose probe keypoints are missing")
    points = {}
    for name, raw_point in raw_points.items():
        if not isinstance(name, str) or not isinstance(raw_point, Mapping):
            raise ValueError("invalid pose probe keypoint")
        try:
            values = tuple(float(raw_point[field]) for field in ("x", "y", "confidence"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid pose probe keypoint: {name}") from exc
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"non-finite pose probe keypoint: {name}")
        x, y, confidence = values
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and 0.0 <= confidence <= 1.0):
            raise ValueError(f"pose probe keypoint outside [0, 1]: {name}")
        points[name] = values
    return points
