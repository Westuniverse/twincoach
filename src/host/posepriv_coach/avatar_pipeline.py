"""把双摄 3D 姿态编排成一帧可交给蒙皮后端的计划。

本模块只负责数据边界和确定性的帧级编排：三角测量、骨骼段、矩阵表和
预绑定顶点批次。最终三角形渲染不在这里完成，A53 可以把同一份计划交给
CPU 参考后端、Verilator 传输层或真实 AXI DMA。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Callable, Mapping

from .avatar_skinning import (
    AXIS_BEAT_BYTES,
    MATRIX_COUNT,
    MATRIX_PACKET_BYTES,
    AffineMatrixQ1_14,
    MatrixWrite,
    PackedSkinningOutputBatch,
    SkinningOutputBeat,
    SkinnedPointQ1_14,
    VertexSkinningBackend,
    VertexSkinningRequest,
    unpack_skinned_point,
    compose_affine_q1_14,
)
from .contracts import SyncedPosePair
from .joint_filter import AdaptiveJointFilter
from .rectified_stereo import RectificationCalibration
from .rectified_backend import RectifiedStereoBackend
from .skeleton_pose import (
    DEFAULT_BONE_LINKS,
    BoneLink,
    BoneSegment3D,
    SkeletonJoint3D,
    bone_affine_matrix_q1_14,
    build_bone_segments,
    build_virtual_joints,
    stable_bone_local_x_axis,
)
from .triangulation import (
    CameraExtrinsics,
    CameraIntrinsics,
    TriangulatedPoint,
    triangulate_rectified_synced_pose_pair,
    triangulate_synced_pose_pair,
)


MatrixProgrammer = Callable[[tuple[MatrixWrite, ...]], None]
TRIANGULATION_BACKENDS = ("general", "rectified-reference")


@dataclass(frozen=True, slots=True)
class AvatarPipelineConfig:
    """固定相机标定和骨骼策略；运行中只替换标定配置，不改数据布局。"""

    front_intrinsics: CameraIntrinsics
    side_intrinsics: CameraIntrinsics
    front_extrinsics: CameraExtrinsics
    side_extrinsics: CameraExtrinsics
    keypoint_names: tuple[str, ...] | None = None
    bone_links: tuple[BoneLink, ...] = DEFAULT_BONE_LINKS
    minimum_confidence: float = 0.35
    maximum_ray_gap: float = 0.15
    minimum_ray_angle_degrees: float = 1.0
    minimum_bone_length: float = 1.0e-6
    world_to_model_scale: float = 1.0
    triangulation_backend: str = "general"
    rectified_baseline: float | None = None
    rectified_focal: float | None = None
    rectified_minimum_disparity: float = 64.0 / (1 << 14)
    rectified_maximum_vertical_disparity: float = 0.02
    rectification: RectificationCalibration | None = None
    rectified_backend: RectifiedStereoBackend | None = None

    def __post_init__(self) -> None:
        if self.keypoint_names is not None:
            if not self.keypoint_names or len(set(self.keypoint_names)) != len(self.keypoint_names):
                raise ValueError("keypoint_names must be non-empty and unique")
            if any(not name for name in self.keypoint_names):
                raise ValueError("keypoint_names must not contain empty names")
        if len(self.bone_links) > MATRIX_COUNT:
            raise ValueError("the configured bone topology exceeds the matrix bank")
        if len({link.name for link in self.bone_links}) != len(self.bone_links):
            raise ValueError("bone link names must be unique")
        if not 0.0 <= self.minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence must be within [0, 1]")
        if not math.isfinite(self.maximum_ray_gap) or self.maximum_ray_gap < 0.0:
            raise ValueError("maximum_ray_gap must be finite and non-negative")
        if (
            not math.isfinite(self.minimum_ray_angle_degrees)
            or not 0.0 <= self.minimum_ray_angle_degrees <= 90.0
        ):
            raise ValueError("minimum_ray_angle_degrees must be within [0, 90]")
        if self.minimum_bone_length < 0.0:
            raise ValueError("minimum_bone_length must be non-negative")
        if not math.isfinite(self.world_to_model_scale) or self.world_to_model_scale <= 0.0:
            raise ValueError("world_to_model_scale must be finite and positive")
        if self.triangulation_backend not in TRIANGULATION_BACKENDS:
            raise ValueError(
                f"unsupported triangulation backend: {self.triangulation_backend}"
            )
        if self.triangulation_backend == "rectified-reference":
            if self.rectified_baseline is None or self.rectified_focal is None:
                raise ValueError(
                    "rectified-reference requires rectified_baseline and rectified_focal"
                )
            if not math.isfinite(self.rectified_baseline) or self.rectified_baseline <= 0.0:
                raise ValueError("rectified_baseline must be finite and positive")
            if not math.isfinite(self.rectified_focal) or self.rectified_focal <= 0.0:
                raise ValueError("rectified_focal must be finite and positive")
        if (
            not math.isfinite(self.rectified_minimum_disparity)
            or self.rectified_minimum_disparity <= 0.0
        ):
            raise ValueError("rectified_minimum_disparity must be positive")
        if (
            not math.isfinite(self.rectified_maximum_vertical_disparity)
            or self.rectified_maximum_vertical_disparity < 0.0
        ):
            raise ValueError("rectified_maximum_vertical_disparity must be non-negative")


@dataclass(frozen=True, slots=True)
class AvatarFramePlan:
    """一帧 3D 姿态和蒙皮 DMA 所需的全部确定性输入。"""

    pair: SyncedPosePair
    points: Mapping[str, TriangulatedPoint]
    joints: Mapping[str, SkeletonJoint3D]
    segments: tuple[BoneSegment3D, ...]
    matrix_writes: tuple[MatrixWrite, ...]
    matrix_valid: tuple[bool, ...]
    vertex_requests: tuple[VertexSkinningRequest, ...]
    model_origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    model_scale: float = 1.0
    pose_preview: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", MappingProxyType(dict(self.points)))
        object.__setattr__(self, "joints", MappingProxyType(dict(self.joints)))
        if len(self.matrix_writes) != MATRIX_COUNT or len(self.matrix_valid) != MATRIX_COUNT:
            raise ValueError("an avatar frame must contain exactly 16 matrix entries")
        if not math.isfinite(self.model_scale) or self.model_scale <= 0.0:
            raise ValueError("avatar frame model_scale must be finite and positive")
        expected_indices = tuple(range(MATRIX_COUNT))
        if tuple(write.index for write in self.matrix_writes) != expected_indices:
            raise ValueError("matrix writes must be ordered by matrix-bank index")

    def matrix_bank(self) -> tuple[AffineMatrixQ1_14, ...]:
        """Return the exact matrix bank consumed by the CPU reference backend."""

        return tuple(write.matrix for write in self.matrix_writes)

    @property
    def packet_tx_bytes(self) -> int:
        """Return the fixed packet input size for this vertex batch."""

        return MATRIX_COUNT * MATRIX_PACKET_BYTES + len(self.vertex_requests) * AXIS_BEAT_BYTES

    @property
    def packet_rx_bytes(self) -> int:
        """Return the padded output size expected from the packet wrapper."""

        return len(self.vertex_requests) * AXIS_BEAT_BYTES

    def execute(
        self,
        backend: VertexSkinningBackend,
        *,
        program_matrices: MatrixProgrammer | None = None,
    ) -> Sequence[SkinningOutputBeat]:
        """Program this frame and exchange its bound vertices through one backend.

        `program_matrices` is deliberately a small callback instead of a second
        transport class. A53 can bind it to an AXI-Lite/AXI-Stream writer later;
        reference and Verilator paths can leave it unset or record the writes.
        """

        if program_matrices is not None:
            program_matrices(self.matrix_writes)
        outputs = backend.exchange(self.vertex_requests)
        if len(outputs) != len(self.vertex_requests):
            raise ValueError("skinning backend returned an unexpected beat count")
        return outputs


@dataclass(frozen=True, slots=True)
class AvatarFrameResult:
    """一帧计划加上后端输出，便于显示层或渲染层继续消费。"""

    plan: AvatarFramePlan
    output_beats: Sequence[SkinningOutputBeat]

    @property
    def skinned_points(self) -> tuple[SkinnedPointQ1_14, ...]:
        if isinstance(self.output_beats, PackedSkinningOutputBatch):
            return self.output_beats.unpack_points()
        return tuple(unpack_skinned_point(beat.data) for beat in self.output_beats)


class AvatarFramePlanner:
    """把一个同步姿态对转换成 3D 骨架和矩阵驻留帧。"""

    def __init__(
        self,
        config: AvatarPipelineConfig,
        *,
        joint_filter: AdaptiveJointFilter | None = None,
    ) -> None:
        self.config = config
        self.joint_filter = joint_filter
        self._bone_local_x_axes: list[tuple[float, float, float] | None] = [
            None
        ] * MATRIX_COUNT

    def build(
        self,
        pair: SyncedPosePair,
        *,
        vertex_requests: tuple[VertexSkinningRequest, ...] = (),
        bone_bind_lengths_q1_14: tuple[int, ...] = (),
        inverse_bind_matrices_q1_14: tuple[AffineMatrixQ1_14, ...] = (),
        bone_topology_indices: tuple[int, ...] = (),
    ) -> AvatarFramePlan:
        """生成一帧计划；顶点请求来自预绑定网格的只读资产。"""

        _validate_calibrated_frame_size(
            pair.front.source_width,
            pair.front.source_height,
            self.config.front_intrinsics,
            "front",
        )
        _validate_calibrated_frame_size(
            pair.side.source_width,
            pair.side.source_height,
            self.config.side_intrinsics,
            "side",
        )
        if self.config.triangulation_backend == "rectified-reference":
            points = triangulate_rectified_synced_pose_pair(
                pair,
                self.config.front_intrinsics,
                self.config.side_intrinsics,
                self.config.front_extrinsics,
                self.config.side_extrinsics,
                baseline=self.config.rectified_baseline,
                focal=self.config.rectified_focal,
                keypoint_names=self.config.keypoint_names,
                minimum_confidence=self.config.minimum_confidence,
                minimum_disparity=self.config.rectified_minimum_disparity,
                maximum_vertical_disparity=self.config.rectified_maximum_vertical_disparity,
                rectification=self.config.rectification,
                backend=self.config.rectified_backend,
            )
        else:
            points = triangulate_synced_pose_pair(
                pair,
                self.config.front_intrinsics,
                self.config.side_intrinsics,
                self.config.front_extrinsics,
                self.config.side_extrinsics,
                keypoint_names=self.config.keypoint_names,
                minimum_confidence=self.config.minimum_confidence,
                maximum_ray_gap=self.config.maximum_ray_gap,
                minimum_ray_angle_degrees=self.config.minimum_ray_angle_degrees,
            )
        joints = build_virtual_joints(points)
        if self.joint_filter is not None:
            pair_timestamp_ns = (
                pair.front.timestamp_ns + pair.side.timestamp_ns
            ) // 2
            joints = self.joint_filter.update(
                joints,
                timestamp_ns=pair_timestamp_ns,
            )
        segments = build_bone_segments(
            joints,
            self.config.bone_links,
            minimum_length=self.config.minimum_bone_length,
        )
        model_origin = _select_model_origin(joints)
        matrices = [AffineMatrixQ1_14.identity() for _ in range(MATRIX_COUNT)]
        matrix_valid = [False] * MATRIX_COUNT
        if bone_topology_indices:
            if len(bone_topology_indices) > MATRIX_COUNT:
                raise ValueError("bone topology mapping exceeds the matrix bank")
            if len(set(bone_topology_indices)) != len(bone_topology_indices) or any(
                index < 0 or index >= len(segments) for index in bone_topology_indices
            ):
                raise ValueError("bone topology mapping must contain unique valid indices")
            matrix_segments = tuple(
                (asset_index, segments[topology_index])
                for asset_index, topology_index in enumerate(bone_topology_indices)
            )
        else:
            matrix_segments = tuple(enumerate(segments))
        for index, segment in matrix_segments:
            local_x_reference = self._bone_local_x_axes[index]
            if local_x_reference is None:
                local_x_reference = (
                    (0.0, 0.0, 1.0)
                    if "arm" in segment.name
                    else (1.0, 0.0, 0.0)
                )
            stable_local_x = stable_bone_local_x_axis(segment, local_x_reference)
            matrix = bone_affine_matrix_q1_14(
                segment,
                world_to_model_scale=self.config.world_to_model_scale,
                local_bone_length_q1_14=(
                    bone_bind_lengths_q1_14[index]
                    if index < len(bone_bind_lengths_q1_14)
                    else None
                ),
                world_origin=model_origin,
                local_x_reference=stable_local_x,
            )
            if matrix is not None:
                self._bone_local_x_axes[index] = stable_local_x
                if index < len(inverse_bind_matrices_q1_14):
                    matrix = compose_affine_q1_14(
                        matrix,
                        inverse_bind_matrices_q1_14[index],
                    )
                matrices[index] = matrix
                matrix_valid[index] = True
        writes = tuple(MatrixWrite(index, matrix) for index, matrix in enumerate(matrices))
        return AvatarFramePlan(
            pair=pair,
            points=points,
            joints=joints,
            segments=segments,
            matrix_writes=writes,
            matrix_valid=tuple(matrix_valid),
            vertex_requests=tuple(vertex_requests),
            model_origin=model_origin,
            model_scale=self.config.world_to_model_scale,
        )
    def process(
        self,
        pair: SyncedPosePair,
        backend: VertexSkinningBackend,
        *,
        vertex_requests: tuple[VertexSkinningRequest, ...] = (),
        bone_bind_lengths_q1_14: tuple[int, ...] = (),
        inverse_bind_matrices_q1_14: tuple[AffineMatrixQ1_14, ...] = (),
        bone_topology_indices: tuple[int, ...] = (),
        program_matrices: MatrixProgrammer | None = None,
    ) -> AvatarFrameResult:
        """生成并执行一帧；输出仍保持 AXI beat 顺序和 TLAST 语义。"""

        plan = self.build(
            pair,
            vertex_requests=vertex_requests,
            bone_bind_lengths_q1_14=bone_bind_lengths_q1_14,
            inverse_bind_matrices_q1_14=inverse_bind_matrices_q1_14,
            bone_topology_indices=bone_topology_indices,
        )
        return AvatarFrameResult(
            plan=plan,
            output_beats=plan.execute(backend, program_matrices=program_matrices),
        )


def _validate_calibrated_frame_size(
    width: int,
    height: int,
    intrinsics: CameraIntrinsics,
    view: str,
) -> None:
    """拒绝把另一种分辨率的关键点套入固定像素内参。"""

    # 1×1 是早期纯归一化 PoseFrame 的“尺寸未知”哨兵，仅为旧回放兼容。
    if (width, height) == (1, 1):
        return
    expected = (intrinsics.width, intrinsics.height)
    if (width, height) != expected:
        raise ValueError(
            f"{view} pose size {width}x{height} does not match calibration "
            f"{expected[0]}x{expected[1]}"
        )


__all__ = [
    "AvatarFramePlan",
    "AvatarFrameResult",
    "AvatarFramePlanner",
    "AvatarPipelineConfig",
    "MatrixProgrammer",
    "TRIANGULATION_BACKENDS",
]


def _select_model_origin(
    joints: Mapping[str, SkeletonJoint3D],
) -> tuple[float, float, float]:
    """Choose a stable person-relative origin so camera distance never enters Q1.14."""

    for name in ("pelvis", "chest"):
        joint = joints.get(name)
        if joint is not None and joint.valid:
            return joint.position
    valid_positions = tuple(joint.position for joint in joints.values() if joint.valid)
    if not valid_positions:
        return (0.0, 0.0, 0.0)
    return tuple(
        sum(position[index] for position in valid_positions) / len(valid_positions)
        for index in range(3)
    )  # type: ignore[return-value]
