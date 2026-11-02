"""Versioned, phase-applicable time scoring. No pose, tracking or rendering changes."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Mapping

from .contracts import (
    DeviationSummary, ExerciseScore, MetricAggregation, PhaseScoreSummary,
    ScoreAggregation, ScoreComponent,
)
from .exercises.squat_tracker import SquatPhase
from .scoring import MetricObservation, MetricRule, RuleScorer
from .depth_angle_scoring import ALGORITHM as DEPTH_ANGLE_ALGORITHM, DepthAngleModel, interpolate_depth
from .exercises.squat import LEGACY_SIDE_METRIC_POLICY, SIDE_METRIC_POLICIES


ALGORITHM = "process_time_v1"
DEPTH = "side_depth_ratio"
TRUNK = "side_trunk_angle_deg"
SYMMETRY = "front_knee_symmetry"
TEMPO = "tempo_seconds"
ACTIVE_PHASES = (SquatPhase.DESCENDING, SquatPhase.BOTTOM, SquatPhase.ASCENDING)


@dataclass(frozen=True, slots=True)
class ProcessScoreConfig:
    maximum_interval_ms: float = 250.0
    bottom_half_window_ms: float = 150.0
    minimum_bottom_window_ms: float = 100.0
    minimum_time_coverage: float = 0.8
    low_score_threshold: float = 80.0
    sustained_deviation_ms: float = 300.0
    moving_trunk_ideal_min: float = 0.0

    def __post_init__(self) -> None:
        for name in ("maximum_interval_ms", "bottom_half_window_ms",
                     "minimum_bottom_window_ms", "sustained_deviation_ms"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.minimum_bottom_window_ms > 2 * self.bottom_half_window_ms:
            raise ValueError("bottom minimum must fit within the window")
        if not 0 < self.minimum_time_coverage <= 1:
            raise ValueError("minimum_time_coverage must be within (0, 1]")
        if not 0 < self.low_score_threshold < 100:
            raise ValueError("low_score_threshold must be within (0, 100)")
        if not math.isfinite(self.moving_trunk_ideal_min):
            raise ValueError("moving_trunk_ideal_min must be finite")

    @classmethod
    def from_record(cls, raw: Mapping) -> ProcessScoreConfig:
        if raw.get("algorithm") != ALGORITHM:
            raise ValueError("unsupported process scoring algorithm")
        fields = cls.__dataclass_fields__
        if set(raw) != {"algorithm", *fields}:
            raise ValueError("process scoring requires all versioned parameters")
        return cls(**{name: float(raw[name]) for name in fields})

    def parameters(self) -> tuple[tuple[str, float], ...]:
        return tuple((name, getattr(self, name)) for name in self.__dataclass_fields__)


@dataclass(frozen=True, slots=True)
class _Sample:
    timestamp_ns: int
    phase: SquatPhase
    observations: Mapping[str, MetricObservation]


@dataclass(frozen=True, slots=True)
class _Segment:
    start: int
    end: int
    phase: SquatPhase
    rule: MetricRule
    left: MetricObservation | None
    right: MetricObservation | None
    left_score: float = 0.0
    right_score: float = 0.0
    right_rule: MetricRule | None = None
    confirm_deviation: bool = True

    @property
    def seconds(self) -> float:
        return (self.end - self.start) / 1e9

    @property
    def valid(self) -> bool:
        return self.left is not None and self.right is not None

    def clip(self, start: int, end: int) -> _Segment | None:
        lo, hi = max(self.start, start), min(self.end, end)
        if hi <= lo:
            return None
        if not self.valid:
            return replace(self, start=lo, end=hi)
        assert self.left is not None and self.right is not None
        a = (lo - self.start) / (self.end - self.start)
        b = (hi - self.start) / (self.end - self.start)

        def observation(fraction: float) -> MetricObservation:
            return MetricObservation(
                self.left.value + fraction * (self.right.value - self.left.value),
                self.left.confidence + fraction * (self.right.confidence - self.left.confidence),
            )

        return replace(self, start=lo, end=hi, left=observation(a), right=observation(b),
                       left_score=self.left_score + a * (self.right_score - self.left_score),
                       right_score=self.left_score + b * (self.right_score - self.left_score))


def _below_fraction(left: float, right: float, threshold: float) -> float:
    if left == right:
        return float(left < threshold)
    low, high = sorted((left, right))
    return max(0.0, min(1.0, (threshold - low) / (high - low)))


def _time_median(segments: list[_Segment]) -> float:
    """Median of continuous piecewise-linear *scores*, weighted by seconds."""
    total = sum(segment.seconds for segment in segments)
    lo, hi = 0.0, 100.0
    for _ in range(48):
        midpoint = (lo + hi) / 2
        below = sum(segment.seconds * (
            float(segment.left_score <= midpoint) if segment.left_score == segment.right_score
            else _below_fraction(segment.left_score, segment.right_score, midpoint)
        ) for segment in segments)
        if below >= total / 2:
            hi = midpoint
        else:
            lo = midpoint
    return (lo + hi) / 2


def _reuse_rule_bounds(rule: MetricRule, lower: float, upper: float) -> MetricRule:
    """Reuse a validated frozen rule only when both bound representations match."""
    same_lower = type(lower) is type(rule.ideal_min) and lower == rule.ideal_min
    same_upper = type(upper) is type(rule.ideal_max) and upper == rule.ideal_max
    if (same_lower and same_upper
            and (lower != 0 or math.copysign(1., lower) == math.copysign(1., rule.ideal_min))
            and (upper != 0 or math.copysign(1., upper) == math.copysign(1., rule.ideal_max))):
        return rule
    return replace(rule, ideal_min=lower, ideal_max=upper)


class ProcessSquatScorer:
    """Own only the current attempt; caller supplies actual tracker boundaries."""

    def __init__(self, scorer: RuleScorer, config: ProcessScoreConfig, *,
                 depth_angle_model: DepthAngleModel | None = None,
                 side_metric_policy: str = LEGACY_SIDE_METRIC_POLICY) -> None:
        if side_metric_policy not in SIDE_METRIC_POLICIES:
            raise ValueError("unsupported side metric selection policy")
        self.scorer = scorer
        self.config = config
        self.depth_angle_model = depth_angle_model
        self.side_metric_policy = side_metric_policy
        self.algorithm = DEPTH_ANGLE_ALGORITHM if depth_angle_model is not None else ALGORITHM
        self.rules = {rule.name: rule for rule in scorer.rules}
        if set(self.rules) != {DEPTH, TRUNK, SYMMETRY, TEMPO} or len(scorer.rules) != 4:
            raise ValueError("process squat requires the four documented metric rules")
        if config.moving_trunk_ideal_min > self.rules[TRUNK].ideal_max:
            raise ValueError("moving trunk ideal range is reversed")
        self._samples: list[_Sample] = []

    def clear(self) -> None:
        self._samples.clear()

    def add(self, timestamp_ns: int, phase: SquatPhase,
            observations: Mapping[str, MetricObservation]) -> None:
        if timestamp_ns < 0:
            raise ValueError("process timestamps must be non-negative")
        if self._samples and timestamp_ns <= self._samples[-1].timestamp_ns:
            raise ValueError("process samples must be strictly increasing")
        self._samples.append(_Sample(timestamp_ns, phase, dict(observations)))

    def phase_rule(self, name: str, phase: SquatPhase) -> MetricRule:
        rule = self.rules[name]
        if name == TRUNK and phase is not SquatPhase.BOTTOM:
            return replace(rule, ideal_min=self.config.moving_trunk_ideal_min)
        return rule

    def live_score(self, phase: SquatPhase,
                   observations: Mapping[str, MetricObservation]) -> ExerciseScore:
        names = (TRUNK, SYMMETRY, DEPTH) if phase is SquatPhase.BOTTOM else (TRUNK, SYMMETRY)
        live_rules = tuple(self.phase_rule(name, phase) for name in names)
        applicable = dict(observations) if phase in ACTIVE_PHASES else {}
        if self.depth_angle_model is not None and phase in ACTIVE_PHASES:
            conditional = self._conditional_trunk(_Sample(0, phase, applicable), phase)
            if conditional is None:
                applicable.pop(TRUNK, None)
            else:
                obs, rule = conditional
                applicable[TRUNK] = obs
                live_rules = tuple(rule if r.name == TRUNK else r for r in live_rules)
        return RuleScorer(
            self.scorer.exercise, live_rules, minimum_coverage=self.scorer.minimum_coverage,
            minimum_observation_confidence=self.scorer.minimum_observation_confidence,
        ).score(applicable)

    def _qualified(self, sample: _Sample, name: str) -> MetricObservation | None:
        obs = sample.observations.get(name)
        return (obs if obs is not None and
                obs.confidence >= self.scorer.minimum_observation_confidence else None)

    def _conditional_trunk(self, sample: _Sample, phase: SquatPhase
                           ) -> tuple[MetricObservation, MetricRule] | None:
        assert self.depth_angle_model is not None
        depth, angle = self._qualified(sample, DEPTH), self._qualified(sample, TRUNK)
        if depth is None or angle is None:
            return None
        bounds = self.depth_angle_model.for_phase(phase.value).bounds(depth.value)
        if bounds is None:
            return None
        return (MetricObservation(angle.value, min(depth.confidence, angle.confidence)),
                _reuse_rule_bounds(self.rules[TRUNK], bounds[0], bounds[1]))

    def _conditional_segments(self, left: _Sample, right: _Sample, *,
                              endpoint_conditions=None) -> list[_Segment]:
        """Subdivide a short trusted interval without inventing valid observations."""
        if endpoint_conditions is None:
            a = self._conditional_trunk(left, left.phase)
            b = self._conditional_trunk(right, left.phase)
        else:
            a, b = endpoint_conditions
        rule = self.phase_rule(TRUNK, left.phase)
        if (a is None or b is None or
                right.timestamp_ns - left.timestamp_ns > self.config.maximum_interval_ms * 1e6):
            return [_Segment(left.timestamp_ns, right.timestamp_ns, left.phase, rule, None, None)]
        assert self.depth_angle_model is not None
        depth_a, depth_b = left.observations[DEPTH].value, right.observations[DEPTH].value
        band = self.depth_angle_model.for_phase(left.phase.value)
        fractions = band.breakpoints(depth_a, a[0].value, depth_b, b[0].value, rule.tolerance)
        original_scores = (self.scorer._component_score(a[1], a[0].value),
                           self.scorer._component_score(b[1], b[0].value))
        confirm = max(original_scores) < self.config.low_score_threshold
        # Only original endpoints can confirm a persistent problem. An isolated
        # spike must not become sustained evidence merely through subdivision.
        same_direction = ((a[0].value < a[1].ideal_min and b[0].value < b[1].ideal_min) or
                          (a[0].value > a[1].ideal_max and b[0].value > b[1].ideal_max))

        def endpoint(timestamp: int) -> tuple[MetricObservation, MetricRule, float]:
            fraction = (timestamp - left.timestamp_ns) / (right.timestamp_ns - left.timestamp_ns)
            depth = interpolate_depth(depth_a, depth_b, fraction)
            bounds = band.bounds(depth)
            assert bounds is not None
            observation = MetricObservation(a[0].value + fraction * (b[0].value - a[0].value),
                                            a[0].confidence + fraction * (b[0].confidence - a[0].confidence))
            conditional_rule = _reuse_rule_bounds(rule, bounds[0], bounds[1])
            return observation, conditional_rule, self.scorer._component_score(conditional_rule, observation.value)

        times = sorted({left.timestamp_ns + round(f * (right.timestamp_ns - left.timestamp_ns))
                        for f in fractions})
        result = []
        # Adjacent subdivisions share precisely the same integer timestamp.
        # Keep the original interpolation arithmetic, but construct/score that
        # endpoint once; never substitute an original observation at f=0/1.
        if len(times) < 2:
            return result
        ao, ar, aq = endpoint(times[0])
        for start, end in zip(times, times[1:]):
            bo, br, bq = endpoint(end)
            result.append(_Segment(start, end, left.phase, ar, ao, bo, aq, bq, br,
                                   confirm and same_direction))
            ao, ar, aq = bo, br, bq
        return result

    def _conditional_invalid_seconds(self) -> tuple[tuple[str, float], ...]:
        """Disjoint primary causes for unobserved conditional-angle time."""
        assert self.depth_angle_model is not None
        totals: dict[str, int] = {}
        for left, right in zip(self._samples, self._samples[1:]):
            if left.phase not in ACTIVE_PHASES:
                continue
            elapsed = right.timestamp_ns - left.timestamp_ns
            reason = None
            if elapsed > self.config.maximum_interval_ms * 1e6:
                reason = "interval_too_large"
            else:
                for name in (DEPTH, TRUNK):
                    observations = [s.observations.get(name) for s in (left, right)]
                    if any(o is None for o in observations):
                        reason = "missing_" + name
                    elif any(o.confidence < self.scorer.minimum_observation_confidence for o in observations):
                        reason = "low_confidence_" + name
                    elif name == DEPTH and any(
                            self.depth_angle_model.for_phase(left.phase.value).bounds(o.value) is None
                            for o in observations):
                        reason = "depth_outside_model_domain"
                    if reason is not None:
                        break
            if reason is not None:
                totals[reason] = totals.get(reason, 0) + elapsed
        return tuple((reason, ns / 1e9) for reason, ns in sorted(totals.items()))

    def _segments(self, name: str) -> list[_Segment]:
        segments = []
        previous_conditional = None
        for left, right in zip(self._samples, self._samples[1:]):
            if left.phase not in ACTIVE_PHASES:
                continue
            if name == TRUNK and self.depth_angle_model is not None:
                if (previous_conditional is not None and previous_conditional[0] is left
                        and previous_conditional[1] is left.phase):
                    a = previous_conditional[2]
                else:
                    a = self._conditional_trunk(left, left.phase)
                b = self._conditional_trunk(right, left.phase)
                previous_conditional = (right, left.phase, b)
                segments.extend(self._conditional_segments(left, right, endpoint_conditions=(a, b)))
                continue
            rule = self.phase_rule(name, left.phase)
            a, b = self._qualified(left, name), self._qualified(right, name)
            if right.timestamp_ns - left.timestamp_ns > self.config.maximum_interval_ms * 1e6:
                a = b = None
            if name == DEPTH and (left.phase is not SquatPhase.BOTTOM or
                                  right.phase is not SquatPhase.BOTTOM):
                continue
            segments.append(_Segment(
                left.timestamp_ns, right.timestamp_ns, left.phase, rule, a, b,
                self.scorer._component_score(rule, a.value) if a is not None else 0.0,
                self.scorer._component_score(rule, b.value) if b is not None else 0.0,
            ))
        return segments

    def _summarize(self, name: str, segments: list[_Segment], *,
                   median: bool = False) -> tuple[ScoreComponent | None, MetricAggregation]:
        applicable = sum(s.seconds for s in segments)
        valid = [s for s in segments if s.valid]
        duration = sum(s.seconds for s in valid)
        coverage = duration / applicable if applicable else 0.0
        low = sum(s.seconds * _below_fraction(s.left_score, s.right_score,
                                            self.config.low_score_threshold) for s in valid)
        phases = []
        for phase in ACTIVE_PHASES:
            ps = [s for s in segments if s.phase is phase]
            pv = [s for s in ps if s.valid]
            pd = sum(s.seconds for s in pv)
            if ps:
                rule = self.phase_rule(name, phase)
                if name == TRUNK and self.depth_angle_model is not None:
                    lower, upper = self.depth_angle_model.for_phase(phase.value).envelope
                    rule = _reuse_rule_bounds(rule, lower, upper)
                phases.append(PhaseScoreSummary(
                    phase.value, rule.ideal_min, rule.ideal_max,
                    sum(s.seconds for s in ps), pd,
                    sum(s.seconds * (s.left_score + s.right_score) / 2 for s in pv) / pd
                    if pd else None,
                    sum(s.seconds * _below_fraction(s.left_score, s.right_score,
                                                    self.config.low_score_threshold) for s in pv),
                ))
        deviations = []
        for direction in ("below", "above"):
            total = longest = run = 0.0
            previous_end = None
            affected_phases: set[str] = set()
            for segment in segments:
                confirmed = False
                if segment.valid and segment.confirm_deviation:
                    assert segment.left is not None and segment.right is not None
                    right_rule = segment.right_rule or segment.rule
                    same_direction = (
                        segment.left.value < segment.rule.ideal_min and segment.right.value < right_rule.ideal_min
                        if direction == "below" else
                        segment.left.value > segment.rule.ideal_max and segment.right.value > right_rule.ideal_max
                    )
                    confirmed = same_direction and max(segment.left_score, segment.right_score) < self.config.low_score_threshold
                if confirmed:
                    run = (run if previous_end == segment.start else 0.0) + segment.seconds
                    total += segment.seconds
                    longest = max(longest, run)
                    previous_end = segment.end
                    affected_phases.add(segment.phase.value)
                else:
                    run = 0.0
                    previous_end = None
            deviations.append(DeviationSummary(direction, total, longest,
                                               tuple(sorted(affected_phases))))
        metadata = MetricAggregation(
            name, ("time_mean_of_depth_conditioned_scores" if name == TRUNK and self.depth_angle_model is not None
                   else "time_median_of_sample_scores" if median else "time_mean_of_sample_scores"),
            ("time_mean_of_raw_angle_not_score_input_ideal_bounds_are_band_envelope"
             if name == TRUNK and self.depth_angle_model is not None else
             "time_mean_of_raw_observations_not_score_input"),
            segments[0].start if segments else None, segments[-1].end if segments else None,
            applicable, duration,
            coverage * 100, low, low / duration * 100 if duration else 0,
            tuple(phases), tuple(deviations),
        )
        minimum_duration = self.config.minimum_bottom_window_ms / 1000 if median else 0
        if not duration or applicable + 1e-12 < minimum_duration or coverage + 1e-12 < self.config.minimum_time_coverage:
            return None, metadata
        value = confidence = score = 0.0
        for segment in valid:
            assert segment.left is not None and segment.right is not None
            value += segment.seconds * (segment.left.value + segment.right.value) / 2
            confidence += segment.seconds * (segment.left.confidence + segment.right.confidence) / 2
            score += segment.seconds * (segment.left_score + segment.right_score) / 2
        rule = self.rules[name]
        component = ScoreComponent(
            name, rule.view, value / duration, rule.unit,
            min(p.ideal_min for p in phases), max(p.ideal_max for p in phases), rule.tolerance,
            _time_median(valid) if median else score / duration,
            confidence / duration * 100, rule.weight,
        )
        return component, metadata

    def finish(self, duration_seconds: float) -> ExerciseScore:
        if len(self._samples) < 2:
            raise ValueError("a completed process score needs an active interval")
        start, end = self._samples[0].timestamp_ns, self._samples[-1].timestamp_ns
        if not math.isfinite(duration_seconds) or abs(duration_seconds - (end - start) / 1e9) > 1e-9:
            raise ValueError("completed duration must match the active sample interval")
        trunk_segments, symmetry_segments = self._segments(TRUNK), self._segments(SYMMETRY)
        joint_valid_seconds = sum(a.seconds for a, b in zip(trunk_segments, symmetry_segments)
                                  if a.valid and b.valid)
        if self.depth_angle_model is not None:
            # The two streams have different subdivisions. Intersect their valid
            # intervals, rather than zipping unrelated segment indices.
            joint_ns = 0
            i = j = 0
            while i < len(trunk_segments) and j < len(symmetry_segments):
                a, b = trunk_segments[i], symmetry_segments[j]
                if a.valid and b.valid:
                    joint_ns += max(0, min(a.end, b.end) - max(a.start, b.start))
                if a.end <= b.end:
                    i += 1
                else:
                    j += 1
            joint_valid_seconds = joint_ns / 1e9
        time_coverage = joint_valid_seconds / ((end - start) / 1e9)
        depth_segments = self._segments(DEPTH)
        bottom = [sample for sample in self._samples if sample.phase is SquatPhase.BOTTOM]
        qualified_bottom = [s for s in bottom if self._qualified(s, DEPTH) is not None]
        if qualified_bottom:
            center = min(qualified_bottom, key=lambda s: self._qualified(s, DEPTH).value)
            radius = round(self.config.bottom_half_window_ms * 1e6)
            lo = max(bottom[0].timestamp_ns, center.timestamp_ns - radius)
            hi = min(bottom[-1].timestamp_ns, center.timestamp_ns + radius)
            depth_segments = [clipped for segment in depth_segments
                              if (clipped := segment.clip(lo, hi)) is not None]
        else:
            depth_segments = []
        components, metrics = [], []
        for name, segments in ((DEPTH, depth_segments), (TRUNK, trunk_segments),
                               (SYMMETRY, symmetry_segments)):
            component, metadata = self._summarize(name, segments, median=name == DEPTH)
            metrics.append(metadata)
            if component is not None:
                components.append(component)
        tempo = self.scorer.score({TEMPO: MetricObservation(duration_seconds, 1.0)})
        components.extend(tempo.components)
        total_weight = sum(r.weight for r in self.scorer.rules)
        observed_weight = sum(c.weight for c in components)
        weight_coverage = observed_weight / total_weight
        reasons = []
        if time_coverage + 1e-12 < self.config.minimum_time_coverage:
            reasons.append("insufficient_process_time_coverage")
        if not any(c.name == DEPTH for c in components):
            reasons.append("insufficient_bottom_window")
        if weight_coverage + 1e-12 < self.scorer.minimum_coverage:
            reasons.append("insufficient_metric_weight_coverage")
        valid = not reasons
        result = ExerciseScore(
            self.scorer.exercise,
            sum(c.score_percent * c.weight for c in components) / observed_weight if valid else None,
            sum(c.confidence_percent * c.weight for c in components) / observed_weight,
            weight_coverage * 100, valid, tuple(components),
            ScoreAggregation(self.algorithm, start, end, time_coverage * 100, tuple(reasons),
                             self.config.parameters(), tuple(metrics), self.depth_angle_model,
                             tuple((phase.value, sum(s.end - s.start for s in trunk_segments
                                                    if s.phase is phase) / 1e9)
                                   for phase in ACTIVE_PHASES) if self.depth_angle_model is not None else (),
                             self._conditional_invalid_seconds() if self.depth_angle_model is not None else (),
                             self.side_metric_policy),
        )
        self.clear()
        return result
