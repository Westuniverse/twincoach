"""Deterministic skeleton-only input for checking the display path."""

from __future__ import annotations

from dataclasses import dataclass
from math import cos, pi

from .avatar_pipeline import AvatarFramePlan
from .avatar_skinning import (
    MATRIX_COUNT,
    AffineMatrixQ1_14,
    MatrixWrite,
    compose_affine_q1_14,
)
from .contracts import CameraView, Keypoint, PoseFrame, ScoreComponent, SyncedPosePair
from .display import CoachDisplayState
from .exercises.squat_tracker import SquatPhase
from .skeleton_pose import bone_affine_matrix_q1_14, build_bone_segments, build_virtual_joints
from .triangulation import TriangulatedPoint
from .mesh_asset import AvatarMeshAsset


_PHASE_ZH = {
    SquatPhase.STANDING: "站立",
    SquatPhase.DESCENDING: "下蹲",
    SquatPhase.BOTTOM: "最低点",
    SquatPhase.ASCENDING: "起身",
}


@dataclass(frozen=True, slots=True)
class SyntheticDisplaySample:
    front: PoseFrame
    side: PoseFrame
    state: CoachDisplayState


def build_synthetic_display_sample(
    frame_index: int,
    timestamp_ns: int,
    *,
    pair_fps: float = 30.0,
    cycle_seconds: float = 4.0,
) -> SyntheticDisplaySample:
    """Build one animated squat pair without camera pixels or a pose model."""

    if frame_index < 0:
        raise ValueError("frame_index must be non-negative")
    if timestamp_ns < 0:
        raise ValueError("timestamp_ns must be non-negative")
    if pair_fps <= 0.0:
        raise ValueError("pair_fps must be positive")
    if cycle_seconds <= 0.0:
        raise ValueError("cycle_seconds must be positive")

    frames_per_cycle = max(4, round(pair_fps * cycle_seconds))
    cycle_position = (frame_index % frames_per_cycle) / frames_per_cycle
    depth = (1.0 - cos(2.0 * pi * cycle_position)) * 0.5
    phase = _squat_phase(cycle_position)
    confidence = 0.97

    front = PoseFrame(
        view=CameraView.FRONT,
        frame_id=frame_index,
        timestamp_ns=timestamp_ns,
        keypoints={
            "left_shoulder": Keypoint(0.40, 0.22 + 0.08 * depth, confidence),
            "right_shoulder": Keypoint(0.60, 0.22 + 0.08 * depth, confidence),
            "left_elbow": Keypoint(0.34, 0.38 + 0.06 * depth, confidence),
            "right_elbow": Keypoint(0.66, 0.38 + 0.06 * depth, confidence),
            "left_wrist": Keypoint(0.31, 0.53 + 0.04 * depth, confidence),
            "right_wrist": Keypoint(0.69, 0.53 + 0.04 * depth, confidence),
            "left_hip": Keypoint(0.44, 0.49 + 0.10 * depth, confidence),
            "right_hip": Keypoint(0.56, 0.49 + 0.10 * depth, confidence),
            "left_knee": Keypoint(0.43 - 0.06 * depth, 0.69 + 0.03 * depth, confidence),
            "right_knee": Keypoint(0.57 + 0.06 * depth, 0.69 + 0.03 * depth, confidence),
            "left_ankle": Keypoint(0.40, 0.92, confidence),
            "right_ankle": Keypoint(0.60, 0.92, confidence),
        },
    )
    side = PoseFrame(
        view=CameraView.SIDE,
        frame_id=frame_index,
        timestamp_ns=timestamp_ns + 7_000_000,
        keypoints={
            "left_shoulder": Keypoint(0.48 - 0.07 * depth, 0.22 + 0.10 * depth, confidence),
            "right_shoulder": Keypoint(0.50 - 0.07 * depth, 0.22 + 0.10 * depth, confidence),
            "left_elbow": Keypoint(0.42 - 0.03 * depth, 0.39 + 0.08 * depth, confidence),
            "right_elbow": Keypoint(0.44 - 0.03 * depth, 0.39 + 0.08 * depth, confidence),
            "left_wrist": Keypoint(0.39, 0.54 + 0.05 * depth, confidence),
            "right_wrist": Keypoint(0.41, 0.54 + 0.05 * depth, confidence),
            "left_hip": Keypoint(0.51, 0.49 + 0.10 * depth, confidence),
            "right_hip": Keypoint(0.53, 0.49 + 0.10 * depth, confidence),
            "left_knee": Keypoint(0.56 + 0.10 * depth, 0.69 + 0.03 * depth, confidence),
            "right_knee": Keypoint(0.58 + 0.10 * depth, 0.69 + 0.03 * depth, confidence),
            "left_ankle": Keypoint(0.52, 0.92, confidence),
            "right_ankle": Keypoint(0.54, 0.92, confidence),
        },
    )
    score = 94.0 - 6.0 * depth
    state = CoachDisplayState(
        phase=phase,
        phase_zh=_PHASE_ZH[phase],
        repetition_count=frame_index // frames_per_cycle,
        score_valid=True,
        conformance_percent=score,
        confidence_percent=confidence * 100.0,
        coverage_percent=100.0,
        feedback_zh="合成动作：保持膝盖与脚尖方向一致",
        processing_fps=pair_fps * 2.0,
        front_fps=pair_fps,
        side_fps=pair_fps,
        synchronized_pair_fps=pair_fps,
        current_skew_ms=7.0,
        p95_skew_ms=7.0,
        capture_dropped_front=0,
        capture_dropped_side=0,
        sync_dropped_front=0,
        sync_dropped_side=0,
        status_zh="合成显示探针，不代表模型或板端性能",
        mean_end_to_end_ms=18.0,
        p95_end_to_end_ms=24.0,
        latency_sample_count=min(frame_index + 1, 1024),
        latency_window_size=1024,
        score_components=(
            ScoreComponent(
                name="side_depth_ratio",
                view=CameraView.SIDE,
                observed_value=0.98 + 0.12 * depth,
                unit="ratio",
                ideal_min=0.9,
                ideal_max=1.1,
                tolerance=0.3,
                score_percent=score,
                confidence_percent=97.0,
                weight=0.3,
            ),
            ScoreComponent(
                name="side_trunk_angle_deg",
                view=CameraView.SIDE,
                observed_value=18.0 + 8.0 * depth,
                unit="deg",
                ideal_min=10.0,
                ideal_max=35.0,
                tolerance=25.0,
                score_percent=96.0,
                confidence_percent=97.0,
                weight=0.25,
            ),
            ScoreComponent(
                name="front_knee_symmetry",
                view=CameraView.FRONT,
                observed_value=0.96,
                unit="ratio",
                ideal_min=0.9,
                ideal_max=1.1,
                tolerance=0.25,
                score_percent=92.0,
                confidence_percent=97.0,
                weight=0.25,
            ),
            ScoreComponent(
                name="tempo_seconds",
                view=CameraView.SIDE,
                observed_value=cycle_seconds,
                unit="s",
                ideal_min=2.0,
                ideal_max=4.0,
                tolerance=2.0,
                score_percent=100.0,
                confidence_percent=100.0,
                weight=0.2,
            ),
        ),
    )
    return SyntheticDisplaySample(front=front, side=side, state=state)


