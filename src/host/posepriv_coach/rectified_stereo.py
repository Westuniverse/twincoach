"""定点校正双摄三角测量的主机黄金模型与 AXI 打包契约。

该算子面向已经完成去畸变、极线校正和同步的双摄关键点。输入坐标使用
Q1.14 的归一化相机坐标，输出仍使用 Q1.14 的世界相对坐标。它不是通用
相机模型的替代品：通用旋转/畸变模型仍由 :mod:`triangulation` 负责，
本模块是其在固定安装、固定标定场景下的可综合加速路径。
"""

from __future__ import annotations

from dataclasses import dataclass
import math


COORDINATE_FRAC_BITS = 14
AXIS_BEAT_BYTES = 16
MIN_DISPARITY_RAW = 64
MIN_CONFIDENCE_RAW = 64
SIGNED16_MIN = -(1 << 15)
SIGNED16_MAX = (1 << 15) - 1
SIGNED32_MIN = -(1 << 31)
SIGNED32_MAX = (1 << 31) - 1


@dataclass(frozen=True, slots=True)
class RectificationCalibration:
    """把原始相机射线旋转到同一校正相机坐标系的标定参数。"""

    front_rotation_camera_to_rectified: tuple[float, ...]
    side_rotation_camera_to_rectified: tuple[float, ...]
    baseline: float

    def __post_init__(self) -> None:
        for name, rotation in (
            ("front", self.front_rotation_camera_to_rectified),
            ("side", self.side_rotation_camera_to_rectified),
        ):
            if len(rotation) != 9 or not all(math.isfinite(value) for value in rotation):
                raise ValueError(f"{name} rectification rotation must contain 9 finite values")
            rows = tuple(rotation[index : index + 3] for index in range(0, 9, 3))
            if any(abs(sum(value * value for value in row) - 1.0) > 1.0e-3 for row in rows):
                raise ValueError(f"{name} rectification rotation rows must be unit length")
            if any(
                abs(sum(rows[left][index] * rows[right][index] for index in range(3)))
                > 1.0e-3
                for left, right in ((0, 1), (0, 2), (1, 2))
            ):
                raise ValueError(f"{name} rectification rotation rows must be orthogonal")
            determinant = (
                rows[0][0] * (rows[1][1] * rows[2][2] - rows[1][2] * rows[2][1])
                - rows[0][1] * (rows[1][0] * rows[2][2] - rows[1][2] * rows[2][0])
                + rows[0][2] * (rows[1][0] * rows[2][1] - rows[1][1] * rows[2][0])
            )
            if abs(determinant - 1.0) > 1.0e-3:
                raise ValueError(f"{name} rectification rotation must be right-handed")
        if not math.isfinite(self.baseline) or self.baseline <= 0.0:
            raise ValueError("rectification baseline must be finite and positive")

    def rotate_normalized_ray(
        self,
        x: float,
        y: float,
        *,
        view: str,
    ) -> tuple[float, float]:
        """将针孔归一化射线变为校正坐标中的归一化点。"""

        if view == "front":
            rotation = self.front_rotation_camera_to_rectified
        elif view == "side":
            rotation = self.side_rotation_camera_to_rectified
        else:
            raise ValueError("rectification view must be front or side")
        rectified = (
            rotation[0] * x + rotation[1] * y + rotation[2],
            rotation[3] * x + rotation[4] * y + rotation[5],
            rotation[6] * x + rotation[7] * y + rotation[8],
        )
        if rectified[2] <= 1.0e-10:
            raise ValueError("rectification maps the ray behind the camera")
        return rectified[0] / rectified[2], rectified[1] / rectified[2]


def _check_signed(value: int, bits: int, name: str) -> int:
    minimum = -(1 << (bits - 1))
    maximum = (1 << (bits - 1)) - 1
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must fit signed {bits}-bit range")
    return int(value)


def _check_unsigned(value: int, bits: int, name: str) -> int:
    if not 0 <= value < (1 << bits):
        raise ValueError(f"{name} must fit unsigned {bits}-bit range")
    return int(value)


def _signed_to_bits(value: int, bits: int) -> int:
    return value & ((1 << bits) - 1)


def _bits_to_signed(value: int, bits: int) -> int:
    value &= (1 << bits) - 1
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def _trunc_division(numerator: int, denominator: int) -> int:
    """Return integer division rounded toward zero, matching the RTL sign path."""

    if denominator == 0:
        raise ZeroDivisionError("rectified stereo disparity is zero")
    magnitude = abs(numerator) // abs(denominator)
    return -magnitude if (numerator < 0) ^ (denominator < 0) else magnitude


def _saturate_signed32(value: int) -> int:
    return max(SIGNED32_MIN, min(SIGNED32_MAX, value))


@dataclass(frozen=True, slots=True)
class RectifiedStereoRequest:
    """One synchronized, rectified keypoint pair and fixed calibration values."""

    x_front: int
    y_front: int
    x_side: int
    y_side: int
    baseline: int
    focal: int
    joint_id: int
    front_confidence: int
    side_confidence: int
    last: bool = False

    def __post_init__(self) -> None:
        for name in ("x_front", "y_front", "x_side", "y_side", "baseline", "focal"):
            _check_signed(getattr(self, name), 16, name)
        _check_unsigned(self.joint_id, 8, "joint_id")
        _check_unsigned(self.front_confidence, 8, "front_confidence")
        _check_unsigned(self.side_confidence, 8, "side_confidence")


@dataclass(frozen=True, slots=True)
class RectifiedStereoResult:
    """Output coordinates and quality metadata produced by the fixed-point core."""

    x: int
    y: int
    z: int
    joint_id: int
    confidence: int
    valid: bool
    disparity_abs: int
    last: bool

    def __post_init__(self) -> None:
        for name in ("x", "y", "z"):
            _check_signed(getattr(self, name), 32, name)
        _check_unsigned(self.joint_id, 8, "joint_id")
        _check_unsigned(self.confidence, 8, "confidence")
        _check_unsigned(self.disparity_abs, 17, "disparity_abs")


def pack_rectified_stereo_request(request: RectifiedStereoRequest) -> bytes:
    """Pack the 128-bit input beat in little-endian byte order."""

    payload = 0
    fields = (
        (request.x_front, 0, 16),
        (request.y_front, 16, 16),
        (request.x_side, 32, 16),
        (request.y_side, 48, 16),
        (request.baseline, 64, 16),
        (request.focal, 80, 16),
        (request.joint_id, 96, 8),
        (request.front_confidence, 104, 8),
        (request.side_confidence, 112, 8),
    )
    for value, offset, width in fields:
        payload |= _signed_to_bits(value, width) << offset
    return payload.to_bytes(AXIS_BEAT_BYTES, "little")


def unpack_rectified_stereo_request(payload: bytes, *, last: bool = False) -> RectifiedStereoRequest:
    if len(payload) != AXIS_BEAT_BYTES:
        raise ValueError("rectified stereo request must be exactly 16 bytes")
    value = int.from_bytes(payload, "little")
    return RectifiedStereoRequest(
        x_front=_bits_to_signed(value >> 0, 16),
        y_front=_bits_to_signed(value >> 16, 16),
        x_side=_bits_to_signed(value >> 32, 16),
        y_side=_bits_to_signed(value >> 48, 16),
        baseline=_bits_to_signed(value >> 64, 16),
        focal=_bits_to_signed(value >> 80, 16),
        joint_id=(value >> 96) & 0xFF,
        front_confidence=(value >> 104) & 0xFF,
        side_confidence=(value >> 112) & 0xFF,
        last=last,
    )


def reference_rectified_stereo_output(
    request: RectifiedStereoRequest,
    *,
    minimum_disparity: int = MIN_DISPARITY_RAW,
    minimum_confidence: int = MIN_CONFIDENCE_RAW,
) -> RectifiedStereoResult:
    """Bit-accurate reference for ``rectified_triangulation_axis``.

    For rectified camera-normalized coordinates the stereo equations reduce to three
    shared-denominator divisions::

        X = B * x_front / disparity
        Y = B * y_front / disparity
        Z = F * B / disparity

    All operands are raw Q1.14 integers, so the quotient is also Q1.14.  When
    the host has already divided pixels by ``fx/fy``, the usual pinhole choice
    is ``focal=1.0`` (encoded as 16384); a different fixed coordinate scale may
    use another focal value.
    Invalid observations still produce one output beat, preserving metadata and
    ``TLAST`` for a DMA batch.
    """

    if not 0 <= minimum_confidence <= 255:
        raise ValueError("minimum_confidence must fit uint8")
    if not 1 <= minimum_disparity <= (1 << 17) - 1:
        raise ValueError("minimum_disparity must be positive and fit 17 bits")
    disparity = request.x_front - request.x_side
    disparity_abs = abs(disparity)
    confidence = min(request.front_confidence, request.side_confidence)
    valid = (
        confidence >= minimum_confidence
        and disparity_abs >= minimum_disparity
    )
    if not valid:
        x = y = z = 0
    else:
        x = _trunc_division(request.baseline * request.x_front, disparity)
        y = _trunc_division(request.baseline * request.y_front, disparity)
        z = _trunc_division(request.focal * request.baseline, disparity)
        x = _saturate_signed32(x)
        y = _saturate_signed32(y)
        z = _saturate_signed32(z)
    return RectifiedStereoResult(
        x=x,
        y=y,
        z=z,
        joint_id=request.joint_id,
        confidence=confidence,
        valid=valid,
        disparity_abs=disparity_abs,
        last=request.last,
    )


def pack_rectified_stereo_result(result: RectifiedStereoResult) -> bytes:
    """Pack the 128-bit output beat used by the AXI wrapper."""

    disparity_report = min(result.disparity_abs, (1 << 15) - 1)
    value = (
        _signed_to_bits(result.x, 32)
        | (_signed_to_bits(result.y, 32) << 32)
        | (_signed_to_bits(result.z, 32) << 64)
        | (result.joint_id << 96)
        | (result.confidence << 104)
        | (int(result.valid) << 112)
        | (disparity_report << 113)
    )
    return value.to_bytes(AXIS_BEAT_BYTES, "little")


def unpack_rectified_stereo_result(
    payload: bytes,
    *,
    last: bool = False,
) -> RectifiedStereoResult:
    if len(payload) != AXIS_BEAT_BYTES:
        raise ValueError("rectified stereo result must be exactly 16 bytes")
    value = int.from_bytes(payload, "little")
    return RectifiedStereoResult(
        x=_bits_to_signed(value >> 0, 32),
        y=_bits_to_signed(value >> 32, 32),
        z=_bits_to_signed(value >> 64, 32),
        joint_id=(value >> 96) & 0xFF,
        confidence=(value >> 104) & 0xFF,
        valid=bool((value >> 112) & 1),
        disparity_abs=(value >> 113) & 0x7FFF,
        last=last,
    )


__all__ = [
    "AXIS_BEAT_BYTES",
    "COORDINATE_FRAC_BITS",
    "MIN_CONFIDENCE_RAW",
    "MIN_DISPARITY_RAW",
    "RectifiedStereoRequest",
    "RectifiedStereoResult",
    "RectificationCalibration",
    "pack_rectified_stereo_request",
    "pack_rectified_stereo_result",
    "reference_rectified_stereo_output",
    "unpack_rectified_stereo_request",
    "unpack_rectified_stereo_result",
]
