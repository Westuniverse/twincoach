"""Compare two replay session reports at the scoring and synchronization boundary."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

from .session_report import REPORT_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class SessionReportComparison:
    passed: bool
    baseline_backend: str
    candidate_backend: str
    repetitions_compared: int
    max_conformance_difference: float
    max_component_score_difference: float
    max_observation_difference_by_unit: tuple[tuple[str, float], ...]
    baseline_valid_triangulated_percent: float | None
    candidate_valid_triangulated_percent: float | None
    baseline_finite_ray_gap_percent: float | None
    candidate_finite_ray_gap_percent: float | None
    baseline_accepted_finite_ray_percent: float | None
    candidate_accepted_finite_ray_percent: float | None
    baseline_mean_ray_gap_world_units: float | None
    candidate_mean_ray_gap_world_units: float | None
    baseline_p95_ray_gap_world_units: float | None
    candidate_p95_ray_gap_world_units: float | None
    baseline_mean_ray_angle_degrees: float | None
    candidate_mean_ray_angle_degrees: float | None
    baseline_p05_ray_angle_degrees: float | None
    candidate_p05_ray_angle_degrees: float | None
    issues: tuple[str, ...]

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "comparison": "coach_session_report",
            "passed": self.passed,
            "baseline_backend": self.baseline_backend,
            "candidate_backend": self.candidate_backend,
            "repetitions_compared": self.repetitions_compared,
            "max_conformance_difference": self.max_conformance_difference,
            "max_component_score_difference": self.max_component_score_difference,
            "max_observation_difference_by_unit": dict(
                self.max_observation_difference_by_unit
            ),
            "3d_geometry_quality": {
                "baseline_valid_triangulated_percent": (
                    self.baseline_valid_triangulated_percent
                ),
                "candidate_valid_triangulated_percent": (
                    self.candidate_valid_triangulated_percent
                ),
                "baseline_finite_ray_gap_percent": self.baseline_finite_ray_gap_percent,
                "candidate_finite_ray_gap_percent": self.candidate_finite_ray_gap_percent,
                "baseline_accepted_finite_ray_percent": (
                    self.baseline_accepted_finite_ray_percent
                ),
                "candidate_accepted_finite_ray_percent": (
                    self.candidate_accepted_finite_ray_percent
                ),
                "baseline_mean_ray_gap_world_units": (
                    self.baseline_mean_ray_gap_world_units
                ),
                "candidate_mean_ray_gap_world_units": (
                    self.candidate_mean_ray_gap_world_units
                ),
                "baseline_p95_ray_gap_world_units": (
                    self.baseline_p95_ray_gap_world_units
                ),
                "candidate_p95_ray_gap_world_units": (
                    self.candidate_p95_ray_gap_world_units
                ),
                "baseline_mean_ray_angle_degrees": (
                    self.baseline_mean_ray_angle_degrees
                ),
                "candidate_mean_ray_angle_degrees": (
                    self.candidate_mean_ray_angle_degrees
                ),
                "baseline_p05_ray_angle_degrees": (
                    self.baseline_p05_ray_angle_degrees
                ),
                "candidate_p05_ray_angle_degrees": (
                    self.candidate_p05_ray_angle_degrees
                ),
            },
            "issues": list(self.issues),
        }


def compare_session_reports(
    baseline_path: str | Path,
    candidate_path: str | Path,
    *,
    score_tolerance: float = 0.05,
    angle_tolerance_deg: float = 0.1,
    scalar_tolerance: float = 0.001,
) -> SessionReportComparison:
    """Compare replay reports without treating their synthetic timings as performance."""

    for name, value in (
        ("score_tolerance", score_tolerance),
        ("angle_tolerance_deg", angle_tolerance_deg),
        ("scalar_tolerance", scalar_tolerance),
    ):
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(f"{name} must be finite and non-negative")

    baseline = _read_report(baseline_path)
    candidate = _read_report(candidate_path)
    issues: list[str] = []
    max_conformance = 0.0
    max_component_score = 0.0
    max_observation_by_unit: dict[str, float] = {}

    for key, label in (("exercise", "动作"), ("rule_set", "规则集")):
        if baseline.header.get(key) != candidate.header.get(key):
            issues.append(f"报告头{label}不一致")
    if baseline.header.get("configuration") != candidate.header.get("configuration"):
        issues.append("报告头运行配置不一致")

    if len(baseline.repetitions) != len(candidate.repetitions):
        issues.append(
            "完整动作数量不一致："
            f"{len(baseline.repetitions)} != {len(candidate.repetitions)}"
        )

    for index, (base_rep, cand_rep) in enumerate(
        zip(baseline.repetitions, candidate.repetitions),
        start=1,
    ):
        prefix = f"第{index}条完整动作"
        for key in ("repetition_count", "front_frame_id", "side_frame_id"):
            if base_rep.get(key) != cand_rep.get(key):
                issues.append(f"{prefix}的 {key} 不一致")

        base_score = _mapping(base_rep.get("score"), f"{prefix}基线分数")
        cand_score = _mapping(cand_rep.get("score"), f"{prefix}候选分数")
        if base_score.get("valid") != cand_score.get("valid"):
            issues.append(f"{prefix}的评分有效性不一致")
        base_aggregation, cand_aggregation = base_score.get("aggregation"), cand_score.get("aggregation")
        if base_aggregation is not None or cand_aggregation is not None:
            if not isinstance(base_aggregation, dict) or not isinstance(cand_aggregation, dict):
                issues.append(f"{prefix}评分算法不一致（单帧/过程）")
            elif base_aggregation.get("algorithm") != cand_aggregation.get("algorithm"):
                issues.append(f"{prefix}评分算法版本不一致")
            else:
                if base_aggregation.get("parameters") != cand_aggregation.get("parameters"):
                    issues.append(f"{prefix}过程评分配置不一致")
                if base_aggregation.get("depth_angle_model") != cand_aggregation.get("depth_angle_model"):
                    issues.append(f"{prefix}深度角度条件模型不一致")
                if base_aggregation.get("side_metric_policy", "legacy_chain_sum_v0") != cand_aggregation.get("side_metric_policy", "legacy_chain_sum_v0"):
                    issues.append(f"{prefix}指标观测选择策略不一致")
                _compare_aggregation(base_aggregation, cand_aggregation, prefix,
                                     score_tolerance, scalar_tolerance, issues)
        base_total, cand_total = base_score.get("conformance_percent"), cand_score.get("conformance_percent")
        if ((base_score.get("valid") is True and base_total is None) or
                (cand_score.get("valid") is True and cand_total is None)):
            raise ValueError(f"{prefix}有效评分必须包含总分")
        if (base_total is None) != (cand_total is None):
            issues.append(f"{prefix}总分缺失状态不一致")

        if base_total is not None and cand_total is not None:
            difference = abs(_number(base_total, prefix) - _number(cand_total, prefix))
            max_conformance = max(max_conformance, difference)
            if difference > score_tolerance:
                issues.append(f"{prefix}总规范度差 {difference:.6g} 超过容差")

        base_components = _component_map(base_score, f"{prefix}基线")
        cand_components = _component_map(cand_score, f"{prefix}候选")
        if base_components.keys() != cand_components.keys():
            issues.append(f"{prefix}评分分项集合不一致")

        for name in sorted(base_components.keys() & cand_components.keys()):
            base_component = base_components[name]
            cand_component = cand_components[name]
            unit = str(base_component.get("unit"))
            if unit != str(cand_component.get("unit")):
                issues.append(f"{prefix}分项 {name} 的单位不一致")
                continue
            if base_component.get("view") != cand_component.get("view"):
                issues.append(f"{prefix}分项 {name} 的视角不一致")

            observation_difference = abs(
                _number(base_component.get("observed_value"), name)
                - _number(cand_component.get("observed_value"), name)
            )
            max_observation_by_unit[unit] = max(
                max_observation_by_unit.get(unit, 0.0),
                observation_difference,
            )
            observation_tolerance = (
                angle_tolerance_deg if unit == "deg" else scalar_tolerance
            )
            if observation_difference > observation_tolerance:
                issues.append(
                    f"{prefix}分项 {name} 观测值差 {observation_difference:.6g} "
                    f"{unit} 超过容差"
                )

            component_score_difference = abs(
                _number(base_component.get("score_percent"), name)
                - _number(cand_component.get("score_percent"), name)
            )
            max_component_score = max(
                max_component_score,
                component_score_difference,
            )
            if component_score_difference > score_tolerance:
                issues.append(
                    f"{prefix}分项 {name} 分数差 "
                    f"{component_score_difference:.6g} 超过容差"
                )

        if _cue_signature(base_rep) != _cue_signature(cand_rep):
            issues.append(f"{prefix}纠正建议不一致")

    _compare_summary_counts(baseline.summary, candidate.summary, issues)
    base_geometry = _geometry_quality(baseline.summary)
    candidate_geometry = _geometry_quality(candidate.summary)
    return SessionReportComparison(
        passed=not issues,
        baseline_backend=str(baseline.header.get("joint_metric_backend", "unknown")),
        candidate_backend=str(candidate.header.get("joint_metric_backend", "unknown")),
        repetitions_compared=min(
            len(baseline.repetitions),
            len(candidate.repetitions),
        ),
        max_conformance_difference=max_conformance,
        max_component_score_difference=max_component_score,
        max_observation_difference_by_unit=tuple(sorted(max_observation_by_unit.items())),
        baseline_valid_triangulated_percent=base_geometry[0],
        candidate_valid_triangulated_percent=candidate_geometry[0],
        baseline_finite_ray_gap_percent=base_geometry[1],
        candidate_finite_ray_gap_percent=candidate_geometry[1],
        baseline_accepted_finite_ray_percent=base_geometry[2],
        candidate_accepted_finite_ray_percent=candidate_geometry[2],
        baseline_mean_ray_gap_world_units=base_geometry[3],
        candidate_mean_ray_gap_world_units=candidate_geometry[3],
        baseline_p95_ray_gap_world_units=base_geometry[4],
        candidate_p95_ray_gap_world_units=candidate_geometry[4],
        baseline_mean_ray_angle_degrees=base_geometry[5],
        candidate_mean_ray_angle_degrees=candidate_geometry[5],
        baseline_p05_ray_angle_degrees=base_geometry[6],
        candidate_p05_ray_angle_degrees=candidate_geometry[6],
        issues=tuple(issues),
    )


def _compare_aggregation(base: Any, candidate: Any, path: str,
                         score_tolerance: float, scalar_tolerance: float,
                         issues: list[str]) -> None:
    """Compare optional process metadata instead of silently dropping its semantics."""
    if isinstance(base, dict) and isinstance(candidate, dict):
        if base.keys() != candidate.keys():
            issues.append(f"{path}过程统计字段不一致")
        for key in base.keys() & candidate.keys():
            _compare_aggregation(base[key], candidate[key], f"{path}.{key}",
                                 score_tolerance, scalar_tolerance, issues)
    elif isinstance(base, list) and isinstance(candidate, list):
        if len(base) != len(candidate):
            issues.append(f"{path}过程统计长度不一致")
        for index, (a, b) in enumerate(zip(base, candidate)):
            _compare_aggregation(a, b, f"{path}[{index}]", score_tolerance,
                                 scalar_tolerance, issues)
    elif (isinstance(base, (int, float)) and not isinstance(base, bool) and
          isinstance(candidate, (int, float)) and not isinstance(candidate, bool)):
        tolerance = score_tolerance if "percent" in path else scalar_tolerance
        if "timestamp_ns" in path:
            tolerance = 0.0
        if not math.isfinite(base) or not math.isfinite(candidate) or abs(base - candidate) > tolerance:
            issues.append(f"{path}过程统计数值不一致")
    elif base != candidate:
        issues.append(f"{path}过程统计不一致")


@dataclass(frozen=True, slots=True)
class _SessionReport:
    header: dict[str, Any]
    repetitions: tuple[dict[str, Any], ...]
    summary: dict[str, Any]


def _read_report(path: str | Path) -> _SessionReport:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON at report line {line_number}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"report line {line_number} is not an object")
        records.append(record)
    if not records:
        raise ValueError("session report is empty")
    header = records[0]
    if (
        header.get("record_type") != "coach_session_header"
        or header.get("schema_version") != REPORT_SCHEMA_VERSION
    ):
        raise ValueError("unsupported session report header or schema version")
    summaries = [
        record for record in records if record.get("record_type") == "session_summary"
    ]
    if len(summaries) != 1:
        raise ValueError("session report must contain exactly one summary")
    repetitions = tuple(
        record
        for record in records
        if record.get("record_type") == "completed_repetition"
    )
    known_count = 1 + len(repetitions) + len(summaries)
    if known_count != len(records):
        raise ValueError("session report contains an unsupported record type")
    return _SessionReport(header, repetitions, summaries[0])


def _component_map(score: dict[str, Any], context: str) -> dict[str, dict[str, Any]]:
    raw_components = score.get("components")
    if not isinstance(raw_components, list):
        raise ValueError(f"{context} components must be a list")
    components: dict[str, dict[str, Any]] = {}
    for raw in raw_components:
        component = _mapping(raw, f"{context} component")
        name = str(component.get("name", ""))
        if not name or name in components:
            raise ValueError(f"{context} component names must be non-empty and unique")
        components[name] = component
    return components


def _cue_signature(repetition: dict[str, Any]) -> tuple[tuple[object, object], ...]:
    cues = repetition.get("cues")
    if not isinstance(cues, list):
        raise ValueError("completed repetition cues must be a list")
    return tuple(
        (_mapping(cue, "cue").get("metric"), _mapping(cue, "cue").get("direction"))
        for cue in cues
    )


def _compare_summary_counts(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    issues: list[str],
) -> None:
    base_pipeline = _mapping(baseline.get("pipeline"), "baseline pipeline summary")
    cand_pipeline = _mapping(candidate.get("pipeline"), "candidate pipeline summary")
    for key in (
        "processed_frames",
        "processed_front_frames",
        "processed_side_frames",
        "synchronized_updates",
        "dropped_front",
        "dropped_side",
    ):
        if base_pipeline.get(key) != cand_pipeline.get(key):
            issues.append(f"会话摘要 {key} 不一致")

    base_sync = _mapping(baseline.get("pose_sync"), "baseline pose-sync summary")
    cand_sync = _mapping(candidate.get("pose_sync"), "candidate pose-sync summary")
    for key in ("paired_frames", "dropped_front", "dropped_side"):
        if base_sync.get(key) != cand_sync.get(key):
            issues.append(f"同步摘要 {key} 不一致")


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def _geometry_quality(
    summary: dict[str, Any],
) -> tuple[
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
]:
    avatar = summary.get("avatar_runtime")
    if not isinstance(avatar, dict):
        return (None, None, None, None, None, None, None)
    return (
        _optional_number(avatar.get("valid_triangulated_percent")),
        _optional_number(avatar.get("finite_ray_gap_percent")),
        _optional_number(avatar.get("accepted_finite_ray_percent")),
        _optional_number(avatar.get("mean_ray_gap_world_units")),
        _optional_number(avatar.get("p95_ray_gap_world_units")),
        _optional_number(avatar.get("mean_ray_angle_degrees")),
        _optional_number(avatar.get("p05_ray_angle_degrees")),
    )


def _optional_number(value: object) -> float | None:
    if value is None:
        return None
    return _number(value, "3D geometry quality")


def _number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must contain numeric values")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{context} contains a non-finite value")
    return result
