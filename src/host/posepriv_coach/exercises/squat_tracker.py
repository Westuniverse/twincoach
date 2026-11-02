"""Small deterministic squat phase tracker for repetition timing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math


class SquatPhase(str, Enum):
    UNKNOWN = "unknown"
    STANDING = "standing"
    DESCENDING = "descending"
    BOTTOM = "bottom"
    ASCENDING = "ascending"


@dataclass(frozen=True, slots=True)
class SquatPhaseConfig:
    standing_min: float = 160.0
    descending_enter: float = 150.0
    bottom_max: float = 110.0
    ascending_enter: float = 120.0
    maximum_sample_gap_ms: float = 1000.0
    missing_tracking_policy: str = "reset"

    def __post_init__(self) -> None:
        if not self.bottom_max < self.ascending_enter < self.descending_enter < self.standing_min:
            raise ValueError("squat phase thresholds must be strictly ordered")
        if not math.isfinite(self.maximum_sample_gap_ms) or self.maximum_sample_gap_ms <= 0.0:
            raise ValueError("maximum_sample_gap_ms must be finite and positive")
        if self.missing_tracking_policy not in ("reset", "hold"):
            raise ValueError("missing_tracking_policy must be reset or hold")


@dataclass(frozen=True, slots=True)
class SquatUpdate:
    phase: SquatPhase
    repetition_count: int
    repetition_completed: bool
    repetition_duration_seconds: float | None


class SquatTracker:
    def __init__(self, config: SquatPhaseConfig | None = None) -> None:
        self.config = config or SquatPhaseConfig()
        self.phase = SquatPhase.UNKNOWN
        self.repetition_count = 0
        self._start_timestamp_ns: int | None = None
        self._last_timestamp_ns: int | None = None
        self._last_valid_timestamp_ns: int | None = None

    def update(
        self,
        knee_angle_deg: float | None,
        timestamp_ns: int,
        *,
        invalidate: bool = False,
    ) -> SquatUpdate:
        if timestamp_ns < 0:
            raise ValueError("timestamp_ns must be non-negative")
        if self._last_timestamp_ns is not None and timestamp_ns <= self._last_timestamp_ns:
            raise ValueError("squat tracker timestamps must be strictly increasing")
        if (
            self._last_valid_timestamp_ns is not None
            and timestamp_ns - self._last_valid_timestamp_ns
            > round(self.config.maximum_sample_gap_ms * 1_000_000)
        ):
            self.phase = SquatPhase.UNKNOWN
            self._start_timestamp_ns = None
        self._last_timestamp_ns = timestamp_ns

        completed = False
        duration: float | None = None
        if knee_angle_deg is None or invalidate:
            if invalidate or self.config.missing_tracking_policy == "reset":
                self.phase = SquatPhase.UNKNOWN
                self._start_timestamp_ns = None
            # In hold mode phase retains only the last accepted observation;
            # this frame still reports unknown and never advances the tracker.
            return SquatUpdate(SquatPhase.UNKNOWN, self.repetition_count, completed, duration)
        self._last_valid_timestamp_ns = timestamp_ns

        if self.phase is SquatPhase.UNKNOWN:
            if knee_angle_deg >= self.config.standing_min:
                self.phase = SquatPhase.STANDING
        elif self.phase is SquatPhase.STANDING:
            if knee_angle_deg <= self.config.descending_enter:
                self.phase = SquatPhase.DESCENDING
                self._start_timestamp_ns = timestamp_ns
        elif self.phase is SquatPhase.DESCENDING:
            if knee_angle_deg <= self.config.bottom_max:
                self.phase = SquatPhase.BOTTOM
            elif knee_angle_deg >= self.config.standing_min:
                self.phase = SquatPhase.STANDING
                self._start_timestamp_ns = None
        elif self.phase is SquatPhase.BOTTOM:
            if knee_angle_deg >= self.config.ascending_enter:
                self.phase = SquatPhase.ASCENDING
        elif self.phase is SquatPhase.ASCENDING:
            if knee_angle_deg >= self.config.standing_min:
                self.phase = SquatPhase.STANDING
                self.repetition_count += 1
                completed = True
                if self._start_timestamp_ns is not None:
                    duration = (timestamp_ns - self._start_timestamp_ns) / 1_000_000_000
                self._start_timestamp_ns = None
            elif knee_angle_deg <= self.config.bottom_max:
                self.phase = SquatPhase.BOTTOM

        return SquatUpdate(self.phase, self.repetition_count, completed, duration)
