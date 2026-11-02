"""Small, model-independent 2D geometry helpers."""

from __future__ import annotations

import math

from .contracts import Keypoint


def to_isotropic_image_coordinates(
    point: Keypoint,
    source_width: int,
    source_height: int,
) -> Keypoint:
    """Scale normalized image axes equally without exceeding the unit range."""

    if source_width < 1 or source_height < 1:
        raise ValueError("source dimensions must be positive")
    common_extent = max(source_width, source_height)
    return Keypoint(
        x=point.x * source_width / common_extent,
        y=point.y * source_height / common_extent,
        confidence=point.confidence,
    )


def distance(a: Keypoint, b: Keypoint) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def joint_angle_degrees(a: Keypoint, vertex: Keypoint, c: Keypoint) -> float | None:
    """Return the smaller angle A-vertex-C in degrees, or None for a zero vector."""

    ax = a.x - vertex.x
    ay = a.y - vertex.y
    cx = c.x - vertex.x
    cy = c.y - vertex.y
    magnitude_product = math.hypot(ax, ay) * math.hypot(cx, cy)
    if magnitude_product <= 1e-12:
        return None
    cosine = (ax * cx + ay * cy) / magnitude_product
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def inclination_from_vertical_degrees(top: Keypoint, bottom: Keypoint) -> float | None:
    dx = abs(top.x - bottom.x)
    dy = abs(top.y - bottom.y)
    if dx <= 1e-12 and dy <= 1e-12:
        return None
    return math.degrees(math.atan2(dx, dy))


def minimum_confidence(*points: Keypoint) -> float:
    return min(point.confidence for point in points)
