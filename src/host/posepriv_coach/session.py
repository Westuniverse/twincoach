"""Continuous dual-view squat session processing for the host MVP."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
from typing import Callable, Mapping

from .contracts import ExerciseScore, PoseFrame, SyncedPosePair
from .exercises.squat import (
    extract_side_knee_angle,
    extract_squat_joint_metrics,
    extract_squat_observations,
    LEGACY_SIDE_METRIC_POLICY, SIDE_METRIC_POLICIES,
)
from .exercises.squat_tracker import SquatPhase, SquatPhaseConfig, SquatTracker
from .pl_metrics import JointMetricBackend
from .scoring import MetricObservation, RuleScorer, load_rule_scorer
from .process_scoring import DEPTH, TEMPO, ProcessScoreConfig, ProcessSquatScorer
from .depth_angle_scoring import DepthAngleModel
from .sync import DualViewSynchronizer


class CorrectionDirection(str, Enum):
    BELOW = "below"
    ABOVE = "above"


@dataclass(frozen=True, slots=True)
class FeedbackCue:
    """A language-neutral explanation of one out-of-range observed metric."""

    metric: str
    direction: CorrectionDirection
    observed_value: float
    component_score_percent: float
    confidence_percent: float
    sustained_seconds: float = 0.0
    phases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SquatSessionUpdate:
    timestamp_ns: int
    front_frame_id: int
    side_frame_id: int
    skew_ns: int
    phase: SquatPhase
    repetition_count: int
    repetition_completed: bool
    repetition_duration_seconds: float | None
    live_score: ExerciseScore
    repetition_score: ExerciseScore | None
    cues: tuple[FeedbackCue, ...]
    pose_pair: SyncedPosePair
    scoring_algorithm: str = "deepest_frame_v0"
    live_cues: tuple[FeedbackCue, ...] = ()


class SquatSessionProcessor:
    """Join synchronized poses into live and completed-repetition results."""

    def __init__(
        self,
        scorer: RuleScorer,
        *,
        tracker_config: SquatPhaseConfig | None = None,
        max_skew_ms: float = 50.0,
        queue_depth: int = 4,
        minimum_tracking_confidence: float = 0.5,
        cue_score_threshold: float = 80.0,
        side_knee_extractor: Callable[[PoseFrame], MetricObservation | None] = (
            extract_side_knee_angle
        ),
        joint_metrics_backend: JointMetricBackend | None = None,
        require_front_ankles_below_knees: bool = False,
        process_score_config: ProcessScoreConfig | None = None,
        depth_angle_model: DepthAngleModel | None = None,
        side_metric_policy: str = LEGACY_SIDE_METRIC_POLICY,
    ) -> None:
        if not 0.0 <= minimum_tracking_confidence <= 1.0:
            raise ValueError("minimum_tracking_confidence must be within [0, 1]")
        if not 0.0 <= cue_score_threshold <= 100.0:
            raise ValueError("cue_score_threshold must be within [0, 100]")
        if not isinstance(require_front_ankles_below_knees, bool):
            raise ValueError("require_front_ankles_below_knees must be boolean")
        self.scorer = scorer
        self.synchronizer = DualViewSynchronizer(max_skew_ms, queue_depth)
        self.tracker = SquatTracker(tracker_config)
        self.minimum_tracking_confidence = minimum_tracking_confidence
        self.cue_score_threshold = cue_score_threshold
        self.side_knee_extractor = side_knee_extractor
        self.joint_metrics_backend = joint_metrics_backend
        self.require_front_ankles_below_knees = require_front_ankles_below_knees
        self._repetition_active = False
        self._deepest_observations: dict[str, MetricObservation] | None = None
        self._deepest_ratio: float | None = None
        if side_metric_policy not in SIDE_METRIC_POLICIES:
            raise ValueError("unsupported side metric selection policy")
        self.side_metric_policy = side_metric_policy
        if depth_angle_model is not None and process_score_config is None:
            raise ValueError("depth-angle scoring requires process scoring")
        self.process_scorer = (ProcessSquatScorer(scorer, process_score_config,
                                                depth_angle_model=depth_angle_model,
                                                side_metric_policy=side_metric_policy)
                               if process_score_config is not None else None)

    def push(self, frame: PoseFrame) -> SquatSessionUpdate | None:
        pair = self.synchronizer.push(frame)
        if pair is None:
            return None

        timestamp_ns = max(pair.front.timestamp_ns, pair.side.timestamp_ns)
        joint_batch = (
            extract_squat_joint_metrics(pair, self.joint_metrics_backend)
            if self.joint_metrics_backend is not None
            else None
        )
        knee = (
            joint_batch.side_knee_angle
            if joint_batch is not None
            else self.side_knee_extractor(pair.side)
        )
        front_order: bool | None = True
        if self.require_front_ankles_below_knees:
            points = tuple(pair.front.keypoints.get(name) for name in (
                "left_knee", "right_knee", "left_ankle", "right_ankle",
            ))
            if any(
                point is None or point.confidence < self.minimum_tracking_confidence
                for point in points
            ):
                front_order = None
            else:
                knees, ankles = points[:2], points[2:]
                front_order = bool(
                    min(point.y for point in ankles) > max(point.y for point in knees)
                )
        tracking_angle = (
            knee.value
            if front_order is True
            and knee is not None and knee.confidence >= self.minimum_tracking_confidence
            else None
        )
        previous_phase = self.tracker.phase
        tracker_update = self.tracker.update(
            tracking_angle, timestamp_ns, invalidate=front_order is False,
        )
        metric_options = ({"side_metric_policy": self.side_metric_policy}
                          if self.side_metric_policy != LEGACY_SIDE_METRIC_POLICY else {})
        observations = dict(
            extract_squat_observations(
                pair,
                include_front_knee_symmetry=joint_batch is None,
                **metric_options,
            )
        )
        if joint_batch is not None:
            observations.update(joint_batch.score_observations)
        # An unknown or contradicted optional qualification cannot supply
        # applicable live guidance, even if individual metric points exist.
        live_score = (
            self.process_scorer.live_score(
                tracker_update.phase, observations if tracking_angle is not None else {},
            ) if self.process_scorer is not None else
            self.scorer.score(observations if front_order is True else {})
        )

        repetition_score: ExerciseScore | None = None
        cues: tuple[FeedbackCue, ...] = ()

        if self.process_scorer is not None:
            if previous_phase is SquatPhase.STANDING and tracker_update.phase is SquatPhase.DESCENDING:
                self._clear_repetition()
                self._repetition_active = True
            if self._repetition_active:
                if self.tracker.phase is SquatPhase.UNKNOWN or (
                    tracker_update.phase is SquatPhase.STANDING and not tracker_update.repetition_completed
                ):
                    self._clear_repetition()
                else:
                    self.process_scorer.add(
                        timestamp_ns, self.tracker.phase,
                        observations if tracking_angle is not None else {},
                    )
                    if tracker_update.repetition_completed:
                        assert tracker_update.repetition_duration_seconds is not None
                        repetition_score = self.process_scorer.finish(
                            tracker_update.repetition_duration_seconds,
                        )
                        cues = self._make_process_cues(repetition_score)
                        self._clear_repetition()
        elif tracking_angle is None:
            # An explicit hold can retain the accepted phase until its valid
            # sample expires. Never remember observations from this frame.
            if self.tracker.phase is SquatPhase.UNKNOWN:
                self._clear_repetition()
        elif previous_phase is SquatPhase.STANDING and tracker_update.phase is SquatPhase.DESCENDING:
            self._repetition_active = True
            self._deepest_observations = None
            self._deepest_ratio = None
            self._remember_deepest(observations)
        elif tracker_update.repetition_completed:
            repetition_observations = dict(self._deepest_observations or {})
            if tracker_update.repetition_duration_seconds is not None:
                repetition_observations["tempo_seconds"] = MetricObservation(
                    value=tracker_update.repetition_duration_seconds,
                    confidence=1.0,
                )
            repetition_score = self.scorer.score(repetition_observations)
            cues = self._make_cues(repetition_score)
            self._clear_repetition()
        elif self._repetition_active:
            self._remember_deepest(observations)
            if tracker_update.phase in (SquatPhase.STANDING, SquatPhase.UNKNOWN):
                self._clear_repetition()

        return SquatSessionUpdate(
            timestamp_ns=timestamp_ns,
            front_frame_id=pair.front.frame_id,
            side_frame_id=pair.side.frame_id,
            skew_ns=pair.skew_ns,
            phase=tracker_update.phase,
            repetition_count=tracker_update.repetition_count,
            repetition_completed=tracker_update.repetition_completed,
            repetition_duration_seconds=tracker_update.repetition_duration_seconds,
            live_score=live_score,
            repetition_score=repetition_score,
            cues=cues,
            pose_pair=pair,
            scoring_algorithm=self.process_scorer.algorithm if self.process_scorer is not None else "deepest_frame_v0",
            live_cues=self._make_cues(live_score) if self.process_scorer is not None else (),
        )

    def _make_process_cues(self, score: ExerciseScore) -> tuple[FeedbackCue, ...]:
        if not score.valid or score.aggregation is None:
            return ()
        cues = [cue for cue in self._make_cues(score) if cue.metric in (DEPTH, TEMPO)]
        components = {component.name: component for component in score.components}
        assert self.process_scorer is not None
        threshold = self.process_scorer.config.sustained_deviation_ms / 1000
        for metric in score.aggregation.metrics:
            if metric.name == DEPTH or metric.name not in components:
                continue
            component = components[metric.name]
            for deviation in metric.deviations:
                if deviation.longest_seconds + 1e-12 >= threshold:
                    cues.append(FeedbackCue(
                        metric.name, CorrectionDirection(deviation.direction),
                        component.observed_value, component.score_percent,
                        component.confidence_percent, deviation.longest_seconds, deviation.phases,
                    ))
        return tuple(sorted(cues, key=lambda cue: (-cue.sustained_seconds,
                                                  cue.component_score_percent, cue.metric)))

    def _remember_deepest(self, observations: Mapping[str, MetricObservation]) -> None:
        depth = observations.get("side_depth_ratio")
        if depth is None or depth.confidence < self.minimum_tracking_confidence:
            return
        if self._deepest_ratio is None or depth.value < self._deepest_ratio:
            self._deepest_ratio = depth.value
            self._deepest_observations = dict(observations)

    def _make_cues(self, score: ExerciseScore) -> tuple[FeedbackCue, ...]:
        if not score.valid:
            return ()

        cues: list[FeedbackCue] = []
        for component in score.components:
            if component.score_percent >= self.cue_score_threshold:
                continue
            direction = (
                CorrectionDirection.BELOW
                if component.observed_value < component.ideal_min
                else CorrectionDirection.ABOVE
            )
            cues.append(
                FeedbackCue(
                    metric=component.name,
                    direction=direction,
                    observed_value=component.observed_value,
                    component_score_percent=component.score_percent,
                    confidence_percent=component.confidence_percent,
                )
            )
        return tuple(sorted(cues, key=lambda cue: (cue.component_score_percent, cue.metric)))

    def _clear_repetition(self) -> None:
        self._repetition_active = False
        self._deepest_observations = None
        self._deepest_ratio = None
        if self.process_scorer is not None:
            self.process_scorer.clear()


def load_squat_session_processor(
    path: str | Path,
    **processor_options: object,
) -> SquatSessionProcessor:
    """Load score and phase settings from one versioned exercise configuration."""

    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    phase = raw.get("phase_thresholds_deg")
    if not isinstance(phase, dict):
        raise ValueError("exercise configuration is missing phase_thresholds_deg")
    tracker_config = SquatPhaseConfig(
        standing_min=float(phase["standing_min"]),
        descending_enter=float(phase["descending_enter"]),
        bottom_max=float(phase["bottom_max"]),
        ascending_enter=float(phase["ascending_enter"]),
        maximum_sample_gap_ms=float(raw.get("maximum_tracking_gap_ms", 1000.0)),
        missing_tracking_policy=raw.get("missing_tracking_policy", "reset"),
    )
    processor_options.setdefault(
        "side_metric_policy", raw.get("side_metric_policy", LEGACY_SIDE_METRIC_POLICY),
    )
    processor_options.setdefault(
        "require_front_ankles_below_knees",
        raw.get("require_front_ankles_below_knees", False),
    )
    if "process_scoring" in raw:
        processor_options.setdefault(
            "process_score_config", ProcessScoreConfig.from_record(raw["process_scoring"]),
        )
    if "depth_angle_scoring" in raw:
        processor_options.setdefault(
            "depth_angle_model", DepthAngleModel.from_record(raw["depth_angle_scoring"]),
        )
    return SquatSessionProcessor(
        load_rule_scorer(config_path),
        tracker_config=tracker_config,
        **processor_options,
    )
