"""Versioned JSONL report for repetitions and end-to-end runtime statistics."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from types import TracebackType
from typing import Any, Mapping, TextIO

from .capture import CapturePumpStats
from .avatar_runtime import AvatarDispatchStats, AvatarRuntimeStats
from .contracts import ExerciseScore
from .comparison_display import ComparisonRenderStats
from .display_dispatch import DisplayDispatchStats
from .movenet_vart import VartInferenceStats
from .motion_window import MotionWindowStats
from .pl_metrics import JointMetricBackendStats
from .pipeline import PipelineStats
from .runtime_identity import read_linux_boot_id
from .session import FeedbackCue, SquatSessionUpdate
from .skeleton_display import SkeletonRenderStats


REPORT_SCHEMA_VERSION = 1


class CoachSessionReportWriter:
    """Record explainable results and timing without images or keypoint coordinates."""

    def __init__(
        self,
        path: str | Path,
        *,
        exercise: str,
        rule_set: str,
        joint_metric_backend: str = "unspecified",
        performance_measurement: bool = True,
        configuration: Mapping[str, Any] | None = None,
    ) -> None:
        self.path = Path(path)
        self._stream: TextIO = self.path.open("w", encoding="utf-8", newline="\n")
        self._closed = False
        header: dict[str, Any] = {
            "record_type": "coach_session_header",
            "schema_version": REPORT_SCHEMA_VERSION,
            "exercise": exercise,
            "rule_set": rule_set,
            "joint_metric_backend": joint_metric_backend,
            "performance_measurement": performance_measurement,
            "timestamp_unit": "ns_monotonic",
            "contains_raw_pixels": False,
            "contains_keypoints": False,
        }
        linux_boot_id = read_linux_boot_id()
        if linux_boot_id is not None:
            header["linux_boot_id"] = linux_boot_id
        if configuration is not None:
            header["configuration"] = dict(configuration)
        self._write_record(header)

    def write_repetition(self, update: SquatSessionUpdate) -> None:
        if self._closed:
            raise ValueError("coach session report is closed")
        if not update.repetition_completed or update.repetition_score is None:
            raise ValueError("only completed repetitions can be written")
        self._write_record(
            {
                "record_type": "completed_repetition",
                "timestamp_ns": update.timestamp_ns,
                "front_frame_id": update.front_frame_id,
                "side_frame_id": update.side_frame_id,
                "skew_ms": update.skew_ns / 1_000_000,
                "repetition_count": update.repetition_count,
                "duration_seconds": update.repetition_duration_seconds,
                "score": _score_record(update.repetition_score),
                "cues": [
                    _cue_record(cue)
                    for cue in update.cues
                ],
            }
        )

    def write_summary(
        self,
        pipeline: PipelineStats,
        capture: CapturePumpStats | None = None,
        avatar: AvatarRuntimeStats | None = None,
        render: ComparisonRenderStats | SkeletonRenderStats | None = None,
        vart: VartInferenceStats | None = None,
        motion: MotionWindowStats | None = None,
        joint: JointMetricBackendStats | None = None,
        native_vart: Mapping[str, Any] | None = None,
        display_dispatch: DisplayDispatchStats | None = None,
        avatar_dispatch: AvatarDispatchStats | None = None,
    ) -> None:
        if self._closed:
            raise ValueError("coach session report is closed")
        sync = pipeline.pose_sync
        record: dict[str, Any] = {
            "record_type": "session_summary",
            "pipeline": {
                "processed_frames": pipeline.processed_frames,
                "processed_front_frames": pipeline.processed_front_frames,
                "processed_side_frames": pipeline.processed_side_frames,
                "synchronized_updates": pipeline.synchronized_updates,
                "dropped_front": pipeline.dropped_front,
                "dropped_side": pipeline.dropped_side,
                "mean_inference_ms": pipeline.mean_inference_ms,
                "mean_front_inference_ms": pipeline.mean_front_inference_ms,
                "mean_side_inference_ms": pipeline.mean_side_inference_ms,
                "mean_session_ms": pipeline.mean_session_ms,
                "mean_end_to_end_ms": pipeline.mean_end_to_end_ms,
                "p95_inference_ms": pipeline.p95_inference_ms,
                "p95_front_inference_ms": pipeline.p95_front_inference_ms,
                "p95_side_inference_ms": pipeline.p95_side_inference_ms,
                "p95_session_ms": pipeline.p95_session_ms,
                "p95_end_to_end_ms": pipeline.p95_end_to_end_ms,
                "max_end_to_end_ms": pipeline.max_end_to_end_ns / 1_000_000,
                "latency_sample_count": pipeline.latency_sample_count,
                "latency_window_size": pipeline.latency_window_size,
                "observed_processing_fps": pipeline.observed_processing_fps,
                "observed_front_fps": pipeline.observed_front_fps,
                "observed_side_fps": pipeline.observed_side_fps,
                "observed_synchronized_pair_fps": (
                    pipeline.observed_synchronized_pair_fps
                ),
            },
            "pose_sync": {
                "paired_frames": sync.paired_frames,
                "dropped_front": sync.dropped_front,
                "dropped_side": sync.dropped_side,
                "queue_overflow_front": sync.queue_overflow_front,
                "queue_overflow_side": sync.queue_overflow_side,
                "skew_rejected_front": sync.skew_rejected_front,
                "skew_rejected_side": sync.skew_rejected_side,
                "superseded_front": sync.superseded_front,
                "superseded_side": sync.superseded_side,
                "skew_sample_count": sync.skew_sample_count,
                "mean_skew_ms": sync.mean_skew_ms,
                "p95_skew_ms": sync.p95_skew_ms,
                "max_skew_ms": sync.max_skew_ms,
            },
        }
        if capture is not None:
            record["capture"] = {
                "front_frames": capture.front_frames,
                "side_frames": capture.side_frames,
                "running": capture.running,
                "failed": capture.failed,
            }
            # Optional fields keep older recorded sessions readable while making
            # the UVC/VideoCapture boundary visible in live reports.
            if capture.front_read_timing is not None:
                record["capture"]["front_read_timing"] = (
                    capture.front_read_timing.to_record()
                )
            if capture.side_read_timing is not None:
                record["capture"]["side_read_timing"] = (
                    capture.side_read_timing.to_record()
                )
        if motion is not None:
            record["motion_telemetry"] = {
                "frames": motion.frames,
                "feature_count": motion.feature_count,
                "front_feature_count": motion.front_feature_count,
                "side_feature_count": motion.side_feature_count,
                "mean_acceleration_per_s2_sq": (
                    motion.mean_acceleration_per_s2_sq
                ),
                "max_acceleration_per_s2_sq": motion.max_acceleration_per_s2_sq,
                "interpretation": "motion_candidate_only_not_fatigue_diagnosis",
            }
            if motion.backend is not None:
                record["motion_telemetry"]["backend"] = dict(motion.backend)
            if motion.batch_stats is not None:
                record["motion_telemetry"]["batch_stats"] = dict(motion.batch_stats)
        if joint is not None:
            record["joint_metric_runtime"] = {
                "batches": joint.batches,
                "input_beats": joint.input_beats,
                "tx_bytes": joint.tx_bytes,
                "rx_bytes": joint.rx_bytes,
                "total_ms": joint.total_ns / 1_000_000,
                "mean_ms": joint.mean_ms,
                "max_ms": joint.max_ns / 1_000_000,
                "p95_ms": joint.p95_ms,
                "latency_sample_count": joint.latency_sample_count,
                "latency_window_size": joint.latency_window_size,
            }
        if avatar is not None:
            record["avatar_runtime"] = {
                "frames": avatar.frames,
                "total_plan_ms": avatar.total_plan_ns / 1_000_000,
                "total_skinning_ms": avatar.total_skinning_ns / 1_000_000,
                "total_display_ms": avatar.total_display_ns / 1_000_000,
                "total_ms": avatar.total_ns / 1_000_000,
                "mean_total_ms": avatar.mean_total_ms,
                "max_total_ms": avatar.max_total_ns / 1_000_000,
                "p95_plan_ms": avatar.p95_plan_ms,
                "p95_skinning_ms": avatar.p95_skinning_ms,
                "p95_display_ms": avatar.p95_display_ms,
                "p95_total_ms": avatar.p95_total_ms,
                "held_matrix_uses": avatar.held_matrix_uses,
                "expired_matrix_uses": avatar.expired_matrix_uses,
                "latency_sample_count": avatar.latency_sample_count,
                "latency_window_size": avatar.latency_window_size,
                "total_vertex_count": avatar.total_vertex_count,
                "total_packet_tx_bytes": avatar.total_packet_tx_bytes,
                "total_packet_rx_bytes": avatar.total_packet_rx_bytes,
                "triangulated_point_count": avatar.triangulated_point_count,
                "valid_triangulated_point_count": (
                    avatar.valid_triangulated_point_count
                ),
                "valid_triangulated_percent": avatar.valid_triangulated_percent,
                "finite_ray_gap_percent": avatar.finite_ray_gap_percent,
                "accepted_finite_ray_percent": avatar.accepted_finite_ray_percent,
                "finite_ray_gap_count": avatar.finite_ray_gap_count,
                "mean_ray_gap_world_units": avatar.mean_ray_gap_world_units,
                "p95_ray_gap_world_units": avatar.p95_ray_gap_world_units,
                "ray_angle_sample_count": avatar.ray_angle_sample_count,
                "mean_ray_angle_degrees": avatar.mean_ray_angle_degrees,
                "p05_ray_angle_degrees": avatar.p05_ray_angle_degrees,
                "triangulation_geometry_backend": (
                    avatar.triangulation_geometry_backend
                ),
                "triangulation_accelerator_backend": (
                    avatar.triangulation_accelerator_backend
                ),
            }
            if avatar.triangulation_backend_stats is not None:
                record["avatar_runtime"]["triangulation_backend_stats"] = (
                    avatar.triangulation_backend_stats.to_record()
                )
            if avatar.joint_filter is not None:
                filter_stats = avatar.joint_filter
                record["avatar_joint_filter"] = {
                    "frames": filter_stats.frames,
                    "valid_samples": filter_stats.valid_samples,
                    "filtered_samples": filter_stats.filtered_samples,
                    "reset_samples": filter_stats.reset_samples,
                    "invalid_samples": filter_stats.invalid_samples,
                    "repeated_timestamp_samples": (
                        filter_stats.repeated_timestamp_samples
                    ),
                    "mean_correction_world_units": filter_stats.mean_correction,
                    "max_correction_world_units": filter_stats.max_correction,
                }
        if render is not None:
            record["display_render"] = {
                "frames": render.frames,
                "total_ms": render.total_ns / 1_000_000,
                "mean_ms": render.mean_ms,
                "max_ms": render.max_ns / 1_000_000,
                "p95_ms": render.p95_ms,
                "latency_sample_count": render.latency_sample_count,
                "latency_window_size": render.latency_window_size,
                "compose_total_ms": render.compose_total_ns / 1_000_000,
                "compose_mean_ms": render.compose_mean_ms,
                "compose_max_ms": render.compose_max_ns / 1_000_000,
                "compose_p95_ms": render.compose_p95_ms,
                "sink_total_ms": render.sink_total_ns / 1_000_000,
                "sink_mean_ms": render.sink_mean_ms,
                "sink_max_ms": render.sink_max_ns / 1_000_000,
                "sink_p95_ms": render.sink_p95_ms,
            }
        if display_dispatch is not None:
            record["display_dispatch"] = {
                "submitted_frames": display_dispatch.submitted_frames,
                "rendered_frames": display_dispatch.rendered_frames,
                "dropped_frames": display_dispatch.dropped_frames,
                "failed": display_dispatch.failed,
                "measurement_window_ms": (
                    display_dispatch.measurement_window_ns / 1_000_000
                ),
                "observed_render_fps": display_dispatch.observed_render_fps,
                "content_timestamp_semantics": (
                    "newer_sync_adjusted_pose_timestamp_to_render_complete"
                ),
                "content_age_frames": display_dispatch.content_age_frames,
                "mean_content_age_ms": display_dispatch.mean_content_age_ms,
                "max_content_age_ms": display_dispatch.max_content_age_ms,
                "p95_content_age_ms": display_dispatch.p95_content_age_ms,
                "content_age_sample_count": (
                    display_dispatch.content_age_sample_count
                ),
                "latency_window_size": display_dispatch.latency_window_size,
                "invalid_content_age_samples": (
                    display_dispatch.invalid_content_age_samples
                ),
                "missing_content_timestamp_frames": (
                    display_dispatch.missing_content_timestamp_frames
                ),
            }
        if avatar_dispatch is not None:
            record["avatar_dispatch"] = {
                "submitted_pairs": avatar_dispatch.submitted_pairs,
                "processed_pairs": avatar_dispatch.processed_pairs,
                "dropped_pairs": avatar_dispatch.dropped_pairs,
                "failed": avatar_dispatch.failed,
                "measurement_window_ms": (
                    avatar_dispatch.measurement_window_ns / 1_000_000
                ),
                "observed_process_fps": avatar_dispatch.observed_process_fps,
                "content_timestamp_semantics": (
                    "newer_sync_adjusted_pose_timestamp_to_avatar_process_complete"
                ),
                "content_age_pairs": avatar_dispatch.content_age_pairs,
                "mean_content_age_ms": avatar_dispatch.mean_content_age_ms,
                "max_content_age_ms": avatar_dispatch.max_content_age_ms,
                "p95_content_age_ms": avatar_dispatch.p95_content_age_ms,
                "content_age_sample_count": (
                    avatar_dispatch.content_age_sample_count
                ),
                "latency_window_size": avatar_dispatch.latency_window_size,
                "invalid_content_age_samples": (
                    avatar_dispatch.invalid_content_age_samples
                ),
                "missing_content_timestamp_pairs": (
                    avatar_dispatch.missing_content_timestamp_pairs
                ),
            }
        if vart is not None:
            record["dpu_runtime"] = {
                "frames": vart.frames,
                "total_ms": vart.total_ns / 1_000_000,
                "mean_ms": vart.mean_ms,
                "max_ms": vart.max_ns / 1_000_000,
                "p95_ms": vart.p95_ms,
                "latency_sample_count": vart.latency_sample_count,
                "latency_window_size": vart.latency_window_size,
            }
        if native_vart is not None:
            record["native_vart"] = dict(native_vart)
        self._write_record(record)

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        if not self._closed:
            self._stream.close()
            self._closed = True

    def _write_record(self, record: object) -> None:
        self._stream.write(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        )
        self._stream.write("\n")

    def __enter__(self) -> CoachSessionReportWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _score_record(score: ExerciseScore) -> dict[str, object]:
    record = {
        "exercise": score.exercise,
        "valid": score.valid,
        "conformance_percent": score.conformance_percent,
        "confidence_percent": score.confidence_percent,
        "coverage_percent": score.coverage_percent,
        "components": [
            {
                "name": component.name,
                "view": component.view.value,
                "observed_value": component.observed_value,
                "unit": component.unit,
                "ideal_min": component.ideal_min,
                "ideal_max": component.ideal_max,
                "tolerance": component.tolerance,
                "score_percent": component.score_percent,
                "confidence_percent": component.confidence_percent,
                "weight": component.weight,
            }
            for component in score.components
        ],
    }
    if score.aggregation is not None:
        record["aggregation"] = asdict(score.aggregation)
        if score.aggregation.side_metric_policy == "legacy_chain_sum_v0":
            record["aggregation"].pop("side_metric_policy")
        if score.aggregation.depth_angle_model is None:
            record["aggregation"].pop("depth_angle_model")
            record["aggregation"].pop("phase_seconds")
            record["aggregation"].pop("depth_angle_invalid_seconds")
    return record


def _cue_record(cue: FeedbackCue) -> dict[str, object]:
    record = {
        "metric": cue.metric, "direction": cue.direction.value,
        "observed_value": cue.observed_value,
        "component_score_percent": cue.component_score_percent,
        "confidence_percent": cue.confidence_percent,
    }
    if cue.sustained_seconds:
        record.update(sustained_seconds=cue.sustained_seconds, phases=list(cue.phases))
    return record
