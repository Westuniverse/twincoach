"""Renderer-independent Chinese display state for console, window or HDMI output."""

from __future__ import annotations

from dataclasses import dataclass

from .capture import CapturePumpStats
from .contracts import ExerciseScore, ScoreComponent
from .feedback_zh import format_completed_squat_zh, format_feedback_cue_zh
from .pipeline import PipelineStats, PipelineStep
from .session import SquatSessionUpdate
from .exercises.squat_tracker import SquatPhase


_PHASE_ZH = {
    SquatPhase.UNKNOWN: "等待姿态",
    SquatPhase.STANDING: "站立",
    SquatPhase.DESCENDING: "下蹲",
    SquatPhase.BOTTOM: "最低点",
    SquatPhase.ASCENDING: "起身",
}

_COMPONENT_ZH = {
    "side_depth_ratio": "下蹲深度",
    "side_trunk_angle_deg": "躯干倾角",
    "front_knee_symmetry": "左右膝对称",
    "tempo_seconds": "动作节奏",
}


@dataclass(frozen=True, slots=True)
class CoachDisplayState:
    phase: SquatPhase
    phase_zh: str
    repetition_count: int
    score_valid: bool
    conformance_percent: float | None
    confidence_percent: float
    coverage_percent: float
    feedback_zh: str | None
    processing_fps: float | None
    front_fps: float | None
    side_fps: float | None
    synchronized_pair_fps: float | None
    current_skew_ms: float | None
    p95_skew_ms: float | None
    capture_dropped_front: int
    capture_dropped_side: int
    sync_dropped_front: int
    sync_dropped_side: int
    status_zh: str
    score_components: tuple[ScoreComponent, ...] = ()
    mean_end_to_end_ms: float | None = None
    p95_end_to_end_ms: float | None = None
    latency_sample_count: int = 0
    latency_window_size: int = 0
    completed_repetition_count: int = 0
    completed_score: ExerciseScore | None = None
    completed_correction_zh: str | None = None
    process_hint_zh: str | None = None


class CoachDisplayModel:
    """Keep the latest synchronized result while single-view frames are processed."""

    def __init__(self) -> None:
        self._latest_update: SquatSessionUpdate | None = None
        self._completed_update: SquatSessionUpdate | None = None

    def update(
        self,
        step: PipelineStep,
        stats: PipelineStats,
        capture: CapturePumpStats | None = None,
    ) -> CoachDisplayState:
        if step.session_update is not None:
            self._latest_update = step.session_update
            if step.session_update.repetition_completed:
                self._completed_update = step.session_update
        update = self._latest_update
        capture_failed = capture.failed if capture is not None else False

        if capture_failed:
            status = "摄像头采集异常"
        elif update is None:
            status = "等待正面和侧面姿态同步"
        elif update.scoring_algorithm == "process_time_v1" and update.phase is SquatPhase.STANDING:
            status = "准备开始下一次深蹲"
        elif not update.live_score.valid:
            status = "关键点不足，当前帧暂不评分"
        else:
            status = "实时指导中"

        if update is None:
            phase = SquatPhase.UNKNOWN
            repetition_count = 0
            score_valid = False
            conformance = None
            confidence = 0.0
            coverage = 0.0
            current_skew_ms = None
            feedback = None
        else:
            phase = update.phase
            repetition_count = update.repetition_count
            score_valid = update.live_score.valid
            conformance = update.live_score.conformance_percent
            confidence = update.live_score.confidence_percent
            coverage = update.live_score.coverage_percent
            current_skew_ms = update.skew_ns / 1_000_000
            feedback = format_completed_squat_zh(update)

        return CoachDisplayState(
            phase=phase,
            phase_zh=_PHASE_ZH[phase],
            repetition_count=repetition_count,
            score_valid=score_valid,
            conformance_percent=conformance,
            confidence_percent=confidence,
            coverage_percent=coverage,
            feedback_zh=feedback,
            processing_fps=stats.observed_processing_fps,
            front_fps=stats.observed_front_fps,
            side_fps=stats.observed_side_fps,
            synchronized_pair_fps=stats.observed_synchronized_pair_fps,
            current_skew_ms=current_skew_ms,
            p95_skew_ms=stats.pose_sync.p95_skew_ms,
            capture_dropped_front=stats.dropped_front,
            capture_dropped_side=stats.dropped_side,
            sync_dropped_front=stats.pose_sync.dropped_front,
            sync_dropped_side=stats.pose_sync.dropped_side,
            status_zh=status,
            score_components=() if update is None else update.live_score.components,
            mean_end_to_end_ms=stats.mean_end_to_end_ms,
            p95_end_to_end_ms=stats.p95_end_to_end_ms,
            latency_sample_count=stats.latency_sample_count,
            latency_window_size=stats.latency_window_size,
            **completed_feedback_fields(self._completed_update),
            process_hint_zh=process_hint_zh(update),
        )


def completed_feedback_fields(update: SquatSessionUpdate | None) -> dict:
    """Latched action result only; never substitutes instantaneous live_score."""
    if update is None:
        return dict(completed_repetition_count=0,completed_score=None,completed_correction_zh=None)
    score=update.repetition_score
    if score is None or not score.valid or score.conformance_percent is None:
        correction='本次观测不足，暂不评分；请保持全身在双摄画面内。'
        if score is not None and score.aggregation is not None:
            reasons = score.aggregation.invalid_reasons
            details = []
            if "insufficient_process_time_coverage" in reasons:
                details.append(f'过程有效观测仅{score.aggregation.time_coverage_percent:.0f}%')
            if "insufficient_bottom_window" in reasons:
                details.append('底部有效观测不足')
            correction = '本次暂不评分：' + '，'.join(details or ['分项覆盖不足']) + '。'
    else:
        correction=format_feedback_cue_zh(update.cues[0]) if update.cues else '各项动作指标符合当前规则，继续保持。'
    return dict(completed_repetition_count=update.repetition_count,completed_score=score,
                completed_correction_zh=correction)


