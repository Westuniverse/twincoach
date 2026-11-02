"""由三维关键点生成预绑定网格可消费的骨骼段参考数据。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping, Sequence

from .avatar_skinning import AffineMatrixQ1_14
from .triangulation import TriangulatedPoint


Vector3 = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class SkeletonJoint3D:
    """可用于驱动骨骼的单个关节。"""

    name: str
    position: Vector3
    confidence: float
    ray_gap: float
    valid: bool

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("skeleton joint name must not be empty")
        if len(self.position) != 3 or not all(math.isfinite(value) for value in self.position):
            raise ValueError("skeleton joint position must be finite")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("skeleton joint confidence must be within [0, 1]")
        if self.ray_gap < 0.0 or not (math.isfinite(self.ray_gap) or math.isinf(self.ray_gap)):
            raise ValueError("skeleton joint ray gap must be non-negative")


@dataclass(frozen=True, slots=True)
class BoneLink:
    """一条从父关节指向子关节的骨骼连接。"""

    name: str
    parent: str
    child: str

    def __post_init__(self) -> None:
        if not self.name or not self.parent or not self.child:
            raise ValueError("bone name and joint names must not be empty")
        if self.parent == self.child:
            raise ValueError("bone parent and child must be different")


@dataclass(frozen=True, slots=True)
class BoneSegment3D:
    """一条可交给骨骼姿态/蒙皮层的有效或无效骨骼段。"""

    name: str
    parent: str
    child: str
    start: Vector3
    end: Vector3
    length: float
    confidence: float
    valid: bool


DEFAULT_BONE_LINKS: tuple[BoneLink, ...] = (
    BoneLink("spine", "pelvis", "chest"),
    BoneLink("neck", "chest", "neck"),
    BoneLink("head", "neck", "nose"),
    BoneLink("left_upper_arm", "left_shoulder", "left_elbow"),
    BoneLink("left_lower_arm", "left_elbow", "left_wrist"),
    BoneLink("right_upper_arm", "right_shoulder", "right_elbow"),
    BoneLink("right_lower_arm", "right_elbow", "right_wrist"),
    BoneLink("left_upper_leg", "left_hip", "left_knee"),
    BoneLink("left_lower_leg", "left_knee", "left_ankle"),
    BoneLink("right_upper_leg", "right_hip", "right_knee"),
    BoneLink("right_lower_leg", "right_knee", "right_ankle"),
)

# MoveNet 没有胸口和颈根关键点。肩中心作为颈根，胸点位于颈根朝骨盆 30% 的位置，
# 使 spine/neck/head 三段在真实 17 点输入下都有非零长度。
CHEST_FRACTION_TOWARD_PELVIS = 0.30


def build_virtual_joints(
    points: Mapping[str, TriangulatedPoint | SkeletonJoint3D],
) -> dict[str, SkeletonJoint3D]:
    """把左右肩/髋补成网格驱动需要的胸口和骨盆关节。"""

    result = {
        name: point if isinstance(point, SkeletonJoint3D) else SkeletonJoint3D(
            name=name,
            position=(point.x, point.y, point.z),
            confidence=point.confidence,
            ray_gap=point.ray_gap,
            valid=point.valid,
        )
        for name, point in points.items()
    }
    _add_midpoint(result, result, "neck", "left_shoulder", "right_shoulder")
    _add_midpoint(result, result, "pelvis", "left_hip", "right_hip")
    _add_interpolated_joint(
        result,
        "chest",
        "neck",
        "pelvis",
        CHEST_FRACTION_TOWARD_PELVIS,
    )
    return result


def build_bone_segments(
    joints: Mapping[str, SkeletonJoint3D],
    links: Sequence[BoneLink] = DEFAULT_BONE_LINKS,
    *,
    minimum_length: float = 1.0e-6,
) -> tuple[BoneSegment3D, ...]:
    """按固定骨骼拓扑生成骨段，不为缺失或无效关节伪造位置。"""

    if not math.isfinite(minimum_length) or minimum_length < 0.0:
        raise ValueError("minimum_length must be finite and non-negative")
    result: list[BoneSegment3D] = []
    for link in links:
        parent = joints.get(link.parent)
        child = joints.get(link.child)
        if parent is None or child is None:
            result.append(_invalid_segment(link))
            continue
        length = _distance(parent.position, child.position)
        valid = parent.valid and child.valid and length >= minimum_length
        result.append(
            BoneSegment3D(
                name=link.name,
                parent=link.parent,
                child=link.child,
                start=parent.position,
                end=child.position,
                length=length,
                confidence=min(parent.confidence, child.confidence) if valid else 0.0,
                valid=valid,
            )
        )
    return tuple(result)


def bone_affine_matrix_q1_14(
    segment: BoneSegment3D,
    *,
    world_to_model_scale: float = 1.0,
    local_bone_length_q1_14: int | None = None,
    world_origin: Vector3 = (0.0, 0.0, 0.0),
    local_x_reference: Vector3 | None = None,
) -> AffineMatrixQ1_14 | None:
    """将骨段的起点和方向转换为蒙皮使用的 3×4 Q1.14 矩阵。

    预绑定模型约定每根骨骼的局部 +Y 轴沿骨骼方向；提供绑定长度时，局部
    +Y 端点会缩放到当前骨段终点。无效骨段返回 None；
    平移超出 Q1.14 可表达范围时直接报错，避免静默饱和导致人体跳变。
    """

    if not math.isfinite(world_to_model_scale) or world_to_model_scale <= 0.0:
        raise ValueError("world_to_model_scale must be finite and positive")
    if len(world_origin) != 3 or not all(math.isfinite(value) for value in world_origin):
        raise ValueError("world_origin must contain three finite coordinates")
    if not segment.valid:
        return None
    direction = _normalize(_sub(segment.end, segment.start))
    rotation = list(
        _rotation_from_positive_y(direction)
        if local_x_reference is None
        else _rotation_from_positive_y_with_reference(direction, local_x_reference)
    )
    # X/Z describe cross-section thickness and follow the global model scale.
    for index in (0, 3, 6, 2, 5, 8):
        rotation[index] *= world_to_model_scale
    if local_bone_length_q1_14 is not None:
        if not 0 < local_bone_length_q1_14 <= (1 << 15) - 1:
            raise ValueError("local bone length must be positive signed Q1.14")
        local_length = local_bone_length_q1_14 / (1 << 14)
        length_scale = segment.length * world_to_model_scale / local_length
        # Scale only the local +Y column. Limb thickness remains stable while
        # the capsule endpoint follows the measured child joint.
        for index in (1, 4, 7):
            rotation[index] *= length_scale
    else:
        for index in (1, 4, 7):
            rotation[index] *= world_to_model_scale
    translation = (
        (segment.start[0] - world_origin[0]) * world_to_model_scale,
        (segment.start[1] - world_origin[1]) * world_to_model_scale,
        (segment.start[2] - world_origin[2]) * world_to_model_scale,
    )
    # AffineMatrixQ1_14 and the RTL both use row-major 3x4 storage.  Keep the
    # translation in columns 3/7/11 instead of appending a separate vector.
    values_float = (
        rotation[0],
        rotation[1],
        rotation[2],
        translation[0],
        rotation[3],
        rotation[4],
        rotation[5],
        translation[1],
        rotation[6],
        rotation[7],
        rotation[8],
        translation[2],
    )
    values = (
        _quantize_q1_14(values_float[0]),
        _quantize_q1_14(values_float[1]),
        _quantize_q1_14(values_float[2]),
        _quantize_q1_14(values_float[3]),
        _quantize_q1_14(values_float[4]),
        _quantize_q1_14(values_float[5]),
        _quantize_q1_14(values_float[6]),
        _quantize_q1_14(values_float[7]),
        _quantize_q1_14(values_float[8]),
        _quantize_q1_14(values_float[9]),
        _quantize_q1_14(values_float[10]),
        _quantize_q1_14(values_float[11]),
    )
    return AffineMatrixQ1_14(values)


def stable_bone_local_x_axis(
    segment: BoneSegment3D,
    reference: Vector3,
) -> Vector3 | None:
    """把上一帧横轴投影到新骨轴法平面，避免反向骨骼发生 180°翻转。"""

    if not segment.valid:
        return None
    direction = _normalize(_sub(segment.end, segment.start))
    return _project_reference_axis(direction, reference)


def _add_midpoint(
    result: dict[str, SkeletonJoint3D],
    points: Mapping[str, SkeletonJoint3D],
    name: str,
    first_name: str,
    second_name: str,
) -> None:
    first = points.get(first_name)
    second = points.get(second_name)
    if first is None or second is None:
        return
    result[name] = SkeletonJoint3D(
        name=name,
        position=(
            (first.position[0] + second.position[0]) * 0.5,
            (first.position[1] + second.position[1]) * 0.5,
            (first.position[2] + second.position[2]) * 0.5,
        ),
        confidence=min(first.confidence, second.confidence),
        ray_gap=max(first.ray_gap, second.ray_gap),
        valid=first.valid and second.valid,
    )


def _add_interpolated_joint(
    result: dict[str, SkeletonJoint3D],
    name: str,
    first_name: str,
    second_name: str,
    amount: float,
) -> None:
    first = result.get(first_name)
    second = result.get(second_name)
    if first is None or second is None:
        return
    result[name] = SkeletonJoint3D(
        name=name,
        position=tuple(
            left + (right - left) * amount
            for left, right in zip(first.position, second.position, strict=True)
        ),  # type: ignore[arg-type]
        confidence=min(first.confidence, second.confidence),
        ray_gap=max(first.ray_gap, second.ray_gap),
        valid=first.valid and second.valid,
    )


def _invalid_segment(link: BoneLink) -> BoneSegment3D:
    return BoneSegment3D(
        name=link.name,
        parent=link.parent,
        child=link.child,
        start=(0.0, 0.0, 0.0),
        end=(0.0, 0.0, 0.0),
        length=0.0,
        confidence=0.0,
        valid=False,
    )


def _rotation_from_positive_y(direction: Vector3) -> tuple[float, ...]:
    """Return the shortest rotation that maps the local +Y axis to direction."""

    x, y, z = direction
    cosine = max(-1.0, min(1.0, y))
    perpendicular = math.sqrt(x * x + z * z)
    if perpendicular <= 1.0e-10:
        if cosine >= 0.0:
            return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        return (1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, -1.0)

    # K is the cross-product matrix for bind_axis(+Y) × direction.
    kx, ky, kz = z, 0.0, -x
    cross_matrix = (
        (0.0, -kz, ky),
        (kz, 0.0, -kx),
        (-ky, kx, 0.0),
    )
    cross_squared = _matrix_multiply(cross_matrix, cross_matrix)
    factor = (1.0 - cosine) / (perpendicular * perpendicular)
    return tuple(
        (1.0 if row == column else 0.0)
        + cross_matrix[row][column]
        + cross_squared[row][column] * factor
        for row in range(3)
        for column in range(3)
    )


def _rotation_from_positive_y_with_reference(
    direction: Vector3,
    reference: Vector3,
) -> tuple[float, ...]:
    local_x = _project_reference_axis(direction, reference)
    local_z = _normalize(_cross(local_x, direction))
    return (
        local_x[0], direction[0], local_z[0],
        local_x[1], direction[1], local_z[1],
        local_x[2], direction[2], local_z[2],
    )


def _project_reference_axis(direction: Vector3, reference: Vector3) -> Vector3:
    if (
        len(reference) != 3
        or not math.isfinite(reference[0])
        or not math.isfinite(reference[1])
        or not math.isfinite(reference[2])
    ):
        raise ValueError("bone local X reference must contain three finite coordinates")
    reference_length = _distance(reference, (0.0, 0.0, 0.0))
    if reference_length <= 1.0e-10:
        raise ValueError("bone local X reference must have non-zero length")
    unit_reference = (
        reference[0] / reference_length,
        reference[1] / reference_length,
        reference[2] / reference_length,
    )
    projection = _sub(
        unit_reference,  # type: ignore[arg-type]
        _scale(direction, _dot(unit_reference, direction)),  # type: ignore[arg-type]
    )
    if _distance(projection, (0.0, 0.0, 0.0)) <= 1.0e-6:
        fallback = min(
            ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
            key=lambda axis: abs(_dot(axis, direction)),
        )
        projection = _sub(fallback, _scale(direction, _dot(fallback, direction)))
    return _normalize(projection)


def _matrix_multiply(
    left: tuple[tuple[float, ...], ...],
    right: tuple[tuple[float, ...], ...],
) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(
            sum(left[row][index] * right[index][column] for index in range(3))
            for column in range(3)
        )
        for row in range(3)
    )


def _quantize_q1_14(value: float) -> int:
    if not math.isfinite(value):
        raise ValueError("bone matrix coefficient must be finite")
    scaled = value * (1 << 14)
    rounded = math.floor(scaled + 0.5) if scaled >= 0.0 else math.ceil(scaled - 0.5)
    if rounded < -32768 or rounded > 32767:
        raise ValueError("bone matrix coefficient exceeds signed Q1.14 range")
    return rounded


def _distance(first: Vector3, second: Vector3) -> float:
    dx = first[0] - second[0]
    dy = first[1] - second[1]
    dz = first[2] - second[2]
    return math.sqrt(sum((dx**2, dy**2, dz**2)))


def _normalize(vector: Vector3) -> Vector3:
    magnitude = _distance(vector, (0.0, 0.0, 0.0))
    if magnitude <= 1.0e-10:
        raise ValueError("bone direction must have non-zero length")
    return (
        vector[0] / magnitude,
        vector[1] / magnitude,
        vector[2] / magnitude,
    )


def _sub(first: Vector3, second: Vector3) -> Vector3:
    return (
        first[0] - second[0],
        first[1] - second[1],
        first[2] - second[2],
    )


def _scale(vector: Vector3, factor: float) -> Vector3:
    return (vector[0] * factor, vector[1] * factor, vector[2] * factor)


def _dot(first: Vector3, second: Vector3) -> float:
    return sum(
        (
            first[0] * second[0],
            first[1] * second[1],
            first[2] * second[2],
        )
    )


def _cross(first: Vector3, second: Vector3) -> Vector3:
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )
