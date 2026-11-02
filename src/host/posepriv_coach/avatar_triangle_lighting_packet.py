"""Experimental lighting AXIS v0.2 codec and explicit numerical selection.

No board transport or production renderer is enabled here. Protocol failures
raise; only valid finite faces outside the numerical PL budget use CPU lighting.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from .avatar_triangle_lighting_fixed import (
    AMBIENT_Q15, CAMERA_SCALE, DEGENERATE_Q15, LIGHT_SCALE, quantize_camera_point,
    triangle_lighting_fixed,
)

NORMAL_BUDGET_FACTOR = 512


class LightingProtocolError(ValueError):
    """A malformed/mismatched result is not a numerical CPU fallback."""


def _rgb_and_id(rgb, face_id):
    if len(rgb) != 3:
        raise ValueError("three RGB8 integers required")
    rgb_type = type(rgb)
    if rgb_type is tuple or rgb_type is list:
        # Only exact built-ins: generic containers/subclasses retain their
        # original iteration behavior and short-circuit exception precedence.
        red, green, blue = rgb
        invalid = (type(red) is not int or not 0 <= red <= 255
            or type(green) is not int or not 0 <= green <= 255
            or type(blue) is not int or not 0 <= blue <= 255)
    else:
        invalid = any(type(v) is not int or not 0 <= v <= 255 for v in rgb)
    if invalid:
        raise ValueError("three RGB8 integers required")
    if type(face_id) is not int or not 0 <= face_id <= 65535:
        raise ValueError("16-bit integer face_id required")


@dataclass(frozen=True, slots=True)
class LightingRequest:
    points: tuple[tuple[int, int, int], ...]
    rgb: tuple[int, int, int]
    face_id: int

    def __post_init__(self):
        _rgb_and_id(self.rgb, self.face_id)
        if type(self.points) is not tuple or len(self.points) != 3:
            raise ValueError("immutable three signed24 points and RGB tuple required")
        first, second, third = self.points
        # Preserve all row-shape checks before any coordinate checks. Exact
        # tuples/ints have no user iteration/comparison hooks to skip here.
        if (type(first) is not tuple or len(first) != 3
                or type(second) is not tuple or len(second) != 3
                or type(third) is not tuple or len(third) != 3):
            raise ValueError("immutable three signed24 points and RGB tuple required")
        for point in self.points:
            x, y, z = point
            if (type(x) is not int or not -(1 << 23) <= x < (1 << 23)
                    or type(y) is not int or not -(1 << 23) <= y < (1 << 23)
                    or type(z) is not int or not -(1 << 23) <= z < (1 << 23)):
                raise ValueError("immutable three signed24 points and RGB tuple required")
        if type(self.rgb) is not tuple:
            raise ValueError("immutable three signed24 points and RGB tuple required")


@dataclass(frozen=True, slots=True)
class LightingResponse:
    color_bgr8: tuple[int, int, int]
    light_q15: int
    face_id: int
    degenerate: bool
    normal_length: int

    def __post_init__(self):
        try:
            _rgb_and_id(self.color_bgr8, self.face_id)
        except (ValueError, TypeError) as error:
            raise LightingProtocolError("invalid result color/id") from error
        if (type(self.color_bgr8) is not tuple or type(self.degenerate) is not bool
                or type(self.light_q15) is not int or type(self.normal_length) is not int):
            raise LightingProtocolError("invalid result field types")
        if self.degenerate:
            if self.normal_length != 0 or self.light_q15 != DEGENERATE_Q15:
                raise LightingProtocolError("inconsistent degenerate result")
        elif (not AMBIENT_Q15 <= self.light_q15 <= LIGHT_SCALE
                or not 17 <= self.normal_length < (1 << 50)):
            raise LightingProtocolError("normal result lacks valid v0.2 length/light")


@dataclass(frozen=True, slots=True)
class PreparedLightingFace:
    points: tuple[tuple[float, float, float], ...]
    rgb: tuple[int, int, int]
    face_id: int
    request: LightingRequest | None


@dataclass(frozen=True, slots=True)
class SelectedFaceColor:
    color_bgr8: tuple[int, int, int]
    fallback_reason: str | None
    light_intensity: float = 1.0


def prepare_lighting_face(points, rgb, face_id) -> PreparedLightingFace:
    """Out-of-range finite coordinates are classified before any transmission."""
    _rgb_and_id(rgb, face_id)
    if (len(points) != 3 or any(len(p) != 3 for p in points)
            or any(type(v) is not float or not math.isfinite(v) for p in points for v in p)):
        raise ValueError("three finite Python-float camera points required")
    points, rgb = tuple(tuple(p) for p in points), tuple(rgb)
    # All nine coordinates are already finite Python floats. Keep the public
    # quantizer as an independent oracle instead of repeating its validation.
    # Finish one point before the next: an earlier out-of-range point must still
    # classify as fallback before a later finite multiplication can overflow.
    quantized = []
    for point in points:
        x = round(point[0] * CAMERA_SCALE)
        y = round(point[1] * CAMERA_SCALE)
        z = round(point[2] * CAMERA_SCALE)
        if not (-(1 << 23) <= x < (1 << 23)
                and -(1 << 23) <= y < (1 << 23)
                and -(1 << 23) <= z < (1 << 23)):
            return PreparedLightingFace(points, rgb, face_id, None)
        quantized.append((x, y, z))
    return PreparedLightingFace(points, rgb, face_id,
        LightingRequest(tuple(quantized), rgb, face_id))


def _requests(requests):
    requests = tuple(requests)
    if not 1 <= len(requests) <= 65536 or any(type(r) is not LightingRequest for r in requests):
        raise ValueError("nonempty batch of at most 65536 LightingRequest objects required")
    if len({r.face_id for r in requests}) != len(requests):
        raise ValueError("duplicate face_id in one batch")
    return requests


def pack_input_batch(requests) -> bytes:
    requests = _requests(requests)
    payload = bytearray()
    for request in requests:
        origin, first, second = request.points
        # Validated fixed-width fields do not overlap. Mask every signed axis
        # before shifting; keep the same packed 27+3+2 byte wire ABI.
        word = ((origin[0] & 0xffffff)
            | ((origin[1] & 0xffffff) << 24)
            | ((origin[2] & 0xffffff) << 48)
            | ((first[0] & 0xffffff) << 72)
            | ((first[1] & 0xffffff) << 96)
            | ((first[2] & 0xffffff) << 120)
            | ((second[0] & 0xffffff) << 144)
            | ((second[1] & 0xffffff) << 168)
            | ((second[2] & 0xffffff) << 192)
            | (request.rgb[0] << 216)
            | (request.rgb[1] << 224)
            | (request.rgb[2] << 232)
            | (request.face_id << 240))
        payload.extend(word.to_bytes(32, "little"))
    return bytes(payload)


def software_reference_output_batch(payload: bytes) -> bytes:
    """HOST integer oracle, not a DMA driver or actual RTL result."""
    if type(payload) is not bytes or not payload or len(payload) % 32:
        raise ValueError("whole 32-byte input faces required")
    requests = []
    for offset in range(0, len(payload), 32):
        word = int.from_bytes(payload[offset:offset + 32], "little")
        coords = []
        for axis in range(9):
            value = (word >> (24 * axis)) & 0xffffff
            coords.append(value - (1 << 24) if value & (1 << 23) else value)
        requests.append(LightingRequest(tuple(tuple(coords[i:i + 3]) for i in (0, 3, 6)),
            tuple((word >> (216 + 8 * i)) & 255 for i in range(3)), word >> 240))
    result = bytearray()
    for request in _requests(requests):
        golden = triangle_lighting_fixed(request.points, request.rgb)
        word = sum(v << (8 * i) for i, v in enumerate(golden.color_bgr8))
        word |= golden.light_q15 << 24 | request.face_id << 40 | int(golden.degenerate) << 56
        word |= (0 if golden.degenerate else golden.length_floor) << 58
        result.extend(word.to_bytes(16, "little"))
    return bytes(result)


def decode_output_batch(payload: bytes, requests) -> tuple[LightingResponse, ...]:
    requests = _requests(requests)
    if type(payload) is not bytes or len(payload) != 16 * len(requests):
        raise LightingProtocolError("exact 16-byte result count required; no padding/truncation")
    results = []
    for index, request in enumerate(requests):
        word = int.from_bytes(payload[index * 16:(index + 1) * 16], "little")
        if word >> 110 or (word >> 57) & 1:
            raise LightingProtocolError("reserved bits or AXIS protocol_error set")
        result = LightingResponse((word & 255, (word >> 8) & 255, (word >> 16) & 255),
            (word >> 24) & 0xffff, (word >> 40) & 0xffff, bool((word >> 56) & 1),
            (word >> 58) & ((1 << 52) - 1))
        if result.face_id != request.face_id:
            raise LightingProtocolError("face_id/order mismatch")
        results.append(result)
    return tuple(results)


def normal_quantization_budget(request: LightingRequest) -> int:
    """Cross-error bound without recalculating cross products or sqrt on PS."""
    origin, first, second = request.points
    if type(request) is LightingRequest:
        # Its frozen constructor has already validated exact signed24 ints.
        # Keep first-then-second, axis 0..2 evaluation order; do not relax
        # the bound or redo cross products/sqrt to choose PL versus CPU.
        return 2 * (abs(first[0] - origin[0]) + abs(first[1] - origin[1])
            + abs(first[2] - origin[2]) + abs(second[0] - origin[0])
            + abs(second[1] - origin[1]) + abs(second[2] - origin[2])) + 16
    return 2 * sum(abs(p[axis] - origin[axis])
        for p in (first, second) for axis in range(3)) + 16


def select_face_color(face: PreparedLightingFace,
        result: LightingResponse | None) -> SelectedFaceColor:
    """Use the PL color only inside its explicit <=1-channel-step face budget."""
    if face.request is None:
        if result is not None:
            raise LightingProtocolError("unexpected result for a non-transmitted face")
        reason = "camera_out_of_range"
    else:
        if type(result) is not LightingResponse or result.face_id != face.face_id:
            raise LightingProtocolError("missing/mismatched decoded result")
        if result.degenerate:
            reason = "quantized_degenerate"
        elif result.normal_length <= NORMAL_BUDGET_FACTOR * normal_quantization_budget(face.request):
            reason = "normal_quantization_budget"
        else:
            return SelectedFaceColor(result.color_bgr8, None, result.light_q15 / LIGHT_SCALE)
    # Imports are lazy: the codec/reference has no NumPy/OpenCV dependency.
    from .avatar_display import _triangle_light_intensity
    from .comparison_display import OpenCVComparisonRenderer
    depth = (face.points[0][2] + face.points[1][2] + face.points[2][2]) / 3.0
    light = _triangle_light_intensity(face.points)
    color = OpenCVComparisonRenderer._triangle_fill_color(depth, light, face.rgb)
    return SelectedFaceColor(tuple(color), reason, light)
