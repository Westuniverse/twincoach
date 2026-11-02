"""Reproducible four-arm scoring study and derived trajectories, without images."""

from __future__ import annotations

import json
from pathlib import Path

from .depth_angle_scoring import DepthAngleModel
from .exercises.squat import (LEGACY_SIDE_METRIC_POLICY, PER_METRIC_SIDE_POLICY,
                              extract_squat_observations, select_side_metrics)
from .exercises.squat_tracker import SquatPhase as Phase
from .feedback_zh import format_feedback_cue_zh
from .pose_trace import read_pose_trace
from .scoring import MetricObservation as Obs
from .session import load_squat_session_processor
from .session_report import _score_record

ARMS = ("baseline", "band_only", "selection_only", "combined")
ARM_LABELS = ("原过程评分", "仅深度角度允许带", "仅优化观测选择", "完整深度角度评分")
METRICS = ("side_depth_ratio", "side_trunk_angle_deg", "front_knee_symmetry")
SYNTHETIC_CASES = ("good", "early_lean", "bottom_lean", "single_spike", "sustained_lean",
                   "shallow", "fast", "depth_blind")


def processors(baseline: Path, combined: Path):
    a, b = (json.loads(p.read_text(encoding="utf-8")) for p in (baseline, combined))
    excluded = {"status", "depth_angle_scoring", "side_metric_policy"}
    if ({k: v for k, v in a.items() if k not in excluded} !=
            {k: v for k, v in b.items() if k not in excluded}):
        raise ValueError("four-arm study requires identical tracking, rules and coverage thresholds")
    if "depth_angle_scoring" in a or a.get("side_metric_policy", LEGACY_SIDE_METRIC_POLICY) != LEGACY_SIDE_METRIC_POLICY:
        raise ValueError("study baseline must use the original observations and process scorer")
    if b.get("side_metric_policy") != PER_METRIC_SIDE_POLICY:
        raise ValueError("combined configuration must explicitly enable per-metric observation selection")
    model = DepthAngleModel.from_record(b["depth_angle_scoring"])
    values = {}
    for arm in ARMS:
        values[arm] = load_squat_session_processor(
            baseline, depth_angle_model=model if arm in ("band_only", "combined") else None,
            side_metric_policy=PER_METRIC_SIDE_POLICY if arm in ("selection_only", "combined") else LEGACY_SIDE_METRIC_POLICY)
    return values, {"baseline": a, "combined": b}


def synthetic_samples(name):
    speed = .5 if name == "fast" else 1.0
    for ms in range(0, 3001, 100):
        phase = (Phase.DESCENDING if ms < 1000 else Phase.BOTTOM if ms < 2000 else
                 Phase.ASCENDING if ms < 3000 else Phase.STANDING)
        depth = 2 - ms / 1000 if ms < 1000 else 1 if ms < 2000 else 1 + (ms - 2000) / 1000
        if name == "shallow":
            depth = max(1.4, depth)
        angle = 35 if (name == "early_lean" and ms <= 600 or
                       name == "bottom_lean" and 1000 <= ms <= 1600) else 20
        if (name == "single_spike" and ms == 400 or
                name == "sustained_lean" and 200 <= ms <= 600):
            angle = 60
        observations = {METRICS[0]: Obs(depth, .95), METRICS[1]: Obs(angle, .95), METRICS[2]: Obs(1, .95)}
        if name == "depth_blind" and 200 <= ms <= 800:
            observations.pop(METRICS[0])
        yield 1_000_000_000 + round(ms * speed * 1e6), phase, observations


def trajectory_row(timestamp, origin, phase, observations, scorer, *, selected=None, tracking_valid=True):
    metrics = {name: {"value": observation.value, "confidence": observation.confidence,
                      "side": selected.get(name) if selected else None}
               for name, observation in observations.items() if name in METRICS}
    live = scorer.live_score(phase, observations if tracking_valid else {})
    trunk = next((c for c in live.components if c.name == METRICS[1]), None)
    return {"time": (timestamp - origin) / 1e9, "phase": phase.value,
            "tracking_valid": tracking_valid, "metrics": metrics,
            "trunk_score": trunk.score_percent if trunk is not None else None,
            "lower": trunk.ideal_min if trunk is not None else None,
            "upper": trunk.ideal_max if trunk is not None else None}


def repetition_record(scores, ps, *, origin, timestamp):
    combined = scores["combined"]
    aggregation = combined.aggregation
    return {"start": (aggregation.start_timestamp_ns - origin) / 1e9,
            "end": (timestamp - origin) / 1e9,
            "scores": {arm: _score_record(s) for arm, s in scores.items()},
            "feedback": [format_feedback_cue_zh(c) for c in ps["combined"]._make_process_cues(combined)]}


