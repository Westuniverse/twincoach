"""Versioned host-side data contracts for the baseline pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .depth_angle_scoring import DepthAngleModel


class CameraView(str, Enum):
    FRONT = "front"
    SIDE = "side"


@dataclass(frozen=True, slots=True)
class Keypoint:
    x: float
    y: float
    confidence: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("keypoint confidence must be within [0, 1]")


@dataclass(frozen=True, slots=True)
class PoseFrame:
    view: CameraView
    frame_id: int
    timestamp_ns: int
    keypoints: Mapping[str, Keypoint]
    source_width: int = 1
    source_height: int = 1

    def __post_init__(self) -> None:
        if self.frame_id < 0:
            raise ValueError("frame_id must be non-negative")
        if self.timestamp_ns < 0:
            raise ValueError("timestamp_ns must be non-negative")
        if self.source_width < 1 or self.source_height < 1:
            raise ValueError("pose source dimensions must be positive")
        object.__setattr__(self, "keypoints", MappingProxyType(dict(self.keypoints)))


@dataclass(frozen=True, slots=True)
class SyncedPosePair:
    front: PoseFrame
    side: PoseFrame
    skew_ns: int

    def __post_init__(self) -> None:
        if self.front.view is not CameraView.FRONT:
            raise ValueError("front frame has the wrong camera view")
        if self.side.view is not CameraView.SIDE:
            raise ValueError("side frame has the wrong camera view")
        expected_skew = abs(self.front.timestamp_ns - self.side.timestamp_ns)
        if self.skew_ns != expected_skew:
            raise ValueError("skew_ns does not match frame timestamps")


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    name: str
    view: CameraView
    observed_value: float
    unit: str
    ideal_min: float
    ideal_max: float
    tolerance: float
    score_percent: float
    confidence_percent: float
    weight: float


@dataclass(frozen=True, slots=True)
class PhaseScoreSummary:
    phase: str
    ideal_min: float
    ideal_max: float
    applicable_seconds: float
    valid_seconds: float
    mean_score_percent: float | None
    low_score_seconds: float


@dataclass(frozen=True, slots=True)
class DeviationSummary:
    direction: str
    confirmed_seconds: float
    longest_seconds: float
    phases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MetricAggregation:
    name: str
    method: str
    observed_value_semantics: str
    start_timestamp_ns: int | None
    end_timestamp_ns: int | None
    applicable_seconds: float
    valid_seconds: float
    time_coverage_percent: float
    low_score_seconds: float
    low_score_share_percent: float
    phases: tuple[PhaseScoreSummary, ...]
    deviations: tuple[DeviationSummary, ...]


@dataclass(frozen=True, slots=True)
class ScoreAggregation:
    algorithm: str
    start_timestamp_ns: int
    end_timestamp_ns: int
    time_coverage_percent: float
    invalid_reasons: tuple[str, ...]
    parameters: tuple[tuple[str, float], ...]
    metrics: tuple[MetricAggregation, ...]
    depth_angle_model: DepthAngleModel | None = None
    phase_seconds: tuple[tuple[str, float], ...] = ()
    depth_angle_invalid_seconds: tuple[tuple[str, float], ...] = ()
    side_metric_policy: str = "legacy_chain_sum_v0"


@dataclass(frozen=True, slots=True)
class ExerciseScore:
    exercise: str
    conformance_percent: float | None
    confidence_percent: float
    coverage_percent: float
    valid: bool
    components: tuple[ScoreComponent, ...]
    aggregation: ScoreAggregation | None = None
