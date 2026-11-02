"""Exercise-specific metric extraction."""

from .squat import (
    SQUAT_KNEE_JOINT_SPECS,
    SquatJointMetricBatch,
    extract_side_knee_angle,
    extract_side_knee_angle_via_pl_reference,
    extract_squat_joint_metrics,
    extract_squat_observations,
    prepare_squat_knee_joints,
)
from .squat_tracker import SquatPhase, SquatPhaseConfig, SquatTracker, SquatUpdate

__all__ = [
    "SquatPhase",
    "SquatPhaseConfig",
    "SquatTracker",
    "SquatUpdate",
    "extract_side_knee_angle",
    "extract_side_knee_angle_via_pl_reference",
    "extract_squat_observations",
    "extract_squat_joint_metrics",
    "prepare_squat_knee_joints",
    "SQUAT_KNEE_JOINT_SPECS",
    "SquatJointMetricBatch",
]
