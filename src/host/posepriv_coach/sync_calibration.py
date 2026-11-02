"""Estimate a fixed dual-camera timestamp offset from a shared light event."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from statistics import median
from typing import Iterable, Sequence


@dataclass(frozen=True, slots=True)
class SignalSample:
    timestamp_ns: int
    value: float

    def __post_init__(self) -> None:
        if self.timestamp_ns < 0:
            raise ValueError("signal timestamp must be non-negative")
        if not math.isfinite(self.value):
            raise ValueError("signal value must be finite")


@dataclass(frozen=True, slots=True)
class SyncOffsetEstimate:
    side_offset_ms: float
    correlation: float
    overlap_samples: int
    search_resolution_ms: float
    front_dynamic_range: float
    side_dynamic_range: float
    quality_passed: bool

    def to_record(self) -> dict[str, object]:
        return asdict(self)


def estimate_side_sync_offset(
    front_samples: Iterable[SignalSample],
    side_samples: Iterable[SignalSample],
    *,
    maximum_offset_ms: float = 150.0,
    search_resolution_ms: float = 1.0,
    minimum_dynamic_range: float = 12.0,
    minimum_correlation: float = 0.75,
) -> SyncOffsetEstimate:
    """Return the offset to add to side timestamps for best signal alignment."""

    if maximum_offset_ms < 0.0:
        raise ValueError("maximum_offset_ms must be non-negative")
    if search_resolution_ms <= 0.0:
        raise ValueError("search_resolution_ms must be positive")
    if minimum_dynamic_range < 0.0:
        raise ValueError("minimum_dynamic_range must be non-negative")
    if not -1.0 <= minimum_correlation <= 1.0:
        raise ValueError("minimum_correlation must be within [-1, 1]")

    front = _validated_samples(front_samples, "front")
    side = _validated_samples(side_samples, "side")
    front_range = _dynamic_range(front)
    side_range = _dynamic_range(side)
    sample_period_ns = max(_median_period_ns(front), _median_period_ns(side))
    resolution_ns = max(1, round(search_resolution_ms * 1_000_000))
    maximum_offset_ns = round(maximum_offset_ms * 1_000_000)

    best: tuple[float, int, int] | None = None
    for offset_ns in range(-maximum_offset_ns, maximum_offset_ns + 1, resolution_ns):
        correlation, count = _correlation_at_offset(
            front,
            side,
            offset_ns,
            sample_period_ns,
        )
        if correlation is None:
            continue
        candidate = (correlation, -abs(offset_ns), offset_ns)
        if best is None or candidate > (best[0], -abs(best[2]), best[2]):
            best = (correlation, count, offset_ns)

    if best is None:
        raise ValueError("camera signal overlap is too short for offset estimation")
    correlation, count, offset_ns = best
    quality_passed = (
        front_range >= minimum_dynamic_range
        and side_range >= minimum_dynamic_range
        and correlation >= minimum_correlation
        and count >= 8
    )
    return SyncOffsetEstimate(
        side_offset_ms=offset_ns / 1_000_000,
        correlation=correlation,
        overlap_samples=count,
        search_resolution_ms=resolution_ns / 1_000_000,
        front_dynamic_range=front_range,
        side_dynamic_range=side_range,
        quality_passed=quality_passed,
    )


def _validated_samples(
    samples: Iterable[SignalSample],
    label: str,
) -> tuple[SignalSample, ...]:
    values = tuple(samples)
    if len(values) < 8:
        raise ValueError(f"{label} signal requires at least eight samples")
    if any(
        current.timestamp_ns <= previous.timestamp_ns
        for previous, current in zip(values, values[1:])
    ):
        raise ValueError(f"{label} signal timestamps must be strictly increasing")
    return values


def _dynamic_range(samples: Sequence[SignalSample]) -> float:
    values = [sample.value for sample in samples]
    return max(values) - min(values)


def _median_period_ns(samples: Sequence[SignalSample]) -> int:
    periods = [
        current.timestamp_ns - previous.timestamp_ns
        for previous, current in zip(samples, samples[1:])
    ]
    return max(1, round(median(periods)))


def _correlation_at_offset(
    front: Sequence[SignalSample],
    side: Sequence[SignalSample],
    side_offset_ns: int,
    sample_period_ns: int,
) -> tuple[float | None, int]:
    start_ns = max(front[0].timestamp_ns, side[0].timestamp_ns + side_offset_ns)
    end_ns = min(front[-1].timestamp_ns, side[-1].timestamp_ns + side_offset_ns)
    if end_ns <= start_ns:
        return None, 0

    front_values: list[float] = []
    side_values: list[float] = []
    timestamp_ns = start_ns
    while timestamp_ns <= end_ns:
        front_values.append(_interpolate(front, timestamp_ns))
        side_values.append(_interpolate(side, timestamp_ns - side_offset_ns))
        timestamp_ns += sample_period_ns
    if len(front_values) < 8:
        return None, len(front_values)
    return _pearson(front_values, side_values), len(front_values)


def _interpolate(samples: Sequence[SignalSample], timestamp_ns: int) -> float:
    low = 0
    high = len(samples) - 1
    while low + 1 < high:
        middle = (low + high) // 2
        if samples[middle].timestamp_ns <= timestamp_ns:
            low = middle
        else:
            high = middle
    left = samples[low]
    right = samples[high]
    if timestamp_ns <= left.timestamp_ns:
        return left.value
    if timestamp_ns >= right.timestamp_ns:
        return right.value
    ratio = (timestamp_ns - left.timestamp_ns) / (
        right.timestamp_ns - left.timestamp_ns
    )
    return left.value + ratio * (right.value - left.value)


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right)
    )
    left_energy = sum((value - left_mean) ** 2 for value in left)
    right_energy = sum((value - right_mean) ** 2 for value in right)
    denominator = math.sqrt(left_energy * right_energy)
    if denominator == 0.0:
        return 0.0
    return numerator / denominator
