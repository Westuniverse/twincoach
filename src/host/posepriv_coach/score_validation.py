"""Evaluate provisional exercise rules against locally labelled pose traces."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path

from .pl_metrics import JointMetricBackend, ReferenceJointMetricBackend
from .pose_trace import read_pose_trace
from .scoring import load_rule_scorer
from .session import load_squat_session_processor


@dataclass(frozen=True, slots=True)
class ScoreValidationCase:
    sample_id: str
    trace: str
    expected_repetitions: int
    observed_repetitions: int
    count_matches: bool
    missed_repetitions: int
    extra_repetitions: int
    expert_scores: tuple[float, ...]
    observed_scores: tuple[float | None, ...]
    absolute_errors: tuple[float, ...]
    score_comparable: bool
    score_labelled_repetition_count: int
    score_compared_repetition_count: int
    score_coverage_percent: float | None
    invalid_observed_score_count: int
    expected_issues: tuple[tuple[str, ...], ...]
    observed_issues: tuple[tuple[str, ...], ...]
    issue_labels_comparable: bool
    issue_labelled_repetition_count: int
    issue_compared_repetition_count: int
    issue_coverage_percent: float | None
    issue_exact_match_count: int
    issue_true_positive: int
    issue_false_positive: int
    issue_false_negative: int


@dataclass(frozen=True, slots=True)
class ScoreValidationResult:
    exercise: str
    sample_count: int
    count_exact_match_percent: float
    expected_repetition_count: int
    observed_repetition_count: int
    missed_repetition_count: int
    extra_repetition_count: int
    mean_absolute_count_error: float
    score_labelled_repetition_count: int
    compared_repetition_count: int
    score_coverage_percent: float | None
    invalid_observed_score_count: int
    score_mean_absolute_error: float | None
    issue_labelled_repetition_count: int
    issue_compared_repetition_count: int
    issue_coverage_percent: float | None
    issue_exact_match_percent: float | None
    issue_precision_percent: float | None
    issue_recall_percent: float | None
    issue_f1_percent: float | None
    cases: tuple[ScoreValidationCase, ...]


def evaluate_score_manifest(
    manifest_path: str | Path,
    rule_path: str | Path,
    *,
    joint_metrics_backend: JointMetricBackend | None = None,
) -> ScoreValidationResult:
    """Replay every labelled trace and compare count plus optional expert scores."""

    manifest_file = Path(manifest_path)
    raw = json.loads(manifest_file.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1 or raw.get("exercise") != "squat":
        raise ValueError("unsupported score-validation manifest")
    samples = raw.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("score-validation manifest must contain samples")

    backend = joint_metrics_backend or ReferenceJointMetricBackend()
    scorer = load_rule_scorer(rule_path)
    if scorer.exercise != "squat":
        raise ValueError("score-validation rules must describe squat")
    allowed_issues = {rule.name for rule in scorer.rules}
    seen_ids: set[str] = set()
    cases: list[ScoreValidationCase] = []
    all_errors: list[float] = []
    total_score_labelled_repetitions = 0
    total_invalid_observed_scores = 0
    total_issue_labelled_repetitions = 0
    total_issue_repetitions = 0
    total_issue_exact_matches = 0
    total_issue_true_positive = 0
    total_issue_false_positive = 0
    total_issue_false_negative = 0

    for index, entry in enumerate(samples):
        if not isinstance(entry, dict):
            raise ValueError(f"invalid validation sample at index {index}")
        sample_id = str(entry.get("id", "")).strip()
        if not sample_id or sample_id in seen_ids:
            raise ValueError(f"sample id must be non-empty and unique: {sample_id!r}")
        seen_ids.add(sample_id)
        trace_value = entry.get("trace")
        if not isinstance(trace_value, str) or not trace_value:
            raise ValueError(f"sample {sample_id} is missing trace")
        expected = entry.get("expected_repetitions")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise ValueError(f"sample {sample_id} has invalid expected_repetitions")
        expert_scores = _expert_scores(entry.get("expert_scores", []), sample_id)
        if expert_scores and len(expert_scores) != expected:
            raise ValueError(
                f"sample {sample_id} expert_scores must match expected_repetitions"
            )
        has_issue_labels = "expected_issues" in entry
        expected_issues = (
            _expected_issues(
                entry.get("expected_issues"),
                sample_id,
                expected,
                allowed_issues,
            )
            if has_issue_labels
            else ()
        )

        trace_path = (manifest_file.parent / trace_value).resolve()
        processor = load_squat_session_processor(
            rule_path,
            joint_metrics_backend=backend,
        )
        observed_scores: list[float | None] = []
        observed_issues: list[tuple[str, ...]] = []
        for frame in read_pose_trace(trace_path):
            update = processor.push(frame)
            if update is not None and update.repetition_completed:
                score = update.repetition_score
                observed_scores.append(
                    score.conformance_percent
                    if score is not None and score.valid
                    else None
                )
                observed_issues.append(tuple(sorted(cue.metric for cue in update.cues)))

        observed = len(observed_scores)
        count_matches = observed == expected
        missed_repetitions = max(expected - observed, 0)
        extra_repetitions = max(observed - expected, 0)
        invalid_observed_score_count = sum(score is None for score in observed_scores)
        total_invalid_observed_scores += invalid_observed_score_count
        score_labelled_repetition_count = len(expert_scores)
        total_score_labelled_repetitions += score_labelled_repetition_count
        score_comparable = bool(expert_scores) and count_matches
        errors = (
            tuple(
                abs(float(observed_score) - expert_score)
                for observed_score, expert_score in zip(
                    observed_scores,
                    expert_scores,
                    strict=True,
                )
                if observed_score is not None
            )
            if score_comparable
            else ()
        )
        all_errors.extend(errors)
        score_compared_repetition_count = len(errors)
        score_coverage_percent = _percent(
            score_compared_repetition_count,
            score_labelled_repetition_count,
        )
        issue_labels_comparable = has_issue_labels and count_matches
        issue_labelled_repetition_count = expected if has_issue_labels else 0
        total_issue_labelled_repetitions += issue_labelled_repetition_count
        issue_exact_match_count = 0
        issue_true_positive = 0
        issue_false_positive = 0
        issue_false_negative = 0
        if issue_labels_comparable:
            for expected_set, observed_set in zip(
                expected_issues,
                observed_issues,
                strict=True,
            ):
                expected_names = set(expected_set)
                observed_names = set(observed_set)
                if expected_names == observed_names:
                    issue_exact_match_count += 1
                issue_true_positive += len(expected_names & observed_names)
                issue_false_positive += len(observed_names - expected_names)
                issue_false_negative += len(expected_names - observed_names)
            total_issue_repetitions += expected
            total_issue_exact_matches += issue_exact_match_count
            total_issue_true_positive += issue_true_positive
            total_issue_false_positive += issue_false_positive
            total_issue_false_negative += issue_false_negative
        issue_compared_repetition_count = expected if issue_labels_comparable else 0
        issue_coverage_percent = _percent(
            issue_compared_repetition_count,
            issue_labelled_repetition_count,
        )
        cases.append(
            ScoreValidationCase(
                sample_id=sample_id,
                trace=trace_value,
                expected_repetitions=expected,
                observed_repetitions=observed,
                count_matches=count_matches,
                missed_repetitions=missed_repetitions,
                extra_repetitions=extra_repetitions,
                expert_scores=expert_scores,
                observed_scores=tuple(observed_scores),
                absolute_errors=errors,
                score_comparable=score_comparable,
                score_labelled_repetition_count=score_labelled_repetition_count,
                score_compared_repetition_count=score_compared_repetition_count,
                score_coverage_percent=score_coverage_percent,
                invalid_observed_score_count=invalid_observed_score_count,
                expected_issues=expected_issues,
                observed_issues=tuple(observed_issues),
                issue_labels_comparable=issue_labels_comparable,
                issue_labelled_repetition_count=issue_labelled_repetition_count,
                issue_compared_repetition_count=issue_compared_repetition_count,
                issue_coverage_percent=issue_coverage_percent,
                issue_exact_match_count=issue_exact_match_count,
                issue_true_positive=issue_true_positive,
                issue_false_positive=issue_false_positive,
                issue_false_negative=issue_false_negative,
            )
        )

    count_matches = sum(case.count_matches for case in cases)
    expected_repetition_count = sum(case.expected_repetitions for case in cases)
    observed_repetition_count = sum(case.observed_repetitions for case in cases)
    missed_repetition_count = sum(case.missed_repetitions for case in cases)
    extra_repetition_count = sum(case.extra_repetitions for case in cases)
    return ScoreValidationResult(
        exercise="squat",
        sample_count=len(cases),
        count_exact_match_percent=count_matches * 100.0 / len(cases),
        expected_repetition_count=expected_repetition_count,
        observed_repetition_count=observed_repetition_count,
        missed_repetition_count=missed_repetition_count,
        extra_repetition_count=extra_repetition_count,
        mean_absolute_count_error=(
            sum(abs(case.observed_repetitions - case.expected_repetitions) for case in cases)
            / len(cases)
        ),
        score_labelled_repetition_count=total_score_labelled_repetitions,
        compared_repetition_count=len(all_errors),
        score_coverage_percent=_percent(
            len(all_errors),
            total_score_labelled_repetitions,
        ),
        invalid_observed_score_count=total_invalid_observed_scores,
        score_mean_absolute_error=(sum(all_errors) / len(all_errors) if all_errors else None),
        issue_labelled_repetition_count=total_issue_labelled_repetitions,
        issue_compared_repetition_count=total_issue_repetitions,
        issue_coverage_percent=_percent(
            total_issue_repetitions,
            total_issue_labelled_repetitions,
        ),
        issue_exact_match_percent=(
            total_issue_exact_matches * 100.0 / total_issue_repetitions
            if total_issue_repetitions
            else None
        ),
        issue_precision_percent=_percent(
            total_issue_true_positive,
            total_issue_true_positive + total_issue_false_positive,
        ),
        issue_recall_percent=_percent(
            total_issue_true_positive,
            total_issue_true_positive + total_issue_false_negative,
        ),
        issue_f1_percent=_f1_percent(
            total_issue_true_positive,
            total_issue_false_positive,
            total_issue_false_negative,
        ),
        cases=tuple(cases),
    )


def _expert_scores(value: object, sample_id: str) -> tuple[float, ...]:
    if not isinstance(value, list):
        raise ValueError(f"sample {sample_id} expert_scores must be a list")
    scores = tuple(float(item) for item in value)
    if any(not math.isfinite(score) or not 0.0 <= score <= 100.0 for score in scores):
        raise ValueError(f"sample {sample_id} expert score must be within [0, 100]")
    return scores


def _expected_issues(
    value: object,
    sample_id: str,
    expected_repetitions: int,
    allowed_issues: set[str],
) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list) or len(value) != expected_repetitions:
        raise ValueError(
            f"sample {sample_id} expected_issues must match expected_repetitions"
        )
    issue_sets: list[tuple[str, ...]] = []
    for repetition_index, raw_issues in enumerate(value, start=1):
        if not isinstance(raw_issues, list):
            raise ValueError(
                f"sample {sample_id} repetition {repetition_index} issues must be a list"
            )
        issues = tuple(str(issue).strip() for issue in raw_issues)
        if any(not issue for issue in issues) or len(set(issues)) != len(issues):
            raise ValueError(
                f"sample {sample_id} repetition {repetition_index} issues must be unique"
            )
        unknown = set(issues) - allowed_issues
        if unknown:
            raise ValueError(
                f"sample {sample_id} has unknown expected issue: {sorted(unknown)[0]}"
            )
        issue_sets.append(tuple(sorted(issues)))
    return tuple(issue_sets)


def _percent(numerator: int, denominator: int) -> float | None:
    return numerator * 100.0 / denominator if denominator else None


def _f1_percent(true_positive: int, false_positive: int, false_negative: int) -> float | None:
    denominator = 2 * true_positive + false_positive + false_negative
    return 2 * true_positive * 100.0 / denominator if denominator else None
