"""Provisional dual-view squat metrics with explicit invalid-data behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..contracts import CameraView, Keypoint, PoseFrame, SyncedPosePair
from ..geometry import (
    inclination_from_vertical_degrees,
    joint_angle_degrees,
    minimum_confidence,
    to_isotropic_image_coordinates,
)
from ..scoring import MetricObservation
from ..pl_metrics import (
    JointSpec,
    JointMetricBackend,
    PreparedJoint,
    combine_joint_batches,
    joint_angle_degrees_from_result,
    prepare_pose_joints,
    reference_joint_output,
    resolve_joint_results,
    run_joint_metrics,
)


SQUAT_KNEE_JOINT_SPECS = (
    JointSpec(0, "left_hip", "left_knee", "left_ankle"),
    JointSpec(1, "right_hip", "right_knee", "right_ankle"),
)

LEGACY_SIDE_METRIC_POLICY = "legacy_chain_sum_v0"
PER_METRIC_SIDE_POLICY = "per_metric_confidence_v1"
SIDE_METRIC_POLICIES = (LEGACY_SIDE_METRIC_POLICY, PER_METRIC_SIDE_POLICY)


@dataclass(frozen=True, slots=True)
class SelectedSideMetric:
    observation: MetricObservation
    side: str


def select_side_metrics(frame: PoseFrame) -> Mapping[str, SelectedSideMetric]:
    """Select actual geometry by the confidence of each metric's own inputs.

    Neither angle value, conformance score nor allowed-band membership participates
    in selection. Ties keep the first (left) side. No missing point is reconstructed.
    """
    if frame.view is not CameraView.SIDE:
        raise ValueError("side metric selection requires a side-view frame")
    selected: dict[str, SelectedSideMetric] = {}
    for side in ("left", "right"):
        depth_points = _required(frame, tuple(f"{side}_{j}" for j in ("hip", "knee", "ankle")))
        angle_points = _required(frame, tuple(f"{side}_{j}" for j in ("shoulder", "hip")))
        candidates = {}
        if depth_points is not None:
            hip, knee, ankle = _metric_points(frame, depth_points)
            denominator = abs(ankle.y - knee.y)
            if denominator > 1e-9:
                candidates["side_depth_ratio"] = MetricObservation(
                    abs(ankle.y - hip.y) / denominator, minimum_confidence(*depth_points))
        if angle_points is not None:
            angle = inclination_from_vertical_degrees(*_metric_points(frame, angle_points))
            if angle is not None:
                candidates["side_trunk_angle_deg"] = MetricObservation(angle, minimum_confidence(*angle_points))
        for name, observation in candidates.items():
            if name not in selected or observation.confidence > selected[name].observation.confidence:
                selected[name] = SelectedSideMetric(observation, side)
    return selected


@dataclass(frozen=True, slots=True)
class SquatJointMetricBatch:
    side_knee_angle: MetricObservation | None
    score_observations: Mapping[str, MetricObservation]


def prepare_squat_knee_joints(
    frame: PoseFrame,
    *,
    minimum_keypoint_confidence: float = 0.5,
) -> tuple[PreparedJoint, ...]:
    """Prepare every sufficiently visible knee for the shared PL metric core."""

    return prepare_pose_joints(
        frame,
        SQUAT_KNEE_JOINT_SPECS,
        minimum_keypoint_confidence=minimum_keypoint_confidence,
    )


def extract_side_knee_angle_via_pl_reference(
    frame: PoseFrame,
) -> MetricObservation | None:
    """Exercise the exact PS/PL contract while using the software golden core."""

    if frame.view is not CameraView.SIDE:
        raise ValueError("side knee angle requires a side-view frame")
    prepared = prepare_squat_knee_joints(frame)
    output_beats = tuple(reference_joint_output(item.beat) for item in prepared)
    resolved = resolve_joint_results(prepared, output_beats)

    observations = []
    for item in resolved:
        angle = joint_angle_degrees_from_result(item.result)
        if angle is not None:
            observations.append(MetricObservation(angle, item.confidence))
    return max(observations, key=lambda observation: observation.confidence, default=None)


def extract_squat_joint_metrics(
    pair: SyncedPosePair,
    backend: JointMetricBackend,
    *,
    minimum_keypoint_confidence: float = 0.5,
) -> SquatJointMetricBatch:
    """Batch both views through one backend transaction for tracking and scoring."""

    front = prepare_squat_knee_joints(
        pair.front,
        minimum_keypoint_confidence=minimum_keypoint_confidence,
    )
    side = prepare_squat_knee_joints(
        pair.side,
        minimum_keypoint_confidence=minimum_keypoint_confidence,
    )
    resolved = run_joint_metrics(combine_joint_batches(front, side), backend)

    angles: dict[tuple[int, int], MetricObservation] = {}
    for item in resolved:
        angle = joint_angle_degrees_from_result(item.result)
        if angle is not None:
            angles[(item.result.view_id, item.result.joint_id)] = MetricObservation(
                angle,
                item.confidence,
            )

    side_candidates = tuple(
        observation
        for (view_id, _joint_id), observation in angles.items()
        if view_id == 1
    )
    side_knee = max(
        side_candidates,
        key=lambda observation: observation.confidence,
        default=None,
    )

    score_observations: dict[str, MetricObservation] = {}
    left = angles.get((0, 0))
    right = angles.get((0, 1))
    if left is not None and right is not None and right.value > 1e-9:
        score_observations["front_knee_symmetry"] = MetricObservation(
            value=left.value / right.value,
            confidence=min(left.confidence, right.confidence),
        )
    return SquatJointMetricBatch(side_knee, score_observations)


def _required(frame: PoseFrame, names: tuple[str, ...]) -> tuple[Keypoint, ...] | None:
    try:
        return tuple(frame.keypoints[name] for name in names)
    except KeyError:
        return None


def _metric_points(frame: PoseFrame, points: tuple[Keypoint, ...]) -> tuple[Keypoint, ...]:
    return tuple(
        to_isotropic_image_coordinates(
            point,
            frame.source_width,
            frame.source_height,
        )
        for point in points
    )


def _best_side_chain(frame: PoseFrame) -> tuple[Keypoint, Keypoint, Keypoint, Keypoint] | None:
    candidates: list[tuple[Keypoint, Keypoint, Keypoint, Keypoint]] = []
    for side in ("left", "right"):
        points = _required(
            frame,
            (
                f"{side}_shoulder",
                f"{side}_hip",
                f"{side}_knee",
                f"{side}_ankle",
            ),
        )
        if points is not None and len(points) == 4:
            candidates.append((points[0], points[1], points[2], points[3]))
    if not candidates:
        return None
    return max(candidates, key=lambda points: sum(point.confidence for point in points))


def extract_side_knee_angle(frame: PoseFrame) -> MetricObservation | None:
    """Return the best visible side knee angle and its weakest keypoint confidence."""

    if frame.view is not CameraView.SIDE:
        raise ValueError("side knee angle requires a side-view frame")
    observations = []
    for side in ("left", "right"):
        points = _required(frame, (f"{side}_hip", f"{side}_knee", f"{side}_ankle"))
        if points is None:
            continue
        angle = joint_angle_degrees(*_metric_points(frame, points))
        if angle is not None:
            observations.append(MetricObservation(angle, minimum_confidence(*points)))
    return max(observations, key=lambda observation: observation.confidence, default=None)


def extract_squat_observations(
    pair: SyncedPosePair,
    *,
    tempo_seconds: float | None = None,
    include_front_knee_symmetry: bool = True,
    side_metric_policy: str = LEGACY_SIDE_METRIC_POLICY,
) -> Mapping[str, MetricObservation]:
    if side_metric_policy not in SIDE_METRIC_POLICIES:
        raise ValueError("unsupported side metric selection policy")
    observations: dict[str, MetricObservation] = {}

    side_chain = _best_side_chain(pair.side)
    if side_chain is not None:
        shoulder, hip, knee, ankle = side_chain
        metric_shoulder, metric_hip, metric_knee, metric_ankle = _metric_points(
            pair.side,
            (shoulder, hip, knee, ankle),
        )
        knee_to_ankle_vertical = abs(metric_ankle.y - metric_knee.y)
        if knee_to_ankle_vertical > 1e-9:
            observations["side_depth_ratio"] = MetricObservation(
                value=abs(metric_ankle.y - metric_hip.y) / knee_to_ankle_vertical,
                confidence=minimum_confidence(hip, knee, ankle),
            )
        trunk_angle = inclination_from_vertical_degrees(metric_shoulder, metric_hip)
        if trunk_angle is not None:
            observations["side_trunk_angle_deg"] = MetricObservation(
                value=trunk_angle,
                confidence=minimum_confidence(shoulder, hip),
            )

    if include_front_knee_symmetry:
        left = _required(pair.front, ("left_hip", "left_knee", "left_ankle"))
        right = _required(pair.front, ("right_hip", "right_knee", "right_ankle"))
        if left is not None and right is not None:
            left_angle = joint_angle_degrees(*_metric_points(pair.front, left))
            right_angle = joint_angle_degrees(*_metric_points(pair.front, right))
            if left_angle is not None and right_angle is not None and right_angle > 1e-9:
                observations["front_knee_symmetry"] = MetricObservation(
                    value=left_angle / right_angle,
                    confidence=minimum_confidence(*left, *right),
                )

    if tempo_seconds is not None and tempo_seconds > 0.0:
        observations["tempo_seconds"] = MetricObservation(value=tempo_seconds, confidence=1.0)

    if side_metric_policy == PER_METRIC_SIDE_POLICY:
        for name in ("side_depth_ratio", "side_trunk_angle_deg"):
            observations.pop(name, None)
        observations.update({name: item.observation for name, item in select_side_metrics(pair.side).items()})

    return observations