def completed_score_text(state: CoachDisplayState) -> str:
    score=state.completed_score
    return (f'{score.conformance_percent:.0f}' if score is not None and score.valid
            and score.conformance_percent is not None else '--')


def completed_components_text(state: CoachDisplayState) -> str:
    score=state.completed_score
    if score is None or not score.valid:
        return ('本次观测不足，未生成分项得分。' if state.completed_repetition_count else
                '完成一次完整深蹲后，显示本次得分与改进建议。')
    labels={'side_depth_ratio':'深度','side_trunk_angle_deg':'躯干','front_knee_symmetry':'对称','tempo_seconds':'节奏'}
    if score.aggregation is not None and score.aggregation.depth_angle_model is not None:
        labels['side_trunk_angle_deg'] = '躯干与深度协调'
    text = '  ·  '.join(f'{labels.get(c.name,c.name)} {c.score_percent:.0f}分' for c in score.components)
    if score.aggregation is not None:
        text += f'  ·  有效时长 {score.aggregation.time_coverage_percent:.0f}%'
    return text


def process_hint_zh(update: SquatSessionUpdate | None) -> str | None:
    if update is None or update.scoring_algorithm not in ("process_time_v1", "depth_angle_time_v1"):
        return None
    if update.phase is SquatPhase.STANDING:
        return "准备开始；总分在完成后更新"
    if not update.live_score.valid:
        if update.scoring_algorithm == "depth_angle_time_v1":
            return "当前有效观测不足或超出评分范围，请检查双摄画面"
        return "当前观测不足，请保持双摄可见"
    if update.live_cues:
        return "实时提示：" + format_feedback_cue_zh(update.live_cues[0])
    if update.scoring_algorithm == "depth_angle_time_v1":
        return "当前深度与姿态符合阶段规则，保持稳定"
    return "当前阶段符合规则，保持稳定"


def format_display_lines_zh(state: CoachDisplayState) -> tuple[str, ...]:
    """Format compact lines shared by terminal and future HDMI renderers."""

    lines = [
        f"阶段：{state.phase_zh} | 次数：{state.repetition_count}",
    ]
    if state.process_hint_zh is not None:
        lines.append(f"过程提示（非最终总分）：{state.process_hint_zh}")
    if state.score_valid and state.conformance_percent is not None:
        lines.append(
            f"动作规范度：{state.conformance_percent:.0f}分 | "
            f"置信度：{state.confidence_percent:.0f}% | "
            f"覆盖率：{state.coverage_percent:.0f}%"
        )
    else:
        lines.append(
            f"动作规范度：-- | 置信度：{state.confidence_percent:.0f}% | "
            f"覆盖率：{state.coverage_percent:.0f}%"
        )

    fps = "--" if state.processing_fps is None else f"{state.processing_fps:.1f}"
    front_fps = "--" if state.front_fps is None else f"{state.front_fps:.1f}"
    side_fps = "--" if state.side_fps is None else f"{state.side_fps:.1f}"
    pair_fps = (
        "--"
        if state.synchronized_pair_fps is None
        else f"{state.synchronized_pair_fps:.1f}"
    )
    skew = "--" if state.current_skew_ms is None else f"{state.current_skew_ms:.1f} ms"
    p95 = "--" if state.p95_skew_ms is None else f"{state.p95_skew_ms:.1f} ms"
    lines.append(
        f"处理帧率 总/正/侧：{fps}/{front_fps}/{side_fps} FPS | "
        f"同步对率：{pair_fps} 对/秒"
    )
    mean_latency = (
        "--"
        if state.mean_end_to_end_ms is None
        else f"{state.mean_end_to_end_ms:.1f} ms"
    )
    p95_latency = (
        "--"
        if state.p95_end_to_end_ms is None
        else f"{state.p95_end_to_end_ms:.1f} ms"
    )
    lines.append(
        f"端到端延迟 平均/P95：{mean_latency}/{p95_latency} | "
        f"近期样本：{state.latency_sample_count}/{state.latency_window_size} 帧"
    )
    lines.append(f"双摄偏差 当前：{skew} | P95：{p95}")
    lines.append(
        f"覆盖丢帧 正/侧：{state.capture_dropped_front}/{state.capture_dropped_side} | "
        f"同步淘汰 正/侧：{state.sync_dropped_front}/{state.sync_dropped_side}"
    )
    lines.append(f"状态：{state.status_zh}")
    for component in state.score_components:
        label = _COMPONENT_ZH.get(component.name, component.name)
        lines.append(
            f"分项-{label}：观测 {component.observed_value:.2f} {component.unit} | "
            f"规范度 {component.score_percent:.0f}分 | "
            f"置信度 {component.confidence_percent:.0f}% | "
            f"理想 {component.ideal_min:g}～{component.ideal_max:g}"
        )
    if state.feedback_zh is not None:
        lines.append(f"建议：{state.feedback_zh}")
    return tuple(lines)
