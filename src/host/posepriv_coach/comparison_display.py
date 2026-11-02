"""真人画面与精细 2.5D 数字人并排显示。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from pathlib import Path
from time import monotonic_ns
from typing import Any, Callable

from .avatar_display import AvatarDisplayModel, AvatarDisplayState, AvatarSnapshotRequest
from .contracts import CameraView, PoseFrame
from .display import CoachDisplayState, completed_score_text, completed_components_text
from .skeleton_display import SKELETON_EDGES, SkeletonDisplayError
from .text_overlay import OptionalCjkTextOverlay, OverlayText


_MESH_ANTIALIAS_TRIANGLE_LIMIT = 256


def _p95_nearest_rank(samples: deque[int]) -> int | None:
    if not samples:
        return None
    ordered = sorted(samples)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def _percent_text(value: float | None) -> str:
    return "--" if value is None else f"{value:.0f}%"


@dataclass(frozen=True, slots=True)
class ComparisonRenderStats:
    """对比画布从合成开始到 sink 返回的实际耗时摘要。"""

    frames: int
    total_ns: int
    max_ns: int
    latency_window_size: int = 0
    latency_sample_count: int = 0
    p95_ns: int | None = None
    compose_total_ns: int = 0
    compose_max_ns: int = 0
    compose_p95_ns: int | None = None
    sink_total_ns: int = 0
    sink_max_ns: int = 0
    sink_p95_ns: int | None = None

    @property
    def mean_ms(self) -> float | None:
        return None if self.frames == 0 else self.total_ns / self.frames / 1_000_000

    @property
    def p95_ms(self) -> float | None:
        return None if self.p95_ns is None else self.p95_ns / 1_000_000

    @property
    def compose_mean_ms(self) -> float | None:
        return None if self.frames == 0 else self.compose_total_ns / self.frames / 1_000_000

    @property
    def compose_p95_ms(self) -> float | None:
        return None if self.compose_p95_ns is None else self.compose_p95_ns / 1_000_000

    @property
    def sink_mean_ms(self) -> float | None:
        return None if self.frames == 0 else self.sink_total_ns / self.frames / 1_000_000

    @property
    def sink_p95_ms(self) -> float | None:
        return None if self.sink_p95_ns is None else self.sink_p95_ns / 1_000_000


class OpenCVComparisonRenderer:
    """Render an opt-in live preview beside a filled, privacy-safe avatar."""

    def __init__(
        self,
        *,
        width: int = 1280,
        height: int = 720,
        minimum_confidence: float = 0.5,
        window_name: str = "PosePriv-Coach Comparison",
        cv2_module: Any | None = None,
        numpy_module: Any | None = None,
        frame_sink: Callable[[Any], None] | None = None,
        avatar_model: AvatarDisplayModel | None = None,
        clock_ns: Callable[[], int] = monotonic_ns,
        latency_window_size: int = 1024,
        show_avatar_rig: bool = False,
        cjk_font_path: str | Path | None = None,
        triangle_painter: Callable[[Any, Any, Any], None] | None = None,
        window_presenter: Callable[[str, Any], None] | None = None,
        lighting_backend: Any | None = None,
        preview_resizer: Callable[[Any, Any], None] | None = None,
        adaptive_camera_layout: bool = False,
        textured_triangle_painter=None,
        show_diagnostics: bool = False,
        avatar_frame_painter=None,
        batch_avatar_snapshot: bool = False,
    ) -> None:
        if width < 800 or height < 600:
            raise ValueError("comparison display requires at least 800x600")
        if not 0.0 <= minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence must be within [0, 1]")
        if latency_window_size <= 0:
            raise ValueError("latency_window_size must be positive")
        if window_presenter is not None and frame_sink is not None:
            raise ValueError("window_presenter requires the original HighGUI window sink")
        if lighting_backend is not None and not callable(getattr(lighting_backend, "shade", None)):
            raise TypeError("borrowed lighting backend with shade required")
        if preview_resizer is not None and not callable(preview_resizer):
            raise TypeError("borrowed callable preview resizer required")
        if avatar_frame_painter is not None:
            if not callable(avatar_frame_painter):
                raise TypeError("borrowed callable avatar frame painter required")
            if any(backend is not None for backend in (triangle_painter, textured_triangle_painter, lighting_backend)):
                raise ValueError("avatar frame painter requires exclusive surface rendering")
        if batch_avatar_snapshot and not getattr(avatar_frame_painter, "supports_batch_snapshot", False):
            raise ValueError("batch snapshot requires an explicit batch-capable frame painter")
        if cv2_module is None:
            try:
                import cv2 as cv2_module  # type: ignore[import-not-found, no-redef]
            except ImportError as exc:
                raise SkeletonDisplayError("对比界面需要 OpenCV") from exc
        if numpy_module is None:
            try:
                import numpy as numpy_module  # type: ignore[import-not-found, no-redef]
            except ImportError as exc:
                raise SkeletonDisplayError("对比界面需要 NumPy") from exc
        self.width = width
        self.height = height
        self.minimum_confidence = minimum_confidence
        self.window_name = window_name
        self._cv2 = cv2_module
        self._numpy = numpy_module
        self._frame_sink = frame_sink
        self._avatar_model = avatar_model
        self._triangle_painter = triangle_painter
        self._textured_triangle_painter = textured_triangle_painter
        self._avatar_frame_painter = avatar_frame_painter
        self._batch_avatar_snapshot = batch_avatar_snapshot
        self._window_presenter = window_presenter
        self._lighting_backend = lighting_backend
        # Borrowed synchronous CPU adapter. Its creator owns close/thread policy.
        self._preview_resizer = preview_resizer
        self._adaptive_camera_layout = adaptive_camera_layout
        self._show_diagnostics = show_diagnostics
        self._clock_ns = clock_ns
        self._avatar_rect = (0, 0, 0, 0)
        self._drag_button: int | None = None
        self._last_mouse: tuple[int, int] | None = None
        self._mouse_callback_registered = False
        self.close_requested = False
        self._closed = False
        self._render_frames = 0
        self._render_total_ns = 0
        self._render_max_ns = 0
        self._render_samples_ns: deque[int] = deque(maxlen=latency_window_size)
        self._compose_total_ns = 0
        self._compose_max_ns = 0
        self._compose_samples_ns: deque[int] = deque(maxlen=latency_window_size)
        self._sink_total_ns = 0
        self._sink_max_ns = 0
        self._sink_samples_ns: deque[int] = deque(maxlen=latency_window_size)
        self._show_avatar_rig = bool(show_avatar_rig)
        self._cjk_text = OptionalCjkTextOverlay(cjk_font_path, numpy_module=self._numpy)
        self._header_cache: Any | None = None
        self._preview_visible = False
        self._header_cache_key: tuple[int, int, bool] | None = None
        self._footer_label_cache: Any | None = None
        self._footer_label_cache_key: tuple[int, int, int] | None = None
        self._footer_value_cache: Any | None = None
        self._footer_value_cache_key: tuple[Any, ...] | None = None

    def update_pair(
        self,
        front: PoseFrame,
        side: PoseFrame,
        state: CoachDisplayState,
        *,
        front_preview: Any | None = None,
        side_preview: Any | None = None,
        avatar_state: AvatarDisplayState | AvatarSnapshotRequest | None = None,
    ) -> None:
        render_start_ns = self._clock_ns()
        if self._closed:
            raise SkeletonDisplayError("对比界面已经关闭")
        if front.view is not CameraView.FRONT or side.view is not CameraView.SIDE:
            raise ValueError("comparison renderer requires front and side poses")
        if self._lighting_backend is not None and not isinstance(avatar_state, AvatarSnapshotRequest):
            raise ValueError("lighting renderer requires a frozen avatar snapshot request")
        if isinstance(avatar_state, AvatarSnapshotRequest):
            if (avatar_state.viewport_width, avatar_state.viewport_height) != (self.width, self.height):
                raise ValueError("avatar snapshot request viewport does not match renderer")
            if not avatar_state.matches_pair(front, side):
                raise ValueError("avatar snapshot request does not match submitted pose pair")
            avatar_state = (avatar_state.resolve_batch() if self._batch_avatar_snapshot else
                            avatar_state.resolve() if self._lighting_backend is None
                            else avatar_state.resolve_with_lighting(self._lighting_backend))
        canvas = self._numpy.zeros((self.height, self.width, 3), dtype=self._numpy.uint8)
        # The existing OpenCV solid fill avoids slow per-pixel NumPy broadcast
        # on A53. Inclusive endpoints cover exactly this canvas, same BGR bytes.
        self._cv2.rectangle(
            canvas, (0, 0), (self.width - 1, self.height - 1), (13, 18, 28), -1
        )
        self._preview_visible = front_preview is not None or side_preview is not None
        self._draw_header(canvas)

        margin = 24
        gap = 18
        header_bottom = 64
        footer_top = self.height - 142
        content_height = footer_top - header_bottom - 12
        live_width = round((self.width - 2 * margin - gap) * 0.48)
        avatar_width = self.width - 2 * margin - gap - live_width
        live_rect = (margin, header_bottom, live_width, content_height)
        avatar_rect = (margin + live_width + gap, header_bottom, avatar_width, content_height)
        self._avatar_rect = avatar_rect

        self._draw_live_panel(canvas, live_rect, front, side, front_preview, side_preview)
        self._draw_avatar_panel(canvas, avatar_rect, front, side, avatar_state)
        self._draw_footer(canvas, state, footer_top)
        self._draw_cjk_overlay(canvas, state, footer_top, front=front, side=side, avatar_state=avatar_state)

        compose_complete_ns = self._clock_ns()
        if compose_complete_ns < render_start_ns:
            raise SkeletonDisplayError("display clock must be monotonic")

        if self._frame_sink is None:
            self._cv2.imshow(self.window_name, canvas)
            if self._window_presenter is not None:
                self._window_presenter(self.window_name, canvas)
            self._register_mouse_callback()
            self.poll_window_events()
        else:
            self._frame_sink(canvas)

        render_complete_ns = self._clock_ns()
        if render_complete_ns < compose_complete_ns:
            raise SkeletonDisplayError("display clock must be monotonic")
        compose_elapsed_ns = compose_complete_ns - render_start_ns
        sink_elapsed_ns = render_complete_ns - compose_complete_ns
        elapsed_ns = render_complete_ns - render_start_ns
        self._render_frames += 1
        self._render_total_ns += elapsed_ns
        self._render_max_ns = max(self._render_max_ns, elapsed_ns)
        self._render_samples_ns.append(elapsed_ns)
        self._compose_total_ns += compose_elapsed_ns
        self._compose_max_ns = max(self._compose_max_ns, compose_elapsed_ns)
        self._compose_samples_ns.append(compose_elapsed_ns)
        self._sink_total_ns += sink_elapsed_ns
        self._sink_max_ns = max(self._sink_max_ns, sink_elapsed_ns)
        self._sink_samples_ns.append(sink_elapsed_ns)

    def poll_window_events(self) -> None:
        """Keep main-thread HighGUI responsive even when an exact image is missing."""
        if self._closed:
            raise SkeletonDisplayError("对比界面已经关闭")
        if self._frame_sink is not None:
            return
        key = int(self._cv2.waitKey(1)) & 0xFF
        if key in (27, ord("q"), ord("Q")):
            self.close_requested = True
        elif key in (ord("r"), ord("R")):
            self._show_avatar_rig = not self._show_avatar_rig

    def stats(self) -> ComparisonRenderStats:
        """返回已完成画布的合成/写屏耗时，不含摄像头和模型推理。"""

        return ComparisonRenderStats(
            frames=self._render_frames,
            total_ns=self._render_total_ns,
            max_ns=self._render_max_ns,
            latency_window_size=self._render_samples_ns.maxlen or 0,
            latency_sample_count=len(self._render_samples_ns),
            p95_ns=_p95_nearest_rank(self._render_samples_ns),
            compose_total_ns=self._compose_total_ns,
            compose_max_ns=self._compose_max_ns,
            compose_p95_ns=_p95_nearest_rank(self._compose_samples_ns),
            sink_total_ns=self._sink_total_ns,
            sink_max_ns=self._sink_max_ns,
            sink_p95_ns=_p95_nearest_rank(self._sink_samples_ns),
        )

    def _register_mouse_callback(self) -> None:
        if self._mouse_callback_registered or self._avatar_model is None:
            return
        setter = getattr(self._cv2, "setMouseCallback", None)
        if not callable(setter):
            return
        setter(self.window_name, self._on_mouse)
        self._mouse_callback_registered = True

    def _on_mouse(self, event: int, x: int, y: int, flags: int, _param: Any = None) -> None:
        if self._avatar_model is None or not self._inside_avatar(x, y):
            # Do not turn a re-entry after leaving this panel into a large drag.
            self._drag_button = None
            self._last_mouse = None
            return
        cv2 = self._cv2
        left_down = getattr(cv2, "EVENT_LBUTTONDOWN", -1)
        right_down = getattr(cv2, "EVENT_RBUTTONDOWN", -1)
        left_up = getattr(cv2, "EVENT_LBUTTONUP", -1)
        right_up = getattr(cv2, "EVENT_RBUTTONUP", -1)
        move = getattr(cv2, "EVENT_MOUSEMOVE", -1)
        wheel = getattr(cv2, "EVENT_MOUSEWHEEL", -1)
        double_left = getattr(cv2, "EVENT_LBUTTONDBLCLK", -1)
        if event == left_down:
            self._drag_button = left_down
            self._last_mouse = (x, y)
        elif event == right_down:
            self._drag_button = right_down
            self._last_mouse = (x, y)
        elif event in (left_up, right_up):
            self._drag_button = None
            self._last_mouse = None
        elif event == double_left:
            self._drag_button = None
            self._last_mouse = None
            self._avatar_model.reset_camera()
        elif event == move and self._drag_button is not None and self._last_mouse is not None:
            previous_x, previous_y = self._last_mouse
            delta_x = x - previous_x
            delta_y = y - previous_y
            if self._drag_button == left_down:
                self._avatar_model.rotate(delta_x * 0.35, delta_y * 0.35)
            else:
                self._avatar_model.pan(delta_x, delta_y)
            self._last_mouse = (x, y)
        elif event == wheel:
            wheel_reader = getattr(cv2, "getMouseWheelDelta", None)
            if callable(wheel_reader):
                wheel_delta = wheel_reader(flags)
            else:
                # Python OpenCV may omit the helper; wheel delta is signed
                # high 16 bits, while buttons/modifiers occupy the low bits.
                wheel_delta = (flags >> 16) & 0xFFFF
                if wheel_delta >= 0x8000:
                    wheel_delta -= 0x10000
            if wheel_delta != 0:
                self._avatar_model.zoom_by(1.1 if wheel_delta > 0 else 0.9)

    def _inside_avatar(self, x: int, y: int) -> bool:
        left, top, width, height = self._avatar_rect
        return left <= x < left + width and top <= y < top + height

    def close(self) -> None:
        if not self._closed and self._frame_sink is None:
            self._cv2.destroyWindow(self.window_name)
        self._closed = True

    def _draw_header(self, canvas: Any) -> None:
        # Header is drawn before all dynamic content on the same solid backdrop.
        # Keep only its strip, never the sink-owned completed frame. CJK is still
        # applied after composition, and a size change invalidates this one cache.
        key = (self.width, self.height, self._preview_visible)
        if self._header_cache_key != key:
            self._draw_header_uncached(canvas)
            self._header_cache = canvas[:64].copy()
            self._header_cache_key = key
        else:
            canvas[:64] = self._header_cache

    def _draw_header_uncached(self, canvas: Any) -> None:
        cv2 = self._cv2
        cv2.putText(
            canvas,
            "POSEPRIV COACH",
            (24, 38),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (245, 248, 255),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            canvas,
            "LOCAL  |  CAMERA PREVIEW" if self._preview_visible else "LOCAL  |  SKELETON ONLY",
            (self.width - 350, 35),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (112, 214, 255),
            1,
            cv2.LINE_AA,
        )

    def _draw_panel_shell(self, canvas: Any, rect: tuple[int, int, int, int], title: str) -> None:
        x, y, width, height = rect
        cv2 = self._cv2
        cv2.rectangle(canvas, (x, y), (x + width, y + height), (50, 68, 89), -1)
        cv2.rectangle(canvas, (x, y), (x + width, y + height), (104, 132, 164), 1)
        cv2.rectangle(canvas, (x, y), (x + width, y + 40), (24, 34, 50), -1)
        cv2.putText(
            canvas,
            title,
            (x + 14, y + 27),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.62,
            (235, 241, 250),
            2,
            cv2.LINE_AA,
        )

    def _draw_live_panel(
        self,
        canvas: Any,
        rect: tuple[int, int, int, int],
        front_pose: PoseFrame,
        side_pose: PoseFrame,
        front_preview: Any | None,
        side_preview: Any | None,
    ) -> None:
        x, y, width, height = rect
        self._draw_panel_shell(canvas, rect, "CAMERA VIEWS")
        slots = self._live_view_slots(rect, front_pose, side_pose, adaptive=self._adaptive_camera_layout)
        for slot, (label, pose, preview) in zip(slots, (
            ("FRONT", front_pose, front_preview),
            ("SIDE", side_pose, side_preview),
        ), strict=True):
            sx, sy, sw, sh = slot
            content = (sx, sy + 22, sw, sh - 22)
            if preview is not None:
                image_rect = self._fit_image(canvas, preview, content)
                self._draw_pose_overlay(canvas, pose, image_rect)
            else:
                self._draw_preview_placeholder(canvas, content, pose)
            self._cv2.putText(
                canvas,
                label,
                (sx + 8, sy + 17),
                self._cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (255, 255, 255),
                1,
                self._cv2.LINE_AA,
            )

    @staticmethod
    def _live_view_slots(rect, front_pose, side_pose, *, adaptive=True):
        """Maximise fitted source area without cropping or changing pose geometry."""
        x, y, width, height = rect
        width, height = width - 6, height - 46
        x, y = x + 3, y + 43
        half_h, half_w = (height - 10) // 2, (width - 10) // 2
        stacked = ((x, y, width, half_h),
                   (x, y + half_h + 10, width, half_h))
        columns = ((x, y, half_w, height),
                   (x + half_w + 10, y, half_w, height))

        def fitted_area(slots):
            area = 0.0
            for slot, pose in zip(slots, (front_pose, side_pose), strict=True):
                scale = min(slot[2] / pose.source_width, (slot[3] - 22) / pose.source_height)
                area += pose.source_width * pose.source_height * scale * scale
            return area

        return columns if adaptive and fitted_area(columns) > fitted_area(stacked) else stacked

    def _draw_avatar_panel(
        self,
        canvas: Any,
        rect: tuple[int, int, int, int],
        front: PoseFrame,
        side: PoseFrame,
        avatar_state: AvatarDisplayState | None = None,
    ) -> None:
        x, y, width, height = rect
        title = ("VIDEO-DRIVEN AVATAR" if avatar_state is not None and avatar_state.pose_preview
                 else "3D AVATAR" if avatar_state is not None else "2D POSE AVATAR")
        self._draw_panel_shell(canvas, rect, title)
        content_y = y + 43
        content_height = height - 46
        if avatar_state is not None:
            self._draw_avatar_3d(
                canvas,
                (x + 8, content_y + 4, width - 16, content_height - 8),
                avatar_state,
            )
            return
        main_width = round(width * 0.68)
        self._draw_avatar(
            canvas,
            front,
            (x + 8, content_y + 4, main_width - 12, content_height - 8),
            accent=(86, 229, 151),
        )
        inset = (x + main_width, content_y + 26, width - main_width - 8, content_height - 52)
        self._cv2.rectangle(
            canvas,
            (inset[0], inset[1]),
            (inset[0] + inset[2], inset[1] + inset[3]),
            (75, 94, 116),
            1,
        )
        self._cv2.putText(
            canvas,
            "SIDE MODEL",
            (inset[0] + 8, inset[1] + 20),
            self._cv2.FONT_HERSHEY_SIMPLEX,
            0.38,
            (170, 192, 218),
            1,
            self._cv2.LINE_AA,
        )
        self._draw_avatar(canvas, side, inset, accent=(255, 177, 75), compact=True)

    def _draw_avatar_3d(
        self,
        canvas: Any,
        rect: tuple[int, int, int, int],
        state: AvatarDisplayState,
    ) -> None:
        """Draw the projected 3D skeleton without touching camera pixels."""

        x, y, width, height = rect
        cv2 = self._cv2
        cv2.rectangle(canvas, (x, y), (x + width, y + height), (15, 24, 35), -1)
        cv2.rectangle(canvas, (x, y), (x + width, y + height), (75, 110, 135), 1)
        scale_x = scale_y = min(width / max(1, state.viewport_width),
                                height / max(1, state.viewport_height))
        panel_x, panel_y = x, y
        x += round((width - state.viewport_width * scale_x) / 2)
        y += round((height - state.viewport_height * scale_y) / 2)

        def point(value: tuple[int, int]) -> tuple[int, int]:
            return x + round(value[0] * scale_x), y + round(value[1] * scale_y)

        # 正交视图仍需要 painter 顺序；远面先画，避免高密度网格按原始索引
        # 绘制时出现前后表面随机覆盖。深度来自同一观察相机，不参与评分。
        # 批量投影避免A53逐顶点写入NumPy小切片。float64 multiply+rint保持
        # 原Python round的ties-to-even；仍保留同一稳定painter顺序和逐面绘制。
        if self._avatar_frame_painter is not None:
            self._avatar_frame_painter(canvas, rect, state)
        else:
            if state.triangle_colors_bgr8:
                ordered = sorted(enumerate(state.projected_triangles), key=lambda item: item[1].depth,
                                 reverse=True)
                triangles = [triangle for _, triangle in ordered]
                polygons = self._project_triangle_polygons(triangles, scale_x, scale_y, x, y)
                colors = [list(state.triangle_colors_bgr8[index]) for index, _ in ordered]
                if getattr(self._triangle_painter, "accepts_numpy_colors", False) is True:
                    colors = self._numpy.asarray(colors, dtype=self._numpy.int32)
            else:
                triangles = sorted(state.projected_triangles, key=lambda item: item.depth, reverse=True)
                polygons = self._project_triangle_polygons(triangles, scale_x, scale_y, x, y)
                colors = self._triangle_fill_colors(triangles)
            triangle_count = len(triangles)
            # Dense opaque meshes have hundreds of shared interior edges. Applying
            # antialiasing independently to every face costs work and filters those
            # internal seams; keep AA for L0 while using integer fills for L1/L2.
            fill_line_type = self._triangle_fill_line_type(triangle_count)
            if state.texture is not None and triangle_count:
                if self._textured_triangle_painter is None:
                    raise ValueError("photographic avatar requires a texture painter")
                self._textured_triangle_painter(canvas, polygons, [t.uvs for t in triangles],
                                               [.90 + .10 * t.light_intensity for t in triangles], state.texture)
            elif self._triangle_painter is not None:
                self._triangle_painter(canvas, polygons, colors)
            else:
                for polygon, color in zip(polygons, colors):
                    cv2.fillConvexPoly(
                        canvas,
                        polygon,
                        color,
                        fill_line_type,
                    )
                    if triangle_count <= _MESH_ANTIALIAS_TRIANGLE_LIMIT:
                        cv2.polylines(canvas, (polygon,), True, (70, 168, 132), 1, cv2.LINE_AA)

        draw_rig = self._should_draw_avatar_rig(bool(state.projected_triangles))
        if draw_rig:
            endpoints: set[tuple[int, int]] = set()
            for segment in state.projected_segments:
                start = point(segment.start)
                end = point(segment.end)
                endpoints.add(start)
                endpoints.add(end)
                cv2.line(
                    canvas,
                    (start[0] + 3, start[1] + 4),
                    (end[0] + 3, end[1] + 4),
                    (5, 10, 17),
                    10,
                    cv2.LINE_AA,
                )
                cv2.line(canvas, start, end, (76, 222, 157), 6, cv2.LINE_AA)
                cv2.line(canvas, start, end, (209, 255, 231), 1, cv2.LINE_AA)
            for center in endpoints:
                cv2.circle(canvas, center, 5, (252, 194, 86), -1, cv2.LINE_AA)
                cv2.circle(canvas, center, 2, (255, 246, 195), -1, cv2.LINE_AA)

        def caption(text, position):
            scale = .42
            measured = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)[0][0]
            scale *= min(1., (width - 28) / max(1, measured))
            cv2.putText(canvas, text, position, cv2.FONT_HERSHEY_SIMPLEX,
                        scale, (189, 216, 238), 1, cv2.LINE_AA)

        if not self._show_diagnostics:
            return
        caption(
            "3D AVATAR  |  MOUSE: ORBIT / WHEEL: ZOOM / R: RIG",
            (panel_x + 12, panel_y + 25),
        )
        caption(
            f"BONES {state.valid_bone_count}/{state.total_bone_count}  "
            f"CONF {state.confidence_percent:.0f}%  VTX {state.skin_vertex_count}",
            (panel_x + 12, panel_y + height - 32),
        )
        ray_gap = (
            "--"
            if state.mean_ray_gap_world_units is None
            else f"{state.mean_ray_gap_world_units:.3f}"
        )
        ray_angle = (
            "--"
            if state.mean_ray_angle_degrees is None
            else f"{state.mean_ray_angle_degrees:.1f}D"
        )
        caption(
            "VIDEO POSE PREVIEW" if state.pose_preview else
            f"3D {state.valid_triangulated_point_count}/"
            f"{state.total_triangulated_point_count}  "
            f"OBS {_percent_text(state.observable_point_percent)}  "
            f"GEO {_percent_text(state.accepted_geometry_percent)}  "
            f"GAP {ray_gap}  ANG {ray_angle}",
            (panel_x + 12, panel_y + height - 14),
        )

    def _project_triangle_polygons(
        self, triangles: Any, scale_x: float, scale_y: float, x: int, y: int,
    ) -> Any:
        """只变换面板像素；不修改三维姿态、评分或蒙皮状态。"""

        numpy = self._numpy
        if not triangles:
            return numpy.empty((0, 3, 2), dtype=numpy.int32)
        pixels = numpy.fromiter(
            (value for triangle in triangles
             for ((ax, ay), (bx, by), (cx, cy)) in (triangle.vertices,)
             for value in (ax, ay, bx, by, cx, cy)),
            dtype=numpy.float64, count=6 * len(triangles),
        ).reshape((-1, 3, 2))
        pixels[:, :, 0] *= scale_x
        pixels[:, :, 1] *= scale_y
        numpy.rint(pixels, out=pixels)
        pixels[:, :, 0] += x
        pixels[:, :, 1] += y
        # NumPy无效/越界cast可能仅警告后返回INT_MIN，不能静默画错位置。
        if (not numpy.isfinite(pixels).all()
                or pixels.min() < -2147483648 or pixels.max() > 2147483647):
            raise SkeletonDisplayError("projected triangle pixels must fit signed int32")
        return pixels.astype(numpy.int32)

    def _triangle_fill_colors(self, triangles: Any) -> Any:
        """批量计算 RGB8 面色；标量函数保留为位精确舍入参考。"""

        if not triangles:
            return []
        numpy = self._numpy
        # fromiter avoids a Python per-element NumPy assignment/conversion.
        # ProjectedTriangle already validates finite depth/light and RGB8.
        values = numpy.fromiter(
            (value for triangle in triangles for value in (
                triangle.depth, triangle.light_intensity, *triangle.base_color_rgb[::-1]
            )), dtype=numpy.float64, count=5 * len(triangles),
        ).reshape((-1, 5))
        normalized = numpy.clip((values[:, 0] + 1.0) * 0.5, 0.0, 1.0)
        light = (1.0 - 0.30 * normalized) * numpy.clip(values[:, 1], 0.18, 1.0)
        # Separate float64 operations match scalar evaluation, including exact
        # half ties. Only an explicitly compatible painter receives the array;
        # default OpenCV and custom painters keep their Python integer lists.
        colors = numpy.rint(values[:, 2:] * light[:, None]).astype(numpy.int32)
        return colors if getattr(self._triangle_painter, "accepts_numpy_colors", False) is True else colors.tolist()

    @staticmethod
    def _triangle_fill_color(
        depth: float,
        light_intensity: float = 1.0,
        base_color_rgb: tuple[int, int, int] = (116, 154, 38),
    ) -> tuple[int, int, int]:
        """组合观察深度与面法线光照，不引入材质或额外渲染线程。"""

        # ``depth`` is the camera-space yaw depth after model fitting.  Clamp it
        # because noisy triangulation may place a vertex just outside [-1, 1].
        normalized = max(0.0, min(1.0, (depth + 1.0) * 0.5))
        light = (1.0 - 0.30 * normalized) * max(0.18, min(1.0, light_intensity))
        red, green, blue = base_color_rgb
        return round(blue * light), round(green * light), round(red * light)

    def _should_draw_avatar_rig(self, has_mesh: bool) -> bool:
        return self._show_avatar_rig or not has_mesh

    def _triangle_fill_line_type(self, triangle_count: int) -> int:
        return (
            self._cv2.LINE_AA
            if triangle_count <= _MESH_ANTIALIAS_TRIANGLE_LIMIT
            else self._cv2.LINE_8
        )

    def _draw_avatar(
        self,
        canvas: Any,
        pose: PoseFrame,
        rect: tuple[int, int, int, int],
        *,
        accent: tuple[int, int, int],
        compact: bool = False,
    ) -> None:
        cv2 = self._cv2
        points = self._pose_points(pose, rect)
        left_shoulder = points.get("left_shoulder")
        right_shoulder = points.get("right_shoulder")
        left_hip = points.get("left_hip")
        right_hip = points.get("right_hip")
        if all(point is not None for point in (left_shoulder, right_shoulder, left_hip, right_hip)):
            torso = self._numpy.array(
                [left_shoulder, right_shoulder, right_hip, left_hip],
                dtype=self._numpy.int32,
            )
            shadow = torso + self._numpy.array((4, 5), dtype=self._numpy.int32)
            cv2.fillConvexPoly(canvas, shadow, (10, 14, 22), cv2.LINE_AA)
            cv2.fillConvexPoly(canvas, torso, (48, 116, 103), cv2.LINE_AA)
            cv2.polylines(canvas, (torso,), True, accent, 2, cv2.LINE_AA)

        thickness = 9 if compact else 15
        for first, second in SKELETON_EDGES:
            point_a = points.get(first)
            point_b = points.get(second)
            if point_a is None or point_b is None:
                continue
            cv2.line(canvas, point_a, point_b, (8, 13, 20), thickness + 8, cv2.LINE_AA)
            cv2.line(canvas, point_a, point_b, accent, thickness, cv2.LINE_AA)
            cv2.line(canvas, point_a, point_b, (205, 255, 230), 2, cv2.LINE_AA)

        for name, center in points.items():
            if name.startswith(("left_", "right_")):
                cv2.circle(canvas, center, max(4, thickness // 2), (238, 211, 125), -1, cv2.LINE_AA)

        shoulders = [point for point in (left_shoulder, right_shoulder) if point is not None]
        if shoulders:
            center_x = round(sum(point[0] for point in shoulders) / len(shoulders))
            shoulder_y = round(sum(point[1] for point in shoulders) / len(shoulders))
            shoulder_span = (
                abs(shoulders[1][0] - shoulders[0][0]) if len(shoulders) == 2 else rect[2] // 5
            )
            radius = max(10, min(28, shoulder_span // 4))
            head_center = (center_x, shoulder_y - radius - max(8, radius // 2))
            cv2.circle(canvas, (head_center[0] + 4, head_center[1] + 5), radius + 2, (8, 13, 20), -1, cv2.LINE_AA)
            cv2.circle(canvas, head_center, radius, (205, 188, 146), -1, cv2.LINE_AA)
            cv2.circle(canvas, head_center, radius, accent, 2, cv2.LINE_AA)
            if not compact:
                cv2.circle(canvas, (head_center[0] - radius // 3, head_center[1] - 2), 2, (35, 46, 58), -1, cv2.LINE_AA)
                cv2.circle(canvas, (head_center[0] + radius // 3, head_center[1] - 2), 2, (35, 46, 58), -1, cv2.LINE_AA)

    def _pose_points(
        self,
        pose: PoseFrame,
        rect: tuple[int, int, int, int],
    ) -> dict[str, tuple[int, int]]:
        x, y, width, height = rect
        points = {}
        for name, keypoint in pose.keypoints.items():
            if keypoint.confidence < self.minimum_confidence:
                continue
            points[name] = (
                x + round(keypoint.x * width),
                y + round(keypoint.y * height),
            )
        return points

    def _draw_pose_overlay(
        self,
        canvas: Any,
        pose: PoseFrame,
        rect: tuple[int, int, int, int],
    ) -> None:
        # POSE-FRAME coordinates are already normalized to the source image.
        # Clip raster drawing to the fitted image, never to the whole panel.
        x, y, width, height = rect
        image = canvas[y:y + height, x:x + width]
        points = {
            name: (min(width - 1, round(point.x * width)),
                   min(height - 1, round(point.y * height)))
            for name, point in pose.keypoints.items()
            if point.confidence >= self.minimum_confidence
            and math.isfinite(point.x) and math.isfinite(point.y)
            and 0.0 <= point.x <= 1.0 and 0.0 <= point.y <= 1.0
        }
        for first, second in SKELETON_EDGES:
            if first in points and second in points:
                self._cv2.line(image, points[first], points[second], (70, 235, 155), 2, self._cv2.LINE_AA)
        for center in points.values():
            self._cv2.circle(image, center, 3, (50, 196, 255), -1, self._cv2.LINE_AA)

    def _draw_preview_placeholder(
        self,
        canvas: Any,
        rect: tuple[int, int, int, int],
        pose: PoseFrame,
    ) -> None:
        x, y, width, height = rect
        self._cv2.rectangle(canvas, (x, y), (x + width, y + height), (23, 30, 42), -1)
        self._draw_avatar(canvas, pose, rect, accent=(95, 116, 139))
        self._cv2.putText(
            canvas,
            "SKELETON ONLY",
            (x + 16, y + height - 18),
            self._cv2.FONT_HERSHEY_SIMPLEX,
            0.46,
            (155, 174, 195),
            1,
            self._cv2.LINE_AA,
        )

    def _fit_image(
        self,
        canvas: Any,
        image: Any,
        rect: tuple[int, int, int, int],
    ) -> tuple[int, int, int, int]:
        x, y, width, height = rect
        shape = getattr(image, "shape", ())
        if (len(shape) != 3 or shape[2] != 3 or shape[0] <= 0 or shape[1] <= 0
                or getattr(image, "dtype", None) != self._numpy.uint8):
            raise SkeletonDisplayError("camera preview requires a nonempty BGR8 image")
        source_height, source_width = image.shape[:2]
        scale = min(width / source_width, height / source_height)
        target_width = max(1, round(source_width * scale))
        target_height = max(1, round(source_height * scale))
        left = x + (width - target_width) // 2
        top = y + (height - target_height) // 2
        if self._preview_resizer is None:
            resized = self._cv2.resize(image, (target_width, target_height))
            canvas[top : top + target_height, left : left + target_width] = resized
        else:
            self._preview_resizer(image, canvas[top : top + target_height, left : left + target_width])
        return left, top, target_width, target_height

    def _draw_footer(self, canvas: Any, state: CoachDisplayState, top: int) -> None:
        if not self._show_diagnostics:
            return self._draw_feedback_footer(canvas,state,top)
        cv2 = self._cv2
        cv2.rectangle(canvas, (0, top), (self.width, self.height), (18, 26, 40), -1)
        score_available = state.score_valid and state.conformance_percent is not None
        score = f"{state.conformance_percent:.0f}" if score_available else "--"
        items = (
            ("PHASE", state.phase.value.upper()),
            ("REPS", str(state.repetition_count)),
            ("SCORE", score),
            ("CONF", "--" if state.confidence_percent is None else f"{state.confidence_percent:.0f}%"),
            ("COVER", "--" if state.coverage_percent is None else f"{state.coverage_percent:.0f}%"),
            ("PAIR FPS", "--" if state.synchronized_pair_fps is None else f"{state.synchronized_pair_fps:.1f}"),
            ("SYNC", "--" if state.current_skew_ms is None else f"{state.current_skew_ms:.1f}ms"),
            ("E2E P95", "--" if state.p95_end_to_end_ms is None else f"{state.p95_end_to_end_ms:.1f}ms"),
        )
        columns = 8 if self.width >= 1200 else 4
        cell_width = self.width // columns
        positions = [(i % columns * cell_width + 18,
                      top + (i // columns * 43 if columns == 4 else 18))
                     for i in range(len(items))]
        label_key = (self.width, self.height, top)
        if self._footer_label_cache_key != label_key:
            for (label, _), (x, y) in zip(items, positions, strict=True):
                cv2.putText(canvas, label, (x, y + 16), cv2.FONT_HERSHEY_SIMPLEX,
                            .40, (127, 153, 184), 1, cv2.LINE_AA)
            # Capture labels before any values; both label rows belong to this band.
            self._footer_label_cache = canvas[top:top + 88].copy()
            self._footer_label_cache_key = label_key
        else:
            canvas[top:top + 88] = self._footer_label_cache
        value_key = (self.width, self.height, top, tuple(value for _, value in items[:5]))
        values_cached = self._footer_value_cache_key == value_key
        if values_cached:
            canvas[top:top + 88] = self._footer_value_cache
        for index, ((_, value), (x, y)) in enumerate(zip(items, positions, strict=True)):
            if index >= 5 or not values_cached:
                scale = .65 if columns == 4 else .75
                text_width = cv2.getTextSize(value, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)[0][0]
                scale *= min(1.0, (cell_width - 36) / max(1, text_width))
                cv2.putText(canvas, value, (x, y + 38), cv2.FONT_HERSHEY_SIMPLEX,
                            scale, (244, 248, 255), 2, cv2.LINE_AA)
            if index == 4 and not values_cached:
                self._footer_value_cache = canvas[top:top + 88].copy()
                self._footer_value_cache_key = value_key
        bar_left, bar_right, bar_top = 24, self.width - 24, top + 96
        cv2.rectangle(canvas, (bar_left, bar_top), (bar_right, bar_top + 12), (45, 60, 78), -1)
        if score_available:
            fill = round((bar_right - bar_left) * max(0.0, min(100.0, state.conformance_percent)) / 100.0)
            cv2.rectangle(canvas, (bar_left, bar_top), (bar_left + fill, bar_top + 12), (78, 220, 148), -1)
        if state.status_zh == "摄像头采集异常":
            status = "CAMERA ERROR | CHECK CONNECTION"
        elif state.feedback_zh:
            status = "REPETITION FEEDBACK READY"
        elif state.phase.value == "unknown":
            status = "WAITING FOR SYNCHRONIZED POSE"
        elif not state.score_valid:
            status = "POSE INCOMPLETE | SCORE UNAVAILABLE"
        else:
            status = "COACHING | KEEP BOTH VIEWS CLEAR"
        cv2.putText(canvas, status, (24, top + 132), cv2.FONT_HERSHEY_SIMPLEX,
                    .48, (188, 207, 228), 1, cv2.LINE_AA)

    def _draw_feedback_footer(self,canvas,state,top):
        cv2=self._cv2
        cv2.rectangle(canvas,(0,top),(self.width,self.height),(18,26,40),-1)
        items=(('COMPLETED',str(state.repetition_count)),('LAST REP SCORE',completed_score_text(state)),
               ('CURRENT PHASE',state.phase.value.upper()))
        for index,(label,value) in enumerate(items):
            x=24+index*(self.width-48)//3
            cv2.putText(canvas,label,(x,top+22),cv2.FONT_HERSHEY_SIMPLEX,.42,(150,174,199),1,cv2.LINE_AA)
            cv2.putText(canvas,value,(x,top+65),cv2.FONT_HERSHEY_SIMPLEX,1.15,
                        (120,232,181) if index==1 else (238,244,252),2,cv2.LINE_AA)
        cv2.putText(canvas,'Complete one squat for a stable action score.',(24,top+99),
                    cv2.FONT_HERSHEY_SIMPLEX,.52,(190,207,226),1,cv2.LINE_AA)
        cv2.putText(canvas,'Score stays until the next complete repetition.',(24,top+130),
                    cv2.FONT_HERSHEY_SIMPLEX,.48,(190,207,226),1,cv2.LINE_AA)

    def _draw_cjk_overlay(self, canvas: Any, state: CoachDisplayState, footer_top: int,
                          *, front=None, side=None, avatar_state=None) -> None:
        """Translate the existing layout; fonts remain optional for library users."""
        if not self._cjk_text.enabled:
            return
        cv2 = self._cv2
        texts = []
        cv2.rectangle(canvas, (0, 0), (self.width - 1, 63), (13, 18, 28), -1)
        def label(text, x, y, width, height, size=16, background=(13, 18, 28)):
            cv2.rectangle(canvas, (x, y), (x + width, y + height), background, -1)
            texts.append(OverlayText(text, x + 2, y, min(size, max(10, width // max(1, len(text)))),
                                     (228, 238, 248)))
        label("PosePriv 运动教练", 24, 8, self.width - 380, 48, 27)
        label("原录像回放 · 同帧点位" if '录像回放' in state.status_zh else
              "本地处理 · 原视频与点位" if self._preview_visible else "本地处理 · 骨架显示",
              self.width - 338, 22, 314, 28, 16)
        ax, ay, aw, ah = self._avatar_rect
        live = (24, 64, ax - 42, ah)
        label("真实视频输入", 36, 70, live[2] - 28, 28, 22, (24, 34, 50))
        label("动作跟随数字人" if avatar_state is not None else "二维姿态展示",
              ax + 12, ay + 6, aw - 28, 28, 22, (24, 34, 50))
        if front is not None and side is not None:
            slots = self._live_view_slots(live, front, side, adaptive=self._adaptive_camera_layout)
            for title, (x, y, width, _) in zip(("正面视角", "侧面视角"), slots):
                label(title, x + 3, y + 1, width - 6, 20, 15, (50, 68, 89))
        if avatar_state is not None:
            # These rectangles match the original captions, below the title band.
            label("拖动旋转 · 滚轮缩放 · R 显示骨架" if self._show_diagnostics else
                  "跟随同帧姿态 · 略侧身观察", ax + 20, ay + 53,
                  aw - 40, 27, 15, (18, 26, 40))
            if self._show_diagnostics:
                label(f"骨骼 {avatar_state.valid_bone_count}/{avatar_state.total_bone_count}  "
                  f"置信度 {avatar_state.confidence_percent:.0f}%  顶点 {avatar_state.skin_vertex_count}",
                  ax + 20, ay + ah - 53, aw - 40, 22, 14, (18, 26, 40))
            label("视频姿态驱动 · 平滑预览" if avatar_state.pose_preview else "双目标定三维重建",
                  ax + 20, ay + ah - 30, aw - 40, 21, 14, (18, 26, 40))
        if not self._show_diagnostics:
            for index,text in enumerate(('完成次数',
                f'动作规范度 · 第{state.completed_repetition_count}次' if state.completed_repetition_count else '动作规范度 · 完成后评分',
                '当前动作')):
                x=24+index*(self.width-48)//3
                label(text,x-2,footer_top+6,(self.width-48)//3-18,22,16,(18,26,40))
                if index==2:
                    label(state.phase_zh,x-2,footer_top+32,(self.width-48)//3-18,
                          27 if state.process_hint_zh else 39,24 if state.process_hint_zh else 30,(18,26,40))
                    if state.process_hint_zh:
                        label(state.process_hint_zh,x-2,footer_top+60,
                              (self.width-48)//3-18,18,12,(18,26,40))
            label(completed_components_text(state),24,footer_top+79,self.width-48,25,18,(18,26,40))
            feedback=(state.status_zh if state.status_zh=='摄像头采集异常' else
                      state.completed_correction_zh or '先完成一次完整深蹲；分数在动作结束后更新并保留。')
            label(feedback,24,footer_top+111,self.width-48,29,20,(18,26,40))
            self._cjk_text.draw_many(canvas,texts)
            return
        columns = 8 if self.width >= 1200 else 4
        cell = self.width // columns
        for i, text in enumerate(("动作阶段", "完成次数", "动作规范度", "观测置信度",
                                  "指标覆盖率", "同步帧率", "同步偏差", "端到端P95")):
            x = i % columns * cell + 18
            y = footer_top + (i // columns * 43 if columns == 4 else 18)
            label(text, x - 2, y, cell - 30, 18, 13, (18, 26, 40))
            if i == 0:
                label(state.phase_zh, x - 2, y + 20, cell - 30, 25, 21, (18, 26, 40))
        feedback = state.feedback_zh or state.status_zh or "等待双摄同步"
        label(feedback, 24, footer_top + 116, self.width - 48, 24, 17, (18, 26, 40))
        self._cjk_text.draw_many(canvas, texts)