def _squat_phase(cycle_position: float) -> SquatPhase:
    if cycle_position < 0.10 or cycle_position >= 0.90:
        return SquatPhase.STANDING
    if cycle_position < 0.45:
        return SquatPhase.DESCENDING
    if cycle_position < 0.55:
        return SquatPhase.BOTTOM
    return SquatPhase.ASCENDING


def build_synthetic_avatar_plan(
    sample: SyntheticDisplaySample,
    mesh_asset: AvatarMeshAsset | None = None,
) -> AvatarFramePlan:
    """将合成双视角关键点变成可直接投影的 3D 骨架计划。

    这是显示探针专用的几何输入，不代表相机标定或模型精度；真实运行时应
    使用 ``AvatarFramePlanner`` 的三角测量结果。
    """

    names = tuple(name for name in sample.front.keypoints if name in sample.side.keypoints)
    points: dict[str, TriangulatedPoint] = {}
    for name in names:
        front = sample.front.keypoints[name]
        side = sample.side.keypoints[name]
        points[name] = TriangulatedPoint(
            x=(front.x - 0.5) * 2.0,
            y=(0.5 - front.y) * 2.2,
            z=(side.x - 0.5) * 2.0,
            confidence=min(front.confidence, side.confidence),
            ray_gap=0.01,
            valid=True,
            ray_angle_degrees=25.0,
        )
    chest = _synthetic_point(points, "left_shoulder", "right_shoulder", y_offset=0.0)
    pelvis = _synthetic_point(points, "left_hip", "right_hip", y_offset=0.0)
    points["chest"] = chest
    points["pelvis"] = pelvis
    points["neck"] = TriangulatedPoint(
        chest.x,
        chest.y + 0.26,
        chest.z,
        chest.confidence,
        chest.ray_gap,
        True,
        chest.ray_angle_degrees,
    )
    points["nose"] = TriangulatedPoint(
        chest.x,
        chest.y + 0.48,
        chest.z,
        chest.confidence,
        chest.ray_gap,
        True,
        chest.ray_angle_degrees,
    )
    joints = build_virtual_joints(points)
    segments = build_bone_segments(joints)
    matrix_segments = tuple(enumerate(segments[:MATRIX_COUNT]))
    if mesh_asset is not None:
        segment_names = tuple(segment.name for segment in segments)
        segment_indices = {name: index for index, name in enumerate(segment_names)}
        unknown = tuple(name for name in mesh_asset.bone_names if name not in segment_indices)
        if unknown:
            raise ValueError(
                "mesh contains bones outside the synthetic topology: "
                + ", ".join(unknown)
            )
        matrix_segments = tuple(
            (asset_index, segments[segment_indices[name]])
            for asset_index, name in enumerate(mesh_asset.bone_names)
        )
    model_origin = joints["pelvis"].position
    matrices = [AffineMatrixQ1_14.identity() for _ in range(MATRIX_COUNT)]
    valid = [False] * MATRIX_COUNT
    for index, segment in matrix_segments:
        matrix = bone_affine_matrix_q1_14(
            segment,
            local_bone_length_q1_14=(
                mesh_asset.bone_bind_lengths_q1_14[index]
                if mesh_asset is not None
                and index < len(mesh_asset.bone_bind_lengths_q1_14)
                else None
            ),
            world_origin=model_origin,
        )
        if matrix is not None:
            if (
                mesh_asset is not None
                and index < len(mesh_asset.inverse_bind_matrices_q1_14)
            ):
                matrix = compose_affine_q1_14(
                    matrix,
                    mesh_asset.inverse_bind_matrices_q1_14[index],
                )
            matrices[index] = matrix
            valid[index] = True
    pair = SyncedPosePair(
        sample.front,
        sample.side,
        abs(sample.front.timestamp_ns - sample.side.timestamp_ns),
    )
    return AvatarFramePlan(
        pair=pair,
        points=points,
        joints=joints,
        segments=segments,
        matrix_writes=tuple(MatrixWrite(index, matrix) for index, matrix in enumerate(matrices)),
        matrix_valid=tuple(valid),
        vertex_requests=() if mesh_asset is None else mesh_asset.skinning_requests(),
        model_origin=model_origin,
    )


def _synthetic_point(
    points: dict[str, TriangulatedPoint],
    first_name: str,
    second_name: str,
    *,
    y_offset: float,
) -> TriangulatedPoint:
    first = points[first_name]
    second = points[second_name]
    return TriangulatedPoint(
        x=(first.x + second.x) * 0.5,
        y=(first.y + second.y) * 0.5 + y_offset,
        z=(first.z + second.z) * 0.5,
        confidence=min(first.confidence, second.confidence),
        ray_gap=max(first.ray_gap, second.ray_gap),
        valid=first.valid and second.valid,
        ray_angle_degrees=min(
            first.ray_angle_degrees,
            second.ray_angle_degrees,
        ),
    )
