"""Thin application runtime joining pipeline steps with local session reporting."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .avatar_runtime import AvatarDispatchStats, AvatarRuntimeStats
from .capture import CapturePumpStats
from .comparison_display import ComparisonRenderStats
from .display_dispatch import DisplayDispatchStats
from .movenet_vart import VartInferenceStats
from .contracts import SyncedPosePair
from .motion_window import MotionWindowStats, TemporalMotionWindow
from .pl_metrics import JointMetricBackendStats
from .pipeline import DualViewCoachPipeline, PipelineStep
from .pose_adapter import MOVENET_COCO_KEYPOINTS
from .session_report import CoachSessionReportWriter


class CoachRuntime:
    """Process queued frames and emit one report record per completed repetition."""

    def __init__(
        self,
        pipeline: DualViewCoachPipeline,
        *,
        report: CoachSessionReportWriter | None = None,
        on_step: Callable[[PipelineStep], None] | None = None,
        on_synced_pair: Callable[[SyncedPosePair], None] | None = None,
        motion_window: TemporalMotionWindow | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.report = report
        self.on_step = on_step
        self.on_synced_pair = on_synced_pair
        self.motion_window = (
            motion_window
            if motion_window is not None
            else TemporalMotionWindow(MOVENET_COCO_KEYPOINTS)
        )

    def process_next(self) -> PipelineStep | None:
        step = self.pipeline.process_next()
        if step is None:
            return None
        update = step.session_update
        if update is not None:
            self.motion_window.push(update.pose_pair.front)
            self.motion_window.push(update.pose_pair.side)
            if self.on_synced_pair is not None:
                self.on_synced_pair(update.pose_pair)
        if (
            self.report is not None
            and update is not None
            and update.repetition_completed
            and update.repetition_score is not None
        ):
            self.report.write_repetition(update)
        if self.on_step is not None:
            self.on_step(step)
        return step

    def drain(self, max_steps: int | None = None) -> int:
        if max_steps is not None and max_steps < 1:
            raise ValueError("max_steps must be positive")
        processed = 0
        while max_steps is None or processed < max_steps:
            if self.process_next() is None:
                break
            processed += 1
        return processed

    def write_summary(
        self,
        capture: CapturePumpStats | None = None,
        avatar: AvatarRuntimeStats | None = None,
        render: ComparisonRenderStats | None = None,
        vart: VartInferenceStats | None = None,
        joint: JointMetricBackendStats | None = None,
        native_vart: Mapping[str, Any] | None = None,
        display_dispatch: DisplayDispatchStats | None = None,
        avatar_dispatch: AvatarDispatchStats | None = None,
    ) -> None:
        if self.report is not None:
            self.report.write_summary(
                self.pipeline.stats(),
                capture,
                avatar,
                render,
                vart,
                self.motion_window.stats(),
                joint,
                native_vart,
                display_dispatch,
                avatar_dispatch,
            )
