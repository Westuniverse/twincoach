"""PL显示光照候选的整数黄金；不接生产渲染、姿态时间轴或评分。"""
from __future__ import annotations

from dataclasses import dataclass
import math

CAMERA_FRAC_BITS = 22
CAMERA_SCALE = 1 << CAMERA_FRAC_BITS
LIGHT_SCALE = 1 << 15
LIGHT_VECTOR_Q15 = (-11469, 18022, -24904)
AMBIENT_Q15 = 9175
DIFFUSE_WEIGHT_Q15 = 23593
DEGENERATE_Q15 = 11469
# n is in Q44; n^2 <= (1e-12)^2 * 2^88, computed without float.
DEGENERATE_SQUARED_MAX = (1 << 88) // 10**24


def divide_round_even(numerator: int, denominator: int) -> int:
    if type(numerator) is not int or type(denominator) is not int or denominator <= 0:
        raise ValueError("integer numerator and positive integer denominator required")
    magnitude, remainder = divmod(abs(numerator), denominator)
    magnitude += int(2 * remainder > denominator
        or (2 * remainder == denominator and magnitude & 1))
    return -magnitude if numerator < 0 else magnitude


def quantize_camera_point(point) -> tuple[int, int, int]:
    if len(point) != 3 or any(type(value) is not float or not math.isfinite(value) for value in point):
        raise ValueError("camera point must contain three finite Python floats")
    quantized = tuple(round(value * CAMERA_SCALE) for value in point)
    if any(value < -(1 << 23) or value >= (1 << 23) for value in quantized):
        raise ValueError("camera point must fit signed Q1.22 without saturation")
    return quantized


@dataclass(frozen=True, slots=True)
class FixedTriangleLighting:
    light_q15: int
    color_bgr8: tuple[int, int, int]
    degenerate: bool
    normal: tuple[int, int, int]
    normal_squared: int
    length_floor: int


def triangle_lighting_fixed(points, base_color_rgb=(116, 154, 38)) -> FixedTriangleLighting:
    """24-bit Q1.22相机XYZ → Q0.15光照与RGB8，保留双面法线和深度提示。

    与float64是不同的显式数值契约；先表征误差，不能声称原BGR逐字节相同。
    sqrt采用整数floor；除法为nearest/even；深度提示采用精确3/10的整数有理式。
    """
    if (len(points) != 3 or any(len(point) != 3 for point in points)
            or any(type(value) is not int or value < -(1 << 23) or value >= (1 << 23)
                for point in points for value in point)):
        raise ValueError("three signed 24-bit integer camera points required")
    if (len(base_color_rgb) != 3 or any(type(value) is not int or not 0 <= value <= 255
            for value in base_color_rgb)):
        raise ValueError("three RGB8 integers required")
    origin, first, second = points
    a = tuple(first[axis] - origin[axis] for axis in range(3))
    b = tuple(second[axis] - origin[axis] for axis in range(3))
    normal = (a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
    squared = sum(value * value for value in normal)
    length = math.isqrt(squared)
    degenerate = squared <= DEGENERATE_SQUARED_MAX
    if degenerate:
        light = DEGENERATE_Q15
    else:
        dot = abs(sum(value * coefficient for value, coefficient in zip(normal, LIGHT_VECTOR_Q15)))
        diffuse = divide_round_even(dot, length)
        light = min(LIGHT_SCALE, AMBIENT_Q15
            + divide_round_even(DIFFUSE_WEIGHT_Q15 * diffuse, LIGHT_SCALE))
    # Original depth = sum(z)/3; normalized=(depth+1)/2, clamped. Keep the
    # sum instead of rounding an intermediate average: shade=(17Q-sum(z))/(20Q).
    depth_sum = max(-3 * CAMERA_SCALE, min(3 * CAMERA_SCALE, sum(point[2] for point in points)))
    shade_numerator = 17 * CAMERA_SCALE - depth_sum
    color = tuple(divide_round_even(channel * light * shade_numerator,
        LIGHT_SCALE * 20 * CAMERA_SCALE) for channel in reversed(base_color_rgb))
    return FixedTriangleLighting(light, color, degenerate, normal, squared, length)
