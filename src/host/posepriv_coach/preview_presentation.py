"""Readable preview feedback, independent of rendering and scoring rules.

Only presentation cadence changes. The completed score remains the original
immutable object, and invalid observations are never turned into valid scores.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from .contracts import ExerciseScore
from .display import CoachDisplayState, completed_components_text
from .exercises.squat_tracker import SquatPhase


@dataclass(frozen=True, slots=True)
class PreviewPresentationConfig:
    phase_confirmation_ms: float = 120.0
    hint_reading_ms: float = 1200.0
    metrics_interval_ms: float = 250.0
    maximum_observation_age_ms: float = 500.0

    def __post_init__(self):
        for value in (self.phase_confirmation_ms, self.hint_reading_ms,
                      self.metrics_interval_ms, self.maximum_observation_age_ms):
            if not math.isfinite(value) or value <= 0:
                raise ValueError('presentation intervals must be finite and positive')


@dataclass(frozen=True, slots=True)
class PreviewPresentationFrame:
    analyzed: bool
    observation_current: bool
    current_score_valid: bool
    observed_phase: SquatPhase
    phase_zh: str
    completed_repetition_count: int
    completed_score: ExerciseScore | None
    completed_summary_zh: str
    completed_components_zh: str
    confidence_percent: float | None
    coverage_percent: float | None
    hint_zh: str
    status_zh: str

    @property
    def lines_zh(self) -> tuple[str, str, str]:
        if not self.analyzed:
            return ('原片回放 · 本模式未进行姿态分析与动作评分',
                    '动作规范度：--　观测置信度：--　覆盖率：--', self.hint_zh)
        confidence = '--' if self.confidence_percent is None else f'{self.confidence_percent:.0f}%'
        coverage = '--' if self.coverage_percent is None else f'{self.coverage_percent:.0f}%'
        return (f'已记录动作 {self.completed_repetition_count} 次　{self.completed_summary_zh}',
                f'阶段：{self.phase_zh}　{self.status_zh}　观测置信度 {confidence}　覆盖率 {coverage}',
                self.hint_zh)


class PreviewScorePresenter:
    """Bound short-lived text changes without modifying source observations.

    Pass media/display ``timestamp_ns`` and the most recent synchronized pose
    ``observation_timestamp_ns`` separately when the display can outlive a pose.
    ``analyzed=False`` or a changed ``chapter_id`` clears all prior results.
    """

    def __init__(self, config: PreviewPresentationConfig | None = None):
        self.config = config or PreviewPresentationConfig()
        self.reset()

    def reset(self):
        self._timestamp_ns = None
        self._chapter_id = None
        self._phase = None
        self._phase_label = '等待姿态'
        self._pending_phase = None
        self._pending_phase_since = None
        self._hint = None
        self._hint_category = None
        self._hint_since = None
        self._metrics = (None, None)
        self._metrics_since = None
        self._current_valid = None
        self._completed_key = None
        self._completed_hint = None
        self._completed_hint_until = None

    def update(self, state: CoachDisplayState | None, *, timestamp_ns: int,
               observation_timestamp_ns: int | None = None,
               analyzed: bool = True, chapter_id: object = None) -> PreviewPresentationFrame:
        if type(timestamp_ns) is not int or timestamp_ns < 0:
            raise ValueError('nonnegative integer presentation timestamp required')
        if observation_timestamp_ns is None:
            observation_timestamp_ns = timestamp_ns
        if (type(observation_timestamp_ns) is not int or observation_timestamp_ns < 0
                or observation_timestamp_ns > timestamp_ns):
            raise ValueError('observation timestamp must be a past nonnegative integer')
        if chapter_id != self._chapter_id:
            self.reset()
            self._chapter_id = chapter_id
        if self._timestamp_ns is not None and timestamp_ns < self._timestamp_ns:
            raise ValueError('non-monotonic presentation timestamp')
        self._timestamp_ns = timestamp_ns
        if not analyzed or state is None:
            self.reset()
            self._chapter_id = chapter_id
            self._timestamp_ns = timestamp_ns
            return PreviewPresentationFrame(False, False, False, SquatPhase.UNKNOWN,
                '未分析', 0, None, '动作规范度：--', '', None, None,
                '本模式仅显示原始录像，不沿用其他片段分数。', '未分析与评分')

        age_ns = timestamp_ns-observation_timestamp_ns
        capture_failed = '异常' in state.status_zh
        current = age_ns <= round(self.config.maximum_observation_age_ms*1e6) and not capture_failed
        current_valid = current and state.score_valid
        completed_key = (state.completed_repetition_count, state.completed_score)
        completed_changed = completed_key != self._completed_key
        self._completed_key = completed_key
        if completed_changed and state.completed_repetition_count:
            self._completed_hint = state.completed_correction_zh
            self._completed_hint_until = timestamp_ns+round(self.config.hint_reading_ms*1e6)

        phase = state.phase if current else SquatPhase.UNKNOWN
        phase_label = state.phase_zh if current else '等待新观测'
        immediate_phase = (self._phase is None or phase is SquatPhase.UNKNOWN
                           or self._phase is SquatPhase.UNKNOWN or completed_changed)
        if immediate_phase:
            self._set_phase(phase, phase_label)
        elif phase is self._phase:
            self._pending_phase = self._pending_phase_since = None
        elif phase is not self._pending_phase:
            self._pending_phase, self._pending_phase_since = phase, timestamp_ns
        elif timestamp_ns-self._pending_phase_since >= round(self.config.phase_confirmation_ms*1e6):
            self._set_phase(phase, phase_label)

        if not current:
            self._metrics = (None, None)
            self._metrics_since = timestamp_ns
        elif (self._metrics_since is None or current_valid != self._current_valid
              or timestamp_ns-self._metrics_since >= round(self.config.metrics_interval_ms*1e6)):
            self._metrics = (state.confidence_percent, state.coverage_percent)
            self._metrics_since = timestamp_ns
        self._current_valid = current_valid

        if capture_failed:
            status, desired_hint, category = ('采集异常', state.status_zh+'，请检查摄像头连接。', 'tracking')
        elif not current:
            status, desired_hint, category = ('观测已超时', '等待新的双摄像头姿态观测；上次结果仅供回看。', 'tracking')
        elif (self._completed_hint and self._completed_hint_until is not None
              and timestamp_ns <= self._completed_hint_until):
            status = '当前观测有效' if state.score_valid else '当前暂不评分'
            desired_hint, category = '上次动作建议：'+self._completed_hint, 'completed'
        elif not state.score_valid:
            status, desired_hint, category = ('当前暂不评分', state.process_hint_zh or state.status_zh, 'invalid')
        else:
            status, desired_hint, category = ('当前观测有效', state.process_hint_zh or state.status_zh, 'guidance')
        if (self._hint is None or category != self._hint_category or completed_changed or category == 'tracking'
                or timestamp_ns-self._hint_since >= round(self.config.hint_reading_ms*1e6)):
            if desired_hint != self._hint or category != self._hint_category:
                self._hint, self._hint_category, self._hint_since = desired_hint, category, timestamp_ns

        score = state.completed_score
        if not state.completed_repetition_count:
            completed_summary = '完成一次动作后显示规范度'
        elif score is None or not score.valid or score.conformance_percent is None:
            completed_summary = '上次动作：暂不评分'
        else:
            completed_summary = f'上次动作规范度：{score.conformance_percent:.0f} 分'
        return PreviewPresentationFrame(True, current, current_valid, state.phase,
            self._phase_label, state.completed_repetition_count, score, completed_summary,
            completed_components_text(state), *self._metrics, self._hint, status)

    def _set_phase(self, phase, label):
        self._phase, self._phase_label = phase, label
        self._pending_phase = self._pending_phase_since = None
