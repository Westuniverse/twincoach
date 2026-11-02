"""Deterministic one-variable sensitivity checks for provisional squat rules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .scoring import MetricObservation, load_rule_scorer


@dataclass(frozen=True, slots=True)
class ScoreSensitivityCase:
    name: str
    changed_metric: str | None
    conformance_percent: float
    component_scores: tuple[tuple[str, float], ...]


_BASELINE = {
    "side_depth_ratio": MetricObservation(1.0, 0.95),
    "side_trunk_angle_deg": MetricObservation(22.0, 0.95),
    "front_knee_symmetry": MetricObservation(1.0, 0.95),
    "tempo_seconds": MetricObservation(3.0, 1.0),
}

_PERTURBATIONS = (
    ("shallow", "side_depth_ratio", MetricObservation(1.4, 0.95)),
    ("excessive_trunk_lean", "side_trunk_angle_deg", MetricObservation(55.0, 0.95)),
    ("knee_asymmetry", "front_knee_symmetry", MetricObservation(1.4, 0.95)),
    ("too_fast", "tempo_seconds", MetricObservation(0.8, 1.0)),
)


def evaluate_squat_rule_sensitivity(
    rule_path: str | Path,
) -> tuple[ScoreSensitivityCase, ...]:
    """Evaluate the ideal baseline and four isolated out-of-range observations."""

    scorer = load_rule_scorer(rule_path)
    if scorer.exercise != "squat":
        raise ValueError("score sensitivity requires squat rules")
    raw_cases = [("baseline", None, _BASELINE)]
    for name, metric, observation in _PERTURBATIONS:
        values = dict(_BASELINE)
        values[metric] = observation
        raw_cases.append((name, metric, values))

    cases = []
    for name, changed_metric, observations in raw_cases:
        score = scorer.score(observations)
        if not score.valid or score.conformance_percent is None:
            raise ValueError(f"sensitivity case {name!r} did not produce a valid score")
        cases.append(
            ScoreSensitivityCase(
                name=name,
                changed_metric=changed_metric,
                conformance_percent=round(score.conformance_percent, 6),
                component_scores=tuple(
                    (component.name, round(component.score_percent, 6))
                    for component in score.components
                ),
            )
        )
    return tuple(cases)
