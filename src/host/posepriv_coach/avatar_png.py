"""仅用标准库把数字人投影三角形栅格化为 PNG。"""

from __future__ import annotations

import binascii
import struct
from typing import Sequence
import zlib

from .avatar_display import AvatarDisplayState
from .avatar_svg import triangle_rgb_color


def render_avatar_views_png(
    states: Sequence[AvatarDisplayState],
    *,
    panel_gap: int = 16,
) -> bytes:
    """以 painter 顺序生成无需 OpenCV/Pillow 的 RGB PNG 验收图。"""

    if not states:
        raise ValueError("至少需要一个数字人视图")
    if panel_gap < 0:
        raise ValueError("panel_gap 不能为负数")
    panel_width = states[0].viewport_width
    height = states[0].viewport_height
    if any(
        state.viewport_width != panel_width or state.viewport_height != height
        for state in states
    ):
        raise ValueError("所有数字人视图必须使用相同尺寸")
    width = len(states) * panel_width + (len(states) - 1) * panel_gap
    pixels = bytearray((13, 18, 28) * (width * height))
    for panel_index, state in enumerate(states):
        offset_x = panel_index * (panel_width + panel_gap)
        _fill_rectangle(
            pixels,
            width,
            height,
            offset_x + 1,
            1,
            offset_x + panel_width - 2,
            height - 2,
            (15, 24, 35),
        )
        for triangle in sorted(
            state.projected_triangles,
            key=lambda item: item.depth,
            reverse=True,
        ):
            points = tuple((offset_x + x, y) for x, y in triangle.vertices)
            _fill_triangle(
                pixels,
                width,
                height,
                points,  # type: ignore[arg-type]
                triangle_rgb_color(triangle),
            )
    return _encode_png(width, height, pixels)


def _fill_rectangle(
    pixels: bytearray,
    width: int,
    height: int,
    left: int,
    top: int,
    right: int,
    bottom: int,
    color: tuple[int, int, int],
) -> None:
    for y in range(max(0, top), min(height - 1, bottom) + 1):
        row_start = (y * width + max(0, left)) * 3
        count = max(0, min(width - 1, right) - max(0, left) + 1)
        pixels[row_start : row_start + count * 3] = bytes(color) * count


def _fill_triangle(
    pixels: bytearray,
    width: int,
    height: int,
    points: tuple[tuple[int, int], tuple[int, int], tuple[int, int]],
    color: tuple[int, int, int],
) -> None:
    first, second, third = points
    area = _edge(first, second, float(third[0]), float(third[1]))
    if abs(area) < 1.0e-9:
        return
    minimum_x = max(0, min(point[0] for point in points))
    maximum_x = min(width - 1, max(point[0] for point in points))
    minimum_y = max(0, min(point[1] for point in points))
    maximum_y = min(height - 1, max(point[1] for point in points))
    positive = area > 0.0
    for y in range(minimum_y, maximum_y + 1):
        sample_y = y + 0.5
        for x in range(minimum_x, maximum_x + 1):
            sample_x = x + 0.5
            edges = (
                _edge(second, third, sample_x, sample_y),
                _edge(third, first, sample_x, sample_y),
                _edge(first, second, sample_x, sample_y),
            )
            inside = all(value >= 0.0 for value in edges) if positive else all(
                value <= 0.0 for value in edges
            )
            if inside:
                offset = (y * width + x) * 3
                pixels[offset : offset + 3] = bytes(color)


def _edge(
    first: tuple[int, int],
    second: tuple[int, int],
    x: float,
    y: float,
) -> float:
    return (second[0] - first[0]) * (y - first[1]) - (
        second[1] - first[1]
    ) * (x - first[0])


def _encode_png(width: int, height: int, pixels: bytearray) -> bytes:
    stride = width * 3
    raw = b"".join(
        b"\x00" + bytes(pixels[row * stride : (row + 1) * stride])
        for row in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, level=6))
        + _png_chunk(b"IEND", b"")
    )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


__all__ = ["render_avatar_views_png"]
