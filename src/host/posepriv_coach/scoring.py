"""Explainable rule-conformance scoring independent of a pose model."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import CameraView, ExerciseScore, ScoreComponent


@dataclass(frozen=True, slots=True)
class MetricRule:
    name: str
    view: CameraView
    unit: str
    ideal_min: float
    ideal_max: float
    tolerance: float
    weight: float

    def __post_init__(self) -> None:
        if not self.unit:
            raise ValueError("metric unit must not be empty")
        if not all(
            math.isfinite(value)
            for value in (self.ideal_min, self.ideal_max, self.tolerance, self.weight)
        ):
            raise ValueError("metric rule values must be finite")
        if self.ideal_min > self.ideal_max:
            raise ValueError("ideal_min must not exceed ideal_max")
        if self.tolerance <= 0.0:
            raise ValueError("tolerance must be positive")
        if self.weight <= 0.0:
            raise ValueError("weight must be positive")


@dataclass(frozen=True, slots=True)
class MetricObservation:
    value: float
    confidence: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.value) or not math.isfinite(self.confidence):
            raise ValueError("observation value and confidence must be finite")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("observation confidence must be within [0, 1]")


class RuleScorer:
    def __init__(
        self,
        exercise: str,
        rules: Sequence[MetricRule],
        *,
        minimum_coverage: float = 0.8,
        minimum_observation_confidence: float = 0.5,
    ) -> None:
        if not rules:
            raise ValueError("at least one metric rule is required")
        if not 0.0 <= minimum_coverage <= 1.0:
            raise ValueError("minimum_coverage must be within [0, 1]")
        if not 0.0 <= minimum_observation_confidence <= 1.0:
            raise ValueError("minimum_observation_confidence must be within [0, 1]")
        self.exercise = exercise
        self.rules = tuple(rules)
        self.minimum_coverage = minimum_coverage
        self.minimum_observation_confidence = minimum_observation_confidence

    def score(self, observations: Mapping[str, MetricObservation]) -> ExerciseScore:
        total_weight = sum(rule.weight for rule in self.rules)
        observed_weight = 0.0
        weighted_score = 0.0
        weighted_confidence = 0.0
        components: list[ScoreComponent] = []

        for rule in self.rules:
            observation = observations.get(rule.name)
            if observation is None or observation.confidence < self.minimum_observation_confidence:
                continue
            component_score = self._component_score(rule, observation.value)
            observed_weight += rule.weight
            weighted_score += component_score * rule.weight
            weighted_confidence += observation.confidence * 100.0 * rule.weight
            components.append(
                ScoreComponent(
                    name=rule.name,
                    view=rule.view,
                    observed_value=observation.value,
                    unit=rule.unit,
                    ideal_min=rule.ideal_min,
                    ideal_max=rule.ideal_max,
                    tolerance=rule.tolerance,
                    score_percent=component_score,
                    confidence_percent=observation.confidence * 100.0,
                    weight=rule.weight,
                )
            )

        coverage = observed_weight / total_weight
        valid = coverage >= self.minimum_coverage and observed_weight > 0.0
        conformance = weighted_score / observed_weight if valid else None
        confidence = weighted_confidence / observed_weight if observed_weight else 0.0
        return ExerciseScore(
            exercise=self.exercise,
            conformance_percent=conformance,
            confidence_percent=confidence,
            coverage_percent=coverage * 100.0,
            valid=valid,
            components=tuple(components),
        )

    @staticmethod
    def _component_score(rule: MetricRule, value: float) -> float:
        if rule.ideal_min <= value <= rule.ideal_max:
            return 100.0
        distance = rule.ideal_min - value if value < rule.ideal_min else value - rule.ideal_max
        return max(0.0, 100.0 * (1.0 - distance / rule.tolerance))


def load_rule_scorer(path: str | Path) -> RuleScorer:
    """Load a versioned exercise rule file without binding the package to one repository path."""

    raw: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema_version") != 2:
        raise ValueError("unsupported exercise-rule schema")
    rules = tuple(
        MetricRule(
            name=entry["name"],
            view=CameraView(entry["view"]),
            unit=str(entry["unit"]),
            ideal_min=float(entry["ideal_min"]),
            ideal_max=float(entry["ideal_max"]),
            tolerance=float(entry["tolerance"]),
            weight=float(entry["weight"]),
        )
        for entry in raw["rules"]
    )
    return RuleScorer(
        exercise=raw["exercise"],
        rules=rules,
        minimum_coverage=float(raw["minimum_coverage"]),
        minimum_observation_confidence=float(raw["minimum_observation_confidence"]),
    )
