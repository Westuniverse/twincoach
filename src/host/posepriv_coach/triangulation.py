"""双视角关键点三角测量的可解释软件参考实现。

坐标约定：相机内参使用像素单位；`PoseFrame` 中的关键点使用相对于源图像
宽高的归一化坐标。外参使用 camera-to-world 旋转矩阵和世界坐标中的相机
光心。模块只处理少量关键点，后续 PL 版本可以复用同样的射线和有效位契约。
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

from .contracts import Keypoint, SyncedPosePair
from .rectified_backend import RectifiedStereoBackend
from .rectified_stereo import (
    COORDINATE_FRAC_BITS,
    MIN_DISPARITY_RAW,
    RectificationCalibration,
    RectifiedStereoRequest,
    RectifiedStereoResult,
    reference_rectified_stereo_output,
)


_EPSILON = 1.0e-10
_ZERO_DISTORTION = (0.0, 0.0, 0.0, 0.0, 0.0)


@dataclass(frozen=True, slots=True)
class CameraIntrinsics:
    """针孔相机内参，焦距、主点和图像尺寸均使用像素单位。

    ``distortion`` 使用 OpenCV 常见的五项 Brown--Conrady 顺序
    ``(k1, k2, p1, p2, k3)``。空元组表示已经完成去畸变，保持旧配置兼容。
    """

    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    distortion: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("camera image dimensions must be positive")
        if not all(
            math.isfinite(value) for value in (self.fx, self.fy, self.cx, self.cy)
        ):
            raise ValueError("camera intrinsics must be finite")
        if self.fx <= 0.0 or self.fy <= 0.0:
            raise ValueError("camera focal lengths must be positive")
        if len(self.distortion) not in (0, 5):
            raise ValueError("distortion must be empty or contain k1,k2,p1,p2,k3")
        if not all(math.isfinite(value) for value in self.distortion):
            raise ValueError("camera distortion must be finite")

    def pixel_from_normalized(self, x: float, y: float) -> tuple[float, float]:
        if not all(math.isfinite(value) for value in (x, y)):
            raise ValueError("normalized image coordinates must be finite")
        return x * self.width, y * self.height

    def undistort_normalized(
        self, x_distorted: float, y_distorted: float
    ) -> tuple[float, float]:
        """将像素反投影得到的畸变归一化坐标迭代还原到针孔坐标。"""

        if not math.isfinite(x_distorted) or not math.isfinite(y_distorted):
            raise ValueError("distorted normalized coordinates must be finite")
        distortion = self.distortion
        if not distortion or distortion == _ZERO_DISTORTION:
            return x_distorted, y_distorted
        k1, k2, p1, p2, k3 = distortion
        two_p1 = 2.0 * p1
        two_p2 = 2.0 * p2
        x = x_distorted
        y = y_distorted
        for _ in range(8):
            radius_sq = x * x + y * y
            radial = (
                1.0
                + k1 * radius_sq
                + k2 * radius_sq * radius_sq
                + k3 * radius_sq * radius_sq * radius_sq
            )
            if abs(radial) <= _EPSILON:
                raise ValueError("camera distortion inversion is singular")
            delta_x = two_p1 * x * y + p2 * (radius_sq + 2.0 * x * x)
            delta_y = p1 * (radius_sq + 2.0 * y * y) + two_p2 * x * y
            x = (x_distorted - delta_x) / radial
            y = (y_distorted - delta_y) / radial
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError("camera distortion inversion did not remain finite")
        projected_x, projected_y = _distort_normalized(x, y, distortion)
        residual = math.hypot(projected_x - x_distorted, projected_y - y_distorted)
        if residual > 1.0e-7:
            raise ValueError(
                "camera distortion inversion did not converge "
                f"(normalized residual {residual:.3g})"
            )
        return x, y


@dataclass(frozen=True, slots=True)
class CameraExtrinsics:
    """相机到世界坐标的刚体外参。矩阵按行主序存储。"""

    rotation_camera_to_world: tuple[float, ...]
    center_world: tuple[float, float, float]

    def __post_init__(self) -> None:
        if len(self.rotation_camera_to_world) != 9:
            raise ValueError("camera rotation must contain nine coefficients")
        if len(self.center_world) != 3:
            raise ValueError("camera center must contain three coordinates")
        if not all(math.isfinite(value) for value in self.rotation_camera_to_world):
            raise ValueError("camera rotation must be finite")
        if not all(math.isfinite(value) for value in self.center_world):
            raise ValueError("camera center must be finite")
        rows = tuple(
            self.rotation_camera_to_world[index : index + 3]
            for index in range(0, 9, 3)
        )
        tolerance = 1.0e-3
        for index, row in enumerate(rows):
            if abs(_dot(row, row) - 1.0) > tolerance:
                raise ValueError(f"camera rotation row {index} must have unit length")
        if any(
            abs(_dot(rows[left], rows[right])) > tolerance
            for left, right in ((0, 1), (0, 2), (1, 2))
        ):
            raise ValueError("camera rotation rows must be orthogonal")
        determinant = (
            rows[0][0] * (rows[1][1] * rows[2][2] - rows[1][2] * rows[2][1])
            - rows[0][1] * (rows[1][0] * rows[2][2] - rows[1][2] * rows[2][0])
            + rows[0][2] * (rows[1][0] * rows[2][1] - rows[1][1] * rows[2][0])
        )
        if abs(determinant - 1.0) > tolerance:
            raise ValueError("camera rotation must be right-handed with determinant +1")

    @classmethod
    def identity(cls) -> "CameraExtrinsics":
        return cls(
            rotation_camera_to_world=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
            center_world=(0.0, 0.0, 0.0),
        )


@dataclass(frozen=True, slots=True)
class TriangulatedPoint:
    """一对关键点生成的世界坐标和质量信息。"""

    x: float
    y: float
    z: float
    confidence: float
    ray_gap: float
    valid: bool
    ray_angle_degrees: float = 0.0

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (self.x, self.y, self.z)):
            raise ValueError("triangulated point coordinates must be finite")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("triangulated confidence must be within [0, 1]")
        if not (math.isfinite(self.ray_gap) or math.isinf(self.ray_gap)):
            raise ValueError("triangulated ray gap must be finite or infinity")
        if self.ray_gap < 0.0:
            raise ValueError("triangulated ray gap must be non-negative")
        if (
            not math.isfinite(self.ray_angle_degrees)
            or not 0.0 <= self.ray_angle_degrees <= 90.0
        ):
            raise ValueError("triangulated ray angle must be within [0, 90] degrees")


@dataclass(frozen=True, slots=True)
class _Ray:
    origin: tuple[float, float, float]
    direction: tuple[float, float, float]


def triangulate_keypoint(
    front: Keypoint,
    side: Keypoint,
    front_intrinsics: CameraIntrinsics,
    side_intrinsics: CameraIntrinsics,
    front_extrinsics: CameraExtrinsics,
    side_extrinsics: CameraExtrinsics,
    *,
    minimum_confidence: float = 0.35,
    maximum_ray_gap: float = 0.15,
    minimum_ray_angle_degrees: float = 1.0,
) -> TriangulatedPoint:
    """用两条观测射线的最近点中点估计世界坐标。

    `maximum_ray_gap` 与世界坐标使用同一单位。`minimum_ray_angle_degrees`
    限制两条直线的锐角夹角，避免低视差放大深度噪声。射线近似平行、
    夹角不足、关键点置信度不足或最近点间距过大时返回 `valid=False`。
    """

    if not 0.0 <= minimum_confidence <= 1.0:
        raise ValueError("minimum_confidence must be within [0, 1]")
    if not math.isfinite(maximum_ray_gap) or maximum_ray_gap < 0.0:
        raise ValueError("maximum_ray_gap must be finite and non-negative")
    if (
        not math.isfinite(minimum_ray_angle_degrees)
        or not 0.0 <= minimum_ray_angle_degrees <= 90.0
    ):
        raise ValueError("minimum_ray_angle_degrees must be within [0, 90]")
    confidence = min(front.confidence, side.confidence)
    if confidence < minimum_confidence:
        return _invalid_point(confidence)

    try:
        front_ray = _ray_from_keypoint(front, front_intrinsics, front_extrinsics)
        side_ray = _ray_from_keypoint(side, side_intrinsics, side_extrinsics)
    except ValueError:
        # A single edge keypoint must not terminate a live session when the
        # inverse distortion model is locally singular or fails to converge.
        return _invalid_point(confidence)
    ray_angle_degrees = math.degrees(
        math.acos(min(1.0, max(0.0, abs(_dot(front_ray.direction, side_ray.direction)))))
    )
    if ray_angle_degrees < minimum_ray_angle_degrees:
        return _invalid_point(confidence, ray_angle_degrees=ray_angle_degrees)
    closest = _closest_points(front_ray, side_ray)
    if closest is None:
        return _invalid_point(confidence, ray_angle_degrees=ray_angle_degrees)
    front_point, side_point = closest
    midpoint = tuple((left + right) * 0.5 for left, right in zip(front_point, side_point))
    ray_gap = _distance(front_point, side_point)
    valid = ray_gap <= maximum_ray_gap
    return TriangulatedPoint(
        x=midpoint[0],
        y=midpoint[1],
        z=midpoint[2],
        confidence=confidence if valid else 0.0,
        ray_gap=ray_gap,
        valid=valid,
        ray_angle_degrees=ray_angle_degrees,
    )


def project_world_keypoint(
    point_world: tuple[float, float, float],
    intrinsics: CameraIntrinsics,
    extrinsics: CameraExtrinsics,
    *,
    confidence: float = 1.0,
) -> Keypoint:
    """将世界点投影到归一化图像坐标，供标定回环和确定性样例使用。

    外参保存 camera-to-world 旋转，因此投影使用其转置完成 world-to-camera。
    位于相机后方或光心平面的点没有有效针孔投影，会显式报错。
    """

    if len(point_world) != 3 or not all(math.isfinite(value) for value in point_world):
        raise ValueError("world point must contain three finite coordinates")
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be within [0, 1]")
    relative = _sub(point_world, extrinsics.center_world)
    rotation = extrinsics.rotation_camera_to_world
    camera_x = rotation[0] * relative[0] + rotation[3] * relative[1] + rotation[6] * relative[2]
    camera_y = rotation[1] * relative[0] + rotation[4] * relative[1] + rotation[7] * relative[2]
    camera_z = rotation[2] * relative[0] + rotation[5] * relative[1] + rotation[8] * relative[2]
    if camera_z <= _EPSILON:
        raise ValueError("world point must be in front of the camera")
    x = camera_x / camera_z
    y = camera_y / camera_z
    if intrinsics.distortion:
        x, y = _distort_normalized(x, y, intrinsics.distortion)
    return Keypoint(
        x=(intrinsics.fx * x + intrinsics.cx) / intrinsics.width,
        y=(intrinsics.fy * y + intrinsics.cy) / intrinsics.height,
        confidence=confidence,
    )


def triangulate_synced_pose_pair(
    pair: SyncedPosePair,
    front_intrinsics: CameraIntrinsics,
    side_intrinsics: CameraIntrinsics,
    front_extrinsics: CameraExtrinsics,
    side_extrinsics: CameraExtrinsics,
    *,
    keypoint_names: Iterable[str] | None = None,
    minimum_confidence: float = 0.35,
    maximum_ray_gap: float = 0.15,
    minimum_ray_angle_degrees: float = 1.0,
) -> dict[str, TriangulatedPoint]:
    """按关键点名称对一帧同步姿态进行三角测量。"""

    names = tuple(keypoint_names) if keypoint_names is not None else tuple(
        name for name in pair.front.keypoints if name in pair.side.keypoints
    )
    result: dict[str, TriangulatedPoint] = {}
    for name in names:
        front_point = pair.front.keypoints.get(name)
        side_point = pair.side.keypoints.get(name)
        if front_point is None or side_point is None:
            continue
        result[name] = triangulate_keypoint(
            front_point,
            side_point,
            front_intrinsics,
            side_intrinsics,
            front_extrinsics,
            side_extrinsics,
            minimum_confidence=minimum_confidence,
            maximum_ray_gap=maximum_ray_gap,
            minimum_ray_angle_degrees=minimum_ray_angle_degrees,
        )
    return result


def triangulate_rectified_keypoint(
    front: Keypoint,
    side: Keypoint,
    front_intrinsics: CameraIntrinsics,
    side_intrinsics: CameraIntrinsics,
    front_extrinsics: CameraExtrinsics,
    *,
    side_extrinsics: CameraExtrinsics | None = None,
    rectification: RectificationCalibration | None = None,
    baseline: float,
    focal: float,
    minimum_confidence: float = 0.35,
    minimum_disparity: float = MIN_DISPARITY_RAW / (1 << COORDINATE_FRAC_BITS),
    maximum_vertical_disparity: float = 0.02,
) -> TriangulatedPoint:
    """Use the fixed-point rectified path for one synchronized keypoint pair.

    The caller must provide already undistorted/rectified observations.  The
    resulting camera-space point is rotated and translated with the front
    camera-to-world pose, so the rest of the avatar planner keeps its existing
    world-coordinate contract.  ``ray_gap`` is zero for a valid rectified
    observation because the simplified model assumes coincident epipolar rays;
    the reported ray angle still exposes low-disparity geometry.
    """

    if not 0.0 <= minimum_confidence <= 1.0:
        raise ValueError("minimum_confidence must be within [0, 1]")
    if not math.isfinite(baseline) or baseline <= 0.0:
        raise ValueError("rectified stereo baseline must be finite and positive")
    if not math.isfinite(focal) or focal <= 0.0:
        raise ValueError("rectified stereo focal must be finite and positive")
    if not math.isfinite(minimum_disparity) or minimum_disparity <= 0.0:
        raise ValueError("rectified stereo minimum disparity must be positive")
    if (
        not math.isfinite(maximum_vertical_disparity)
        or maximum_vertical_disparity < 0.0
    ):
        raise ValueError("rectified stereo vertical disparity must be non-negative")
    confidence = min(front.confidence, side.confidence)
    if confidence < minimum_confidence:
        return _invalid_point(confidence)
    if rectification is None and (
        side_extrinsics is not None
        and not _rotations_close(
            front_extrinsics.rotation_camera_to_world,
            side_extrinsics.rotation_camera_to_world,
        )
    ):
        # The simplified Q1.14 equations operate in one rectified camera
        # frame.  Do not reinterpret a 90-degree, unrectified side camera as
        # horizontal disparity; the general ray solver remains available for
        # that geometry.
        return _invalid_point(confidence)
    if rectification is None and (
        _has_nonzero_distortion(front_intrinsics)
        or _has_nonzero_distortion(side_intrinsics)
    ):
        # A rectified fixed-point core has no distortion iteration.  Keep a
        # single point invalid rather than silently applying the wrong model.
        return _invalid_point(confidence)

    try:
        if rectification is None:
            front_x, front_y = _normalized_camera_coordinates(front, front_intrinsics)
            side_x, side_y = _normalized_camera_coordinates(side, side_intrinsics)
        else:
            front_x, front_y = _rectified_camera_coordinates(
                front,
                front_intrinsics,
                rectification,
                view="front",
            )
            side_x, side_y = _rectified_camera_coordinates(
                side,
                side_intrinsics,
                rectification,
                view="side",
            )
    except ValueError:
        return _invalid_point(confidence)
    vertical_error = abs(front_y - side_y)
    ray_angle_degrees = _rectified_ray_angle(
        front_x,
        front_y,
        side_x,
        side_y,
    )
    if vertical_error > maximum_vertical_disparity:
        return _invalid_point(confidence, ray_angle_degrees=ray_angle_degrees)

    scale = 1 << COORDINATE_FRAC_BITS
    request = RectifiedStereoRequest(
        x_front=_quantize_rectified(front_x, "front x"),
        y_front=_quantize_rectified(front_y, "front y"),
        x_side=_quantize_rectified(side_x, "side x"),
        y_side=_quantize_rectified(side_y, "side y"),
        baseline=_quantize_rectified(baseline, "baseline"),
        focal=_quantize_rectified(focal, "focal"),
        joint_id=0,
        front_confidence=min(255, max(0, round(front.confidence * 255.0))),
        side_confidence=min(255, max(0, round(side.confidence * 255.0))),
    )
    reference = reference_rectified_stereo_output(
        request,
        minimum_disparity=max(1, math.ceil(minimum_disparity * scale)),
        minimum_confidence=max(0, math.ceil(minimum_confidence * 255.0)),
    )
    if not reference.valid:
        return _invalid_point(confidence, ray_angle_degrees=ray_angle_degrees)
    camera_point = (
        reference.x / scale,
        reference.y / scale,
        reference.z / scale,
    )
    world_rotation = front_extrinsics.rotation_camera_to_world
    if rectification is not None:
        world_rotation = _mat3_mul(
            front_extrinsics.rotation_camera_to_world,
            _mat3_transpose(rectification.front_rotation_camera_to_rectified),
        )
    world_point = _add(
        front_extrinsics.center_world,
        _mat3_vec(world_rotation, camera_point),
    )
    return TriangulatedPoint(
        x=world_point[0],
        y=world_point[1],
        z=world_point[2],
        confidence=confidence,
        ray_gap=0.0,
        valid=True,
        ray_angle_degrees=ray_angle_degrees,
    )


def triangulate_rectified_synced_pose_pair(
    pair: SyncedPosePair,
    front_intrinsics: CameraIntrinsics,
    side_intrinsics: CameraIntrinsics,
    front_extrinsics: CameraExtrinsics,
    side_extrinsics: CameraExtrinsics,
    *,
    baseline: float,
    focal: float,
    keypoint_names: Iterable[str] | None = None,
    minimum_confidence: float = 0.35,
    minimum_disparity: float = MIN_DISPARITY_RAW / (1 << COORDINATE_FRAC_BITS),
    maximum_vertical_disparity: float = 0.02,
    rectification: RectificationCalibration | None = None,
    backend: RectifiedStereoBackend | None = None,
) -> dict[str, TriangulatedPoint]:
    """Triangulate all common keypoints through reference or a batch backend."""

    names = tuple(keypoint_names) if keypoint_names is not None else tuple(
        name for name in pair.front.keypoints if name in pair.side.keypoints
    )
    result: dict[str, TriangulatedPoint] = {}
    if backend is not None:
        return _triangulate_rectified_with_backend(
            pair,
            names,
            front_intrinsics,
            side_intrinsics,
            front_extrinsics,
            side_extrinsics,
            baseline=baseline,
            focal=focal,
            minimum_confidence=minimum_confidence,
            minimum_disparity=minimum_disparity,
            maximum_vertical_disparity=maximum_vertical_disparity,
            rectification=rectification,
            backend=backend,
        )
    for name in names:
        front_point = pair.front.keypoints.get(name)
        side_point = pair.side.keypoints.get(name)
        if front_point is None or side_point is None:
            continue
        result[name] = triangulate_rectified_keypoint(
            front_point,
            side_point,
            front_intrinsics,
            side_intrinsics,
            front_extrinsics,
            side_extrinsics=side_extrinsics,
            rectification=rectification,
            baseline=baseline,
            focal=focal,
            minimum_confidence=minimum_confidence,
            minimum_disparity=minimum_disparity,
            maximum_vertical_disparity=maximum_vertical_disparity,
        )
    return result


def _triangulate_rectified_with_backend(
    pair: SyncedPosePair,
    names: tuple[str, ...],
    front_intrinsics: CameraIntrinsics,
    side_intrinsics: CameraIntrinsics,
    front_extrinsics: CameraExtrinsics,
    side_extrinsics: CameraExtrinsics,
    *,
    baseline: float,
    focal: float,
    minimum_confidence: float,
    minimum_disparity: float,
    maximum_vertical_disparity: float,
    rectification: RectificationCalibration | None,
    backend: RectifiedStereoBackend,
) -> dict[str, TriangulatedPoint]:
    prepared: list[tuple[str, float, float, RectifiedStereoRequest]] = []
    for name in names:
        front = pair.front.keypoints.get(name)
        side = pair.side.keypoints.get(name)
        if front is None or side is None:
            continue
        confidence = min(front.confidence, side.confidence)
        coordinates = _rectified_coordinates_or_none(
            front,
            side,
            front_intrinsics,
            side_intrinsics,
            front_extrinsics,
            side_extrinsics,
            rectification=rectification,
        )
        if coordinates is None:
            request = _invalid_rectified_request(
                baseline=baseline,
                focal=focal,
                joint_id=len(prepared),
                last=False,
            )
            ray_angle_degrees = 0.0
        else:
            front_x, front_y, side_x, side_y = coordinates
            ray_angle_degrees = _rectified_ray_angle(
                front_x,
                front_y,
                side_x,
                side_y,
            )
            request = _make_rectified_backend_request(
                front,
                side,
                front_x,
                front_y,
                side_x,
                side_y,
                baseline=baseline,
                focal=focal,
                joint_id=len(prepared),
                minimum_confidence=minimum_confidence,
                minimum_disparity=minimum_disparity,
                maximum_vertical_disparity=maximum_vertical_disparity,
            )
        prepared.append((name, confidence, ray_angle_degrees, request))
    if not prepared:
        return {}
    requests = tuple(
        _replace_rectified_last(item[3], index == len(prepared) - 1)
        for index, item in enumerate(prepared)
    )
    outputs = backend.exchange(requests)
    if len(outputs) != len(requests):
        raise ValueError("rectified stereo backend returned an unexpected result count")
    result: dict[str, TriangulatedPoint] = {}
    for (name, confidence, ray_angle_degrees, _), request, output in zip(
        prepared,
        requests,
        outputs,
        strict=True,
    ):
        if output.joint_id != request.joint_id or output.last != request.last:
            raise ValueError("rectified stereo backend metadata does not match request")
        result[name] = _rectified_result_to_point(
            output,
            confidence=confidence,
            ray_angle_degrees=ray_angle_degrees,
            front_extrinsics=front_extrinsics,
            rectification=rectification,
        )
    return result


def _rectified_coordinates_or_none(
    front: Keypoint,
    side: Keypoint,
    front_intrinsics: CameraIntrinsics,
    side_intrinsics: CameraIntrinsics,
    front_extrinsics: CameraExtrinsics,
    side_extrinsics: CameraExtrinsics,
    *,
    rectification: RectificationCalibration | None,
) -> tuple[float, float, float, float] | None:
    """Return both rays in the fixed core's common coordinate frame.

    The reference path historically accepted only equal rotations and zero
    distortion.  Keep that safety rule when no OpenCV rectification contract
    is present; a calibrated rectification explicitly opts into the more
    general camera-to-rectified rotation.
    """

    if rectification is None and (
        not _rotations_close(
            front_extrinsics.rotation_camera_to_world,
            side_extrinsics.rotation_camera_to_world,
        )
        or _has_nonzero_distortion(front_intrinsics)
        or _has_nonzero_distortion(side_intrinsics)
    ):
        return None
    try:
        if rectification is None:
            front_x, front_y = _normalized_camera_coordinates(front, front_intrinsics)
            side_x, side_y = _normalized_camera_coordinates(side, side_intrinsics)
        else:
            front_x, front_y = _rectified_camera_coordinates(
                front, front_intrinsics, rectification, view="front"
            )
            side_x, side_y = _rectified_camera_coordinates(
                side, side_intrinsics, rectification, view="side"
            )
    except ValueError:
        return None
    return front_x, front_y, side_x, side_y


def _invalid_rectified_request(
    *,
    baseline: float,
    focal: float,
    joint_id: int,
    last: bool,
) -> RectifiedStereoRequest:
    """Build a zero-confidence beat while preserving batch metadata."""

    return RectifiedStereoRequest(
        x_front=0,
        y_front=0,
        x_side=0,
        y_side=0,
        baseline=_quantize_rectified(baseline, "baseline"),
        focal=_quantize_rectified(focal, "focal"),
        joint_id=joint_id,
        front_confidence=0,
        side_confidence=0,
        last=last,
    )


def _make_rectified_backend_request(
    front: Keypoint,
    side: Keypoint,
    front_x: float,
    front_y: float,
    side_x: float,
    side_y: float,
    *,
    baseline: float,
    focal: float,
    joint_id: int,
    minimum_confidence: float,
    minimum_disparity: float,
    maximum_vertical_disparity: float,
) -> RectifiedStereoRequest:
    """Quantize a host observation to one 128-bit accelerator beat.

    Host-only quality gates are represented as zero confidence, not by dropping
    the beat.  This keeps ``joint_id`` and TLAST aligned when a DMA batch mixes
    valid and invalid joints.
    """

    front_confidence = min(255, max(0, round(front.confidence * 255.0)))
    side_confidence = min(255, max(0, round(side.confidence * 255.0)))
    confidence_ok = min(front.confidence, side.confidence) >= minimum_confidence
    disparity = abs(front_x - side_x)
    quality_ok = (
        confidence_ok
        and disparity >= minimum_disparity
        and abs(front_y - side_y) <= maximum_vertical_disparity
    )
    if not quality_ok:
        front_confidence = side_confidence = 0
    return RectifiedStereoRequest(
        x_front=_quantize_rectified(front_x, "front x"),
        y_front=_quantize_rectified(front_y, "front y"),
        x_side=_quantize_rectified(side_x, "side x"),
        y_side=_quantize_rectified(side_y, "side y"),
        baseline=_quantize_rectified(baseline, "baseline"),
        focal=_quantize_rectified(focal, "focal"),
        joint_id=joint_id,
        front_confidence=front_confidence,
        side_confidence=side_confidence,
    )


def _replace_rectified_last(
    request: RectifiedStereoRequest,
    last: bool,
) -> RectifiedStereoRequest:
    """Return a request with a batch-local TLAST marker."""

    return RectifiedStereoRequest(
        x_front=request.x_front,
        y_front=request.y_front,
        x_side=request.x_side,
        y_side=request.y_side,
        baseline=request.baseline,
        focal=request.focal,
        joint_id=request.joint_id,
        front_confidence=request.front_confidence,
        side_confidence=request.side_confidence,
        last=last,
    )


def _rectified_result_to_point(
    result: RectifiedStereoResult,
    *,
    confidence: float,
    ray_angle_degrees: float,
    front_extrinsics: CameraExtrinsics,
    rectification: RectificationCalibration | None,
) -> TriangulatedPoint:
    if not result.valid:
        return _invalid_point(confidence, ray_angle_degrees=ray_angle_degrees)
    scale = 1 << COORDINATE_FRAC_BITS
    camera_point = (result.x / scale, result.y / scale, result.z / scale)
    world_rotation = front_extrinsics.rotation_camera_to_world
    if rectification is not None:
        world_rotation = _mat3_mul(
            world_rotation,
            _mat3_transpose(rectification.front_rotation_camera_to_rectified),
        )
    world_point = _add(
        front_extrinsics.center_world,
        _mat3_vec(world_rotation, camera_point),
    )
    return TriangulatedPoint(
        x=world_point[0],
        y=world_point[1],
        z=world_point[2],
        confidence=confidence,
        ray_gap=0.0,
        valid=True,
        ray_angle_degrees=ray_angle_degrees,
    )


def _normalized_camera_coordinates(
    point: Keypoint,
    intrinsics: CameraIntrinsics,
) -> tuple[float, float]:
    pixel_x, pixel_y = intrinsics.pixel_from_normalized(point.x, point.y)
    return (
        (pixel_x - intrinsics.cx) / intrinsics.fx,
        (pixel_y - intrinsics.cy) / intrinsics.fy,
    )


def _rectified_camera_coordinates(
    point: Keypoint,
    intrinsics: CameraIntrinsics,
    rectification: RectificationCalibration,
    *,
    view: str,
) -> tuple[float, float]:
    pixel_x, pixel_y = intrinsics.pixel_from_normalized(point.x, point.y)
    camera_x = (pixel_x - intrinsics.cx) / intrinsics.fx
    camera_y = (pixel_y - intrinsics.cy) / intrinsics.fy
    undistorted_x, undistorted_y = intrinsics.undistort_normalized(camera_x, camera_y)
    return rectification.rotate_normalized_ray(
        undistorted_x,
        undistorted_y,
        view=view,
    )


def _has_nonzero_distortion(intrinsics: CameraIntrinsics) -> bool:
    return any(abs(value) > 1.0e-12 for value in intrinsics.distortion)


def _rotations_close(
    first: tuple[float, ...],
    second: tuple[float, ...],
    *,
    tolerance: float = 1.0e-6,
) -> bool:
    return len(first) == len(second) and all(
        abs(left - right) <= tolerance for left, right in zip(first, second)
    )


def _quantize_rectified(value: float, name: str) -> int:
    if not math.isfinite(value):
        raise ValueError(f"rectified stereo {name} must be finite")
    quantized = round(value * (1 << COORDINATE_FRAC_BITS))
    if quantized < -(1 << 15) or quantized > (1 << 15) - 1:
        raise ValueError(f"rectified stereo {name} exceeds signed Q1.14 range")
    return int(quantized)


def _rectified_ray_angle(
    front_x: float,
    front_y: float,
    side_x: float,
    side_y: float,
) -> float:
    first = _normalize((front_x, front_y, 1.0))
    second = _normalize((side_x, side_y, 1.0))
    dot = min(1.0, max(0.0, abs(_dot(first, second))))
    return math.degrees(math.acos(dot))


def _ray_from_keypoint(
    point: Keypoint,
    intrinsics: CameraIntrinsics,
    extrinsics: CameraExtrinsics,
) -> _Ray:
    pixel_x, pixel_y = intrinsics.pixel_from_normalized(point.x, point.y)
    camera_x = (pixel_x - intrinsics.cx) / intrinsics.fx
    camera_y = (pixel_y - intrinsics.cy) / intrinsics.fy
    undistorted_x, undistorted_y = intrinsics.undistort_normalized(camera_x, camera_y)
    camera_direction = (undistorted_x, undistorted_y, 1.0)
    normalized_camera_direction = _normalize(camera_direction)
    world_direction = _mat3_vec(extrinsics.rotation_camera_to_world, normalized_camera_direction)
    return _Ray(
        origin=extrinsics.center_world,
        direction=_normalize(world_direction),
    )


def _distort_normalized(
    x: float,
    y: float,
    distortion: tuple[float, ...],
) -> tuple[float, float]:
    k1, k2, p1, p2, k3 = distortion
    radius_sq = x * x + y * y
    radial = 1.0 + k1 * radius_sq + k2 * radius_sq**2 + k3 * radius_sq**3
    return (
        x * radial + 2.0 * p1 * x * y + p2 * (radius_sq + 2.0 * x * x),
        y * radial + p1 * (radius_sq + 2.0 * y * y) + 2.0 * p2 * x * y,
    )


def _closest_points(
    first: _Ray,
    second: _Ray,
) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    """返回两条前向射线的最近点；平行或最近点位于相机后方时返回 None。"""

    delta = _sub(first.origin, second.origin)
    dot_dirs = _dot(first.direction, second.direction)
    determinant = 1.0 - dot_dirs * dot_dirs
    if abs(determinant) <= _EPSILON:
        return None
    right_first = -_dot(first.direction, delta)
    right_second = _dot(second.direction, delta)
    first_distance = (right_first + dot_dirs * right_second) / determinant
    second_distance = (dot_dirs * right_first + right_second) / determinant
    if first_distance <= _EPSILON or second_distance <= _EPSILON:
        return None
    first_point = _add(first.origin, _scale(first.direction, first_distance))
    second_point = _add(second.origin, _scale(second.direction, second_distance))
    return first_point, second_point


def _invalid_point(
    confidence: float,
    *,
    ray_angle_degrees: float = 0.0,
) -> TriangulatedPoint:
    return TriangulatedPoint(
        x=0.0,
        y=0.0,
        z=0.0,
        confidence=confidence,
        ray_gap=math.inf,
        valid=False,
        ray_angle_degrees=ray_angle_degrees,
    )


def _mat3_vec(
    matrix: tuple[float, ...],
    vector: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        matrix[0] * vector[0] + matrix[1] * vector[1] + matrix[2] * vector[2],
        matrix[3] * vector[0] + matrix[4] * vector[1] + matrix[5] * vector[2],
        matrix[6] * vector[0] + matrix[7] * vector[1] + matrix[8] * vector[2],
    )


def _mat3_mul(first: tuple[float, ...], second: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(
        sum(first[row * 3 + index] * second[index * 3 + column] for index in range(3))
        for row in range(3)
        for column in range(3)
    )


def _mat3_transpose(matrix: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(matrix[column * 3 + row] for row in range(3) for column in range(3))


def _normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    magnitude = math.sqrt(_dot(vector, vector))
    if magnitude <= _EPSILON:
        raise ValueError("camera ray must have non-zero length")
    return (
        vector[0] / magnitude,
        vector[1] / magnitude,
        vector[2] / magnitude,
    )


def _dot(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    return sum(
        (
            first[0] * second[0],
            first[1] * second[1],
            first[2] * second[2],
        )
    )


def _add(
    first: tuple[float, float, float],
    second: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        first[0] + second[0],
        first[1] + second[1],
        first[2] + second[2],
    )


def _sub(
    first: tuple[float, float, float],
    second: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        first[0] - second[0],
        first[1] - second[1],
        first[2] - second[2],
    )


def _scale(vector: tuple[float, float, float], scalar: float) -> tuple[float, float, float]:
    return (
        vector[0] * scalar,
        vector[1] * scalar,
        vector[2] * scalar,
    )


def _distance(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    delta = _sub(first, second)
    return math.sqrt(_dot(delta, delta))
