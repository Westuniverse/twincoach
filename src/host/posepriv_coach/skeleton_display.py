"""Optional OpenCV skeleton-only renderer for a local window or HDMI display."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from pathlib import Path
from time import monotonic_ns
from typing import Any, Callable

from .contracts import CameraView, PoseFrame
from .display import CoachDisplayState
from .text_overlay import OptionalCjkTextOverlay, OverlayText


SKELETON_EDGES = (
    ("left_shoulder", "right_shoulder"),
    ("left_shoulder", "left_elbow"),
    ("left_elbow", "left_wrist"),
    ("right_shoulder", "right_elbow"),
    ("right_elbow", "right_wrist"),
    ("left_shoulder", "left_hip"),
    ("right_shoulder", "right_hip"),
    ("left_hip", "right_hip"),
    ("left_hip", "left_knee"),
    ("left_knee", "left_ankle"),
    ("right_hip", "right_knee"),
    ("right_knee", "right_ankle"),
)

_COMPONENT_ASCII = {
    "side_depth_ratio": "DEPTH",
    "side_trunk_angle_deg": "TRUNK",
    "front_knee_symmetry": "SYMMETRY",
    "tempo_seconds": "TEMPO",
}


def _p95_nearest_rank(samples: deque[int]) -> int | None:
    if not samples:
        return None
    ordered = sorted(samples)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


class SkeletonDisplayError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SkeletonRenderStats:
    """记录骨架画布合成和 sink 写入边界的耗时。"""

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


class OpenCVSkeletonRenderer:
    """Render pose coordinates on a black canvas without exposing camera pixels."""

    def __init__(
        self,
        *,
        width: int = 1280,
        height: int = 720,
        minimum_confidence: float = 0.5,
        window_name: str = "PosePriv-Coach",
        cv2_module: Any | None = None,
        numpy_module: Any | None = None,
        frame_sink: Callable[[Any], None] | None = None,
        clock_ns: Callable[[], int] = monotonic_ns,
        latency_window_size: int = 1024,
        cjk_font_path: str | Path | None = None,
    ) -> None:
        if width < 640 or height < 360:
            raise ValueError("display resolution must be at least 640x360")
        if not 0.0 <= minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence must be within [0, 1]")
        if latency_window_size <= 0:
            raise ValueError("latency_window_size must be positive")
        if cv2_module is None:
            try:
                import cv2 as cv2_module  # type: ignore[import-not-found, no-redef]
            except ImportError as exc:
                raise SkeletonDisplayError("骨架窗口需要 OpenCV") from exc
        if numpy_module is None:
            try:
                import numpy as numpy_module  # type: ignore[import-not-found, no-redef]
            except ImportError as exc:
                raise SkeletonDisplayError("骨架窗口需要 NumPy") from exc
        self.width = width
        self.height = height
        self.minimum_confidence = minimum_confidence
        self.window_name = window_name
        self._cv2 = cv2_module
        self._numpy = numpy_module
        self._frame_sink = frame_sink
        self._clock_ns = clock_ns
        self._poses: dict[CameraView, PoseFrame] = {}
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
        self._cjk_text = OptionalCjkTextOverlay(cjk_font_path, numpy_module=self._numpy)

    def update(self, pose: PoseFrame, state: CoachDisplayState) -> None:
        if self._closed:
            raise SkeletonDisplayError("骨架窗口已经关闭")
        self._poses[pose.view] = pose
        self._render(state)

    def update_pair(
        self,
        front: PoseFrame,
        side: PoseFrame,
        state: CoachDisplayState,
    ) -> None:
        """Update one synchronized pair and refresh the window exactly once."""

        if self._closed:
            raise SkeletonDisplayError("骨架窗口已经关闭")
        if front.view is not CameraView.FRONT:
            raise ValueError("front pose has the wrong camera view")
        if side.view is not CameraView.SIDE:
            raise ValueError("side pose has the wrong camera view")
        self._poses[CameraView.FRONT] = front
        self._poses[CameraView.SIDE] = side
        self._render(state)

    def _render(self, state: CoachDisplayState) -> None:
        render_start_ns = self._clock_ns()
        canvas = self._numpy.zeros((self.height, self.width, 3), dtype=self._numpy.uint8)
        panel_width = self.width // 2
        self._draw_panel(canvas, CameraView.FRONT, 0, panel_width, "FRONT")
        self._draw_panel(
            canvas,
            CameraView.SIDE,
            panel_width,
            self.width - panel_width,
            "SIDE",
        )
        self._draw_status(canvas, state)
        self._draw_cjk_overlay(canvas, state)
        compose_complete_ns = self._clock_ns()
        if compose_complete_ns < render_start_ns:
            raise SkeletonDisplayError("display clock must be monotonic")
        if self._frame_sink is None:
            self._cv2.imshow(self.window_name, canvas)
            key = int(self._cv2.waitKey(1)) & 0xFF
            if key in (27, ord("q"), ord("Q")):
                self.close_requested = True
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

    def stats(self) -> SkeletonRenderStats:
        """返回骨架画布合成/写屏耗时，不含摄像头和模型推理。"""

        return SkeletonRenderStats(
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

    def close(self) -> None:
        if not self._closed and self._frame_sink is None:
            self._cv2.destroyWindow(self.window_name)
        self._closed = True

    def _draw_panel(
        self,
        canvas: Any,
        view: CameraView,
        x_offset: int,
        panel_width: int,
        label: str,
    ) -> None:
        cv2 = self._cv2
        top = 64
        bottom = self.height - 150
        left = x_offset + 24
        right = x_offset + panel_width - 24
        cv2.rectangle(canvas, (left, top), (right, bottom), (70, 70, 70), 1)
        cv2.putText(
            canvas,
            label,
            (left, 42),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (180, 220, 255),
            2,
            cv2.LINE_AA,
        )
        pose = self._poses.get(view)
        if pose is None:
            return

        def point(name: str) -> tuple[int, int] | None:
            keypoint = pose.keypoints.get(name)
            if keypoint is None or keypoint.confidence < self.minimum_confidence:
                return None
            return (
                left + round(keypoint.x * (right - left)),
                top + round(keypoint.y * (bottom - top)),
            )

        for first, second in SKELETON_EDGES:
            point_a = point(first)
            point_b = point(second)
            if point_a is not None and point_b is not None:
                cv2.line(canvas, point_a, point_b, (80, 210, 120), 3, cv2.LINE_AA)
        for name in pose.keypoints:
            center = point(name)
            if center is not None:
                cv2.circle(canvas, center, 5, (50, 180, 255), -1, cv2.LINE_AA)

    def _draw_status(self, canvas: Any, state: CoachDisplayState) -> None:
        cv2 = self._cv2
        score = "--" if state.conformance_percent is None else f"{state.conformance_percent:.0f}"
        fps = "--" if state.processing_fps is None else f"{state.processing_fps:.1f}"
        front_fps = "--" if state.front_fps is None else f"{state.front_fps:.1f}"
        side_fps = "--" if state.side_fps is None else f"{state.side_fps:.1f}"
        pair_fps = (
            "--"
            if state.synchronized_pair_fps is None
            else f"{state.synchronized_pair_fps:.1f}"
        )
        p95_latency = (
            "--"
            if state.p95_end_to_end_ms is None
            else f"{state.p95_end_to_end_ms:.1f}ms"
        )
        skew = "--" if state.current_skew_ms is None else f"{state.current_skew_ms:.1f}ms"
        weakest = (
            min(state.score_components, key=lambda component: component.score_percent)
            if state.score_components
            else None
        )
        weakest_text = (
            "WEAK --"
            if weakest is None
            else f"WEAK {_COMPONENT_ASCII.get(weakest.name, weakest.name.upper())} "
            f"{weakest.score_percent:.0f}"
        )
        lines = (
            f"PHASE {state.phase.value.upper()}    REPS {state.repetition_count}",
            f"SCORE {score}    CONF {state.confidence_percent:.0f}%    "
            f"COVER {state.coverage_percent:.0f}%",
            f"FPS TOTAL {fps}    FRONT/SIDE {front_fps}/{side_fps}    "
            f"PAIR {pair_fps}    E2E P95 {p95_latency}",
            f"SKEW {skew}    {weakest_text}    PRIVACY SKELETON-ONLY",
        )
        for index, line in enumerate(lines):
            cv2.putText(
                canvas,
                line,
                (28, self.height - 120 + index * 28),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (235, 235, 235),
                2,
                cv2.LINE_AA,
            )

    def _draw_cjk_overlay(self, canvas: Any, state: CoachDisplayState) -> None:
        """在可选字体存在时补充中文状态；无字体时保持原 ASCII 画面。"""

        feedback = state.feedback_zh or state.status_zh or "等待双摄同步"
        self._cjk_text.draw_many(
            canvas,
            (
                OverlayText(
                    "本地运动教练 · 隐私模式",
                    28,
                    8,
                    16,
                    (112, 214, 255),
                ),
                OverlayText(
                    f"状态：{feedback}",
                    28,
                    self.height - 34,
                    16,
                    (228, 238, 248),
                    stroke_width=1,
                    stroke_color_bgr=(13, 18, 28),
                ),
            ),
        )
