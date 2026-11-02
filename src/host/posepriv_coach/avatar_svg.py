"""不依赖 OpenCV 的数字人多视角 SVG 验收渲染。"""

from __future__ import annotations

from html import escape
from typing import Sequence

from .avatar_display import AvatarDisplayState, ProjectedTriangle


def render_avatar_views_svg(
    states: Sequence[AvatarDisplayState],
    *,
    titles: Sequence[str] | None = None,
    panel_gap: int = 16,
) -> str:
    """把已经蒙皮和投影的多个观察角度写成独立 SVG 面板。"""

    if not states:
        raise ValueError("至少需要一个数字人视图")
    if panel_gap < 0:
        raise ValueError("panel_gap 不能为负数")
    width = states[0].viewport_width
    height = states[0].viewport_height
    if any(
        state.viewport_width != width or state.viewport_height != height
        for state in states
    ):
        raise ValueError("所有数字人视图必须使用相同尺寸")
    labels = tuple(titles) if titles is not None else tuple(
        f"视角 {index + 1}" for index in range(len(states))
    )
    if len(labels) != len(states):
        raise ValueError("titles 数量必须与视图数量一致")

    total_width = len(states) * width + (len(states) - 1) * panel_gap
    elements = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_width}" '
            f'height="{height}" viewBox="0 0 {total_width} {height}">'
        ),
        f'<rect width="{total_width}" height="{height}" fill="#0d121c"/>',
    ]
    for panel_index, (state, title) in enumerate(zip(states, labels, strict=True)):
        offset_x = panel_index * (width + panel_gap)
        elements.extend(_render_panel(state, title, offset_x))
    elements.append("</svg>")
    return "\n".join(elements) + "\n"


def _render_panel(
    state: AvatarDisplayState,
    title: str,
    offset_x: int,
) -> list[str]:
    width = state.viewport_width
    height = state.viewport_height
    elements = [
        (
            f'<rect x="{offset_x + 1}" y="1" width="{width - 2}" height="{height - 2}" '
            'rx="8" fill="#0f1823" stroke="#4b6e87"/>'
        ),
    ]
    for triangle in sorted(
        state.projected_triangles,
        key=lambda item: item.depth,
        reverse=True,
    ):
        points = " ".join(
            f"{offset_x + x},{y}" for x, y in triangle.vertices
        )
        elements.append(
            f'<polygon points="{points}" fill="{triangle_hex_color(triangle)}"/>'
        )
    if not state.projected_triangles:
        for segment in state.projected_segments:
            elements.append(
                f'<line x1="{offset_x + segment.start[0]}" y1="{segment.start[1]}" '
                f'x2="{offset_x + segment.end[0]}" y2="{segment.end[1]}" '
                'stroke="#4cde9d" stroke-width="8" stroke-linecap="round"/>'
            )
    elements.extend(
        [
            (
                f'<text x="{offset_x + 18}" y="30" fill="#edf4fb" '
                f'font-family="sans-serif" font-size="18">{escape(title)}</text>'
            ),
            (
                f'<text x="{offset_x + 18}" y="{height - 18}" fill="#a9c3d9" '
                'font-family="sans-serif" font-size="13">'
                f'顶点 {state.skin_vertex_count} · 三角形 {len(state.projected_triangles)} · '
                f'骨段 {state.valid_bone_count}/{state.total_bone_count}</text>'
            ),
        ]
    )
    return elements


def triangle_rgb_color(triangle: ProjectedTriangle) -> tuple[int, int, int]:
    normalized_depth = max(0.0, min(1.0, (triangle.depth + 1.0) * 0.5))
    light = (
        (1.0 - 0.30 * normalized_depth)
        * max(0.18, min(1.0, triangle.light_intensity))
    )
    red = round(triangle.base_color_rgb[0] * light)
    green = round(triangle.base_color_rgb[1] * light)
    blue = round(triangle.base_color_rgb[2] * light)
    return red, green, blue


def triangle_hex_color(triangle: ProjectedTriangle) -> str:
    red, green, blue = triangle_rgb_color(triangle)
    return f"#{red:02x}{green:02x}{blue:02x}"


__all__ = ["render_avatar_views_svg", "triangle_hex_color", "triangle_rgb_color"]
