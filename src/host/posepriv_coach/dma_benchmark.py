"""Parse the standalone DMA self-test log into comparable measurements."""

from __future__ import annotations

from dataclasses import dataclass
import re


_HEADER = "PosePriv joint-metrics DMA self-test"
_FREQUENCY = re.compile(r"Timer frequency:\s*(\d+)\s*Hz")
_RESULT = re.compile(r"PASS:\s*(\d+)-byte transaction, DMA ticks=(\d+)")
_STEADY = re.compile(
    r"STEADY:\s*(\d+)-byte transaction, iterations=(\d+),\s*"
    r"min_ticks=(\d+), total_ticks=(\d+), max_ticks=(\d+)"
)


@dataclass(frozen=True, slots=True)
class DmaMeasurement:
    byte_count: int
    ticks: int
    latency_us: float
    effective_payload_mbps: float


@dataclass(frozen=True, slots=True)
class DmaSteadyMeasurement:
    byte_count: int
    iterations: int
    minimum_ticks: int
    mean_ticks: float
    maximum_ticks: int
    mean_latency_us: float
    effective_payload_mbps: float


@dataclass(frozen=True, slots=True)
class DmaBenchmark:
    timer_frequency_hz: int
    measurements: tuple[DmaMeasurement, ...]
    steady: DmaSteadyMeasurement | None = None


def parse_dma_selftest_log(
    text: str,
    *,
    expected_byte_counts: tuple[int, ...] = (16, 32, 64),
) -> DmaBenchmark:
    """Parse the final boot's successful self-test block."""

    if _HEADER not in text:
        raise ValueError("DMA self-test header not found")
    block = text.rsplit(_HEADER, 1)[1]
    pass_index = block.rfind("POSEPRIV_DMA_TEST: PASS")
    fail_index = block.rfind("POSEPRIV_DMA_TEST: FAIL")
    if pass_index < 0 or fail_index > pass_index:
        raise ValueError("final DMA self-test did not pass")

    frequency_match = _FREQUENCY.search(block)
    if frequency_match is None:
        raise ValueError("timer frequency not found")
    frequency_hz = int(frequency_match.group(1))
    if frequency_hz <= 0:
        raise ValueError("timer frequency must be positive")

    raw_results = {
        int(byte_count): int(ticks)
        for byte_count, ticks in _RESULT.findall(block[:pass_index])
    }
    if tuple(sorted(raw_results)) != tuple(sorted(expected_byte_counts)):
        raise ValueError("DMA self-test transaction sizes are incomplete")

    measurements = []
    for byte_count in expected_byte_counts:
        ticks = raw_results[byte_count]
        if ticks <= 0:
            raise ValueError("DMA tick count must be positive")
        measurements.append(
            DmaMeasurement(
                byte_count=byte_count,
                ticks=ticks,
                latency_us=ticks * 1_000_000 / frequency_hz,
                effective_payload_mbps=(
                    byte_count * 8 * frequency_hz / ticks / 1_000_000
                ),
            )
        )
    steady_matches = _STEADY.findall(block[:pass_index])
    steady = None
    if steady_matches:
        byte_count, iterations, minimum_ticks, total_ticks, maximum_ticks = (
            int(value) for value in steady_matches[-1]
        )
        if (
            byte_count <= 0
            or iterations <= 0
            or minimum_ticks <= 0
            or total_ticks <= 0
            or maximum_ticks < minimum_ticks
        ):
            raise ValueError("DMA steady measurement values must be positive and ordered")
        mean_ticks = total_ticks / iterations
        if not minimum_ticks <= mean_ticks <= maximum_ticks:
            raise ValueError("DMA steady mean must be within minimum and maximum")
        steady = DmaSteadyMeasurement(
            byte_count=byte_count,
            iterations=iterations,
            minimum_ticks=minimum_ticks,
            mean_ticks=mean_ticks,
            maximum_ticks=maximum_ticks,
            mean_latency_us=mean_ticks * 1_000_000 / frequency_hz,
            effective_payload_mbps=(
                byte_count * 8 * frequency_hz / mean_ticks / 1_000_000
            ),
        )
    return DmaBenchmark(frequency_hz, tuple(measurements), steady)