def study_synthetic(name: str, baseline: Path, combined: Path):
    ps, _ = processors(baseline, combined)
    rows, origin, end = [], 1_000_000_000, 0
    for timestamp, phase, observations in synthetic_samples(name):
        for processor in ps.values():
            processor.process_scorer.add(timestamp, phase, observations)
        rows.append(trajectory_row(timestamp, origin, phase, observations, ps["combined"].process_scorer))
        end = timestamp
    scores = {arm: p.process_scorer.finish((end - origin) / 1e9) for arm, p in ps.items()}
    rep = repetition_record(scores, ps, origin=origin, timestamp=end)
    return {"sample": name, "synthetic": True, "tracking_identical": True, "rows": rows,
            "repetitions": [rep], "completed": 1,
            "valid_scores": {arm: int(score.valid) for arm, score in scores.items()}}


def study_trace(path: Path, baseline: Path, combined: Path):
    ps, _ = processors(baseline, combined)
    rows, repetitions, origin = [], [], None
    rescued_depth = rescued_active = 0
    selection_switches = {name: 0 for name in METRICS[:2]}
    previous_selection = {}
    for frame in read_pose_trace(path):
        updates = {arm: p.push(frame) for arm, p in ps.items()}
        if all(u is None for u in updates.values()):
            continue
        if any(u is None for u in updates.values()):
            raise AssertionError("four-arm synchronizers differ")
        signature = lambda u: (u.timestamp_ns, u.front_frame_id, u.side_frame_id, u.phase,
                               u.repetition_count, u.repetition_completed, u.repetition_duration_seconds)
        u = updates["combined"]
        if any(signature(other) != signature(u) for other in updates.values()):
            raise AssertionError("four-arm tracking differs")
        origin = u.timestamp_ns if origin is None else origin
        observed = extract_squat_observations(u.pose_pair, side_metric_policy=PER_METRIC_SIDE_POLICY)
        old = extract_squat_observations(u.pose_pair)
        selected = {name: item.side for name, item in select_side_metrics(u.pose_pair.side).items()}
        threshold = ps["combined"].scorer.minimum_observation_confidence
        trusted = lambda mapping, name: name in mapping and mapping[name].confidence >= threshold
        rescue = trusted(observed, METRICS[0]) and not trusted(old, METRICS[0])
        rescued_depth += rescue
        rescued_active += rescue and u.phase in (Phase.DESCENDING, Phase.BOTTOM, Phase.ASCENDING)
        for name, side in selected.items():
            if name in previous_selection and side != previous_selection[name]:
                selection_switches[name] += 1
            previous_selection[name] = side
        row = trajectory_row(u.timestamp_ns, origin, u.phase, observed, ps["combined"].process_scorer,
                             selected=selected, tracking_valid=u.phase is not Phase.UNKNOWN)
        row["original_depth"] = ({"value": old[METRICS[0]].value, "confidence": old[METRICS[0]].confidence}
                                 if METRICS[0] in old else None)
        rows.append(row)
        if u.repetition_completed:
            scores = {arm: update.repetition_score for arm, update in updates.items()}
            rep = repetition_record(scores, ps, origin=origin, timestamp=u.timestamp_ns)
            rep["number"] = u.repetition_count
            repetitions.append(rep)
    if origin is None:
        raise ValueError("trace has no synchronized pose pairs")
    return {"sample": path.stem, "synthetic": False, "tracking_identical": True,
            "rows": rows, "repetitions": repetitions, "completed": len(repetitions),
            "valid_scores": {arm: sum(rep["scores"][arm]["valid"] for rep in repetitions) for arm in ARMS},
            "rescued_depth_observations": rescued_depth, "rescued_depth_active": rescued_active,
            "selection_switches": selection_switches}


def build_study(baseline: Path, combined: Path, traces):
    _, configurations = processors(baseline, combined)
    cases = [study_trace(p, baseline, combined) for p in traces]
    return {"schema_version": 1, "study": "depth_angle_four_arm_v1",
            "contains_pixels": False, "contains_keypoints": False, "contains_derived_motion_metrics": True,
            "performance_measurement": False, "expert_validated": False,
            "configurations": configurations,
            "arms": [{"id": a, "label": label} for a, label in zip(ARMS, ARM_LABELS)],
            "real_summary": {"cases": len(cases), "completed": sum(c["completed"] for c in cases),
                "valid_scores": {arm: sum(c["valid_scores"][arm] for c in cases) for arm in ARMS},
                "synchronized_updates": sum(len(c["rows"]) for c in cases)},
            "cases": cases + [study_synthetic(name, baseline, combined) for name in SYNTHETIC_CASES]}
