"""由同一组三维关节生成几何一致的双视角确定性动作。"""

from __future__ import annotations

from math import cos, pi

from .avatar_calibration import AvatarCalibration
from .contracts import CameraView, PoseFrame
from .triangulation import project_world_keypoint


def build_projected_squat_pair(
    frame_index: int,
    timestamp_ns: int,
    calibration: AvatarCalibration,
    *,
    pair_fps: float = 30.0,
    cycle_seconds: float = 4.0,
    side_skew_ns: int = 7_000_000,
) -> tuple[PoseFrame, PoseFrame]:
    """生成一个共享 3D 真值的 MoveNet 17 点深蹲同步对。

    该样例只用于检查投影、三角测量和下游数字人数据链，不代表真实人体动作分布。
    世界坐标以正面相机前方约 3 米为中心，Y 轴向下以匹配图像坐标。
    """

    if frame_index < 0 or timestamp_ns < 0:
        raise ValueError("frame index and timestamp must be non-negative")
    if pair_fps <= 0.0 or cycle_seconds <= 0.0:
        raise ValueError("pair_fps and cycle_seconds must be positive")
    if side_skew_ns < 0:
        raise ValueError("side_skew_ns must be non-negative")
    frames_per_cycle = max(4, round(pair_fps * cycle_seconds))
    phase = (frame_index % frames_per_cycle) / frames_per_cycle
    depth = (1.0 - cos(2.0 * pi * phase)) * 0.5
    confidence = 0.97
    points = _squat_world_points(depth)
    front_keypoints = {
        name: project_world_keypoint(
            point,
            calibration.front_intrinsics,
            calibration.front_extrinsics,
            confidence=confidence,
        )
        for name, point in points.items()
    }
    side_keypoints = {
        name: project_world_keypoint(
            point,
            calibration.side_intrinsics,
            calibration.side_extrinsics,
            confidence=confidence,
        )
        for name, point in points.items()
    }
    return (
        PoseFrame(
            CameraView.FRONT,
            frame_index,
            timestamp_ns,
            front_keypoints,
            calibration.front_intrinsics.width,
            calibration.front_intrinsics.height,
        ),
        PoseFrame(
            CameraView.SIDE,
            frame_index,
            timestamp_ns + side_skew_ns,
            side_keypoints,
            calibration.side_intrinsics.width,
            calibration.side_intrinsics.height,
        ),
    )


def _squat_world_points(depth: float) -> dict[str, tuple[float, float, float]]:
    torso_drop = 0.35 * depth
    hip_z = 3.0 - 0.25 * depth
    knee_z = 3.0 + 0.28 * depth
    return {
        "nose": (0.0, -0.78 + torso_drop, 3.0 - 0.08 * depth),
        "left_eye": (-0.045, -0.82 + torso_drop, 3.0 - 0.07 * depth),
        "right_eye": (0.045, -0.82 + torso_drop, 3.0 - 0.07 * depth),
        "left_ear": (-0.11, -0.77 + torso_drop, 3.0),
        "right_ear": (0.11, -0.77 + torso_drop, 3.0),
        "left_shoulder": (-0.30, -0.48 + torso_drop, 2.98),
        "right_shoulder": (0.30, -0.48 + torso_drop, 3.02),
        "left_elbow": (-0.43, -0.12 + torso_drop, 3.00),
        "right_elbow": (0.43, -0.12 + torso_drop, 3.04),
        "left_wrist": (-0.48, 0.22 + torso_drop, 3.02),
        "right_wrist": (0.48, 0.22 + torso_drop, 3.06),
        "left_hip": (-0.20, 0.10 + torso_drop, hip_z),
        "right_hip": (0.20, 0.10 + torso_drop, hip_z + 0.03),
        "left_knee": (-0.22, 0.65 + 0.15 * depth, knee_z),
        "right_knee": (0.22, 0.65 + 0.15 * depth, knee_z + 0.03),
        "left_ankle": (-0.23, 0.80, 3.0),
        "right_ankle": (0.23, 0.80, 3.03),
    }
