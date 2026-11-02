# SPDX-License-Identifier: MIT
"""Transparent aggregate scorecard, never calibrated 3D accuracy or acceptance."""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any


_STATUSES = frozenset({
    "accepted_under_assumptions", "incomplete_markers", "nonconvex_or_mirrored_ids",
    "degenerate_marker_geometry", "planar_solver_failed", "no_positive_finite_pose",
    "reprojection_rejected", "ambiguous_pose",
})
_RESIDUAL_STATUSES = frozenset({
    "accepted_under_assumptions", "reprojection_rejected", "ambiguous_pose",
})


def _count(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer, not bool")
    return value


def _finite(value: Any, name: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be finite numeric, not bool")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite numeric")
    if number < 0 or positive and number == 0:
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")
    return number


def evaluate_relative_object_summary(summary: Mapping[str, Any], *,
                                     reprojection_budget_px: float) -> dict[str, Any]:
    """Evaluate exactly the counts reported by a camera-v1 session, not raw pixels.

    The explicit budget is an evaluation scale only, never a solver threshold.
    Missing per-frame association/time/ground-truth data remain unscored.
    """
    budget = _finite(reprojection_budget_px, "evaluation reprojection budget", positive=True)
    if not isinstance(summary, Mapping) or summary.get("schema") != "posepriv.relative_object_camera.v1":
        raise ValueError("unsupported relative object camera summary schema")
    for name in ("metric_geometry", "DPU_executed", "PL_executed", "physical_scene_validated",
                 "performance_measurement", "contains_raw_pixels", "contains_point_coordinates"):
        if summary.get(name) is not False:
            raise ValueError(f"camera v1 must explicitly declare {name}=false")
    if (summary.get("camera_parameters") != "assumed_not_calibrated"
            or summary.get("method") != "single_view_planar_PnP_not_stereo"
            or summary.get("units") != "marker_width_units"
            or summary.get("model_depth_is_prior") is not True):
        raise ValueError("relative object assumptions are missing or inconsistent")
    frames = _count(summary.get("processed_frames"), "processed_frames")
    if not 1 <= frames <= 10000:
        raise ValueError("camera v1 processed_frames must be within [1,10000]")
    accepted = _count(summary.get("accepted_under_assumptions_frames"), "accepted frames")
    statuses = summary.get("status_counts")
    if not isinstance(statuses, Mapping) or any(status not in _STATUSES for status in statuses):
        raise ValueError("camera v1 has missing or unknown status counts")
    counts = {status: _count(count, f"status_counts.{status}") for status, count in statuses.items()}
    if sum(counts.values()) != frames or counts.get("accepted_under_assumptions", 0) != accepted:
        raise ValueError("camera v1 frame/status/acceptance counts disagree")
    diagnosed = sum(counts.get(status, 0) for status in _RESIDUAL_STATUSES)
    maximum = summary.get("maximum_reprojection_px")
    if diagnosed:
        maximum = _finite(maximum, "maximum_reprojection_px")
    elif maximum is not None:
        raise ValueError("camera v1 has a residual without diagnosed frames")
    # Explicitly carry only assumptions, not arbitrary input fields or source paths.
    width = _count(summary.get("source_width"), "source_width")
    height = _count(summary.get("source_height"), "source_height")
    if not 1 <= width <= 4096 or not 1 <= height <= 2160:
        raise ValueError("camera v1 source dimensions are outside the contract")
    focal = _finite(summary.get("focal_width_ratio"), "focal_width_ratio", positive=True)
    marker_height = _finite(summary.get("marker_height_ratio"), "marker_height_ratio", positive=True)
    depth = _finite(summary.get("model_depth_ratio"), "model_depth_ratio")
    if focal > 8 or not .1 <= marker_height <= 10 or depth > 10:
        raise ValueError("camera v1 assumptions are outside the existing model contract")
    covered = frames - counts.get("incomplete_markers", 0)
    reprojection_score = None
    if diagnosed:
        # Compare first: avoid overflow on a tiny positive evaluation budget.
        reprojection_score = 0.0 if maximum >= budget else 100.0 * (1.0 - maximum / budget)
    metrics = {
        "usable_color_correspondence_coverage": {
            "score": 100.0 * covered / frames, "units": "percent",
            "numerator": covered, "denominator": frames,
            "formula": "100*(processed-incomplete_markers)/processed",
            "scope": "complete_color_ids_only_not_pose_or_geometry_accuracy",
        },
        "model_acceptance_under_assumptions": {
            "score": 100.0 * accepted / frames, "units": "percent",
            "numerator": accepted, "denominator": frames,
            "formula": "100*accepted_under_assumptions/processed",
            "scope": "solver_acceptance_under_explicit_assumptions_not_ground_truth",
        },
        "conditional_reprojection_consistency": {
            "score": reprojection_score, "units": "points_out_of_100",
            "diagnosed_frames": diagnosed, "maximum_px": maximum, "budget_px": budget,
            "formula": "100*max(0,1-maximum_reprojection_px/budget_px)",
            "scope": "worst_observed_residual_including_rejected_and_ambiguous_frames",
            "unscored_reason": None if diagnosed else "no_diagnosed_reprojection_samples",
        },
        "same_frame_integrity": {
            "score": None, "unscored_reason": "aggregate_v1_has_no_per_frame_source_result_association",
        },
        "temporal_stability": {
            "score": None, "unscored_reason": "aggregate_v1_has_no_timestamp_or_pose_sequence",
        },
        "metric_3d_accuracy": {
            "score": None, "unscored_reason": "no_real_calibration_or_3d_ground_truth",
        },
    }
    return {
        "schema": "posepriv.relative_object_evaluation.v1",
        "source_schema": "posepriv.relative_object_camera.v1",
        "evidence_scope": "aggregate_self_report_not_independent_validation",
        "camera_parameters": "assumed_not_calibrated", "metric_geometry": False,
        "DPU_executed": False, "PL_executed": False, "physical_scene_validated": False,
        "performance_measurement": False, "contains_raw_pixels": False,
        "contains_point_coordinates": False, "overall_score": None,
        "overall_unscored_reason": "incomparable_components_and_missing_integrity_stability_ground_truth",
        "assumptions": {"source_width": width, "source_height": height,
                        "focal_width_ratio": focal, "marker_height_ratio": marker_height,
                        "model_depth_ratio": depth, "model_depth_is_prior": True,
                        "units": "marker_width_units"},
        "processed_frames": frames, "status_counts": counts, "metrics": metrics,
        "warnings": ["evaluation_budget_does_not_change_detection_or_solver_thresholds",
                     "cross_session_scores_require_same_budget_assumptions_and_input_conditions",
                     "scores_are_not_human_pose_or_surface_reconstruction_accuracy"]
                    + (["no_accepted_relative_model_frames"] if accepted == 0 else []),
    }
