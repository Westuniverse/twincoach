"""Deterministic timing summary for one pose-estimator boundary."""

from __future__ import annotations

from dataclasses import dataclass
import math
from time import perf_counter_ns
from typing import Any, Callable

from .capture import CapturedFrame
from .pipeline import PoseEstimator


@dataclass(frozen=True, slots=True)
class PoseBenchmarkResult:
    iterations: int
    warmup_iterations: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float
    throughput_fps: float


def benchmark_pose_estimator(
    estimator: PoseEstimator,
    frame: CapturedFrame[Any],
    *,
    iterations: int = 200,
    warmup_iterations: int = 20,
    clock_ns: Callable[[], int] = perf_counter_ns,
) -> PoseBenchmarkResult:
    """Measure sequential preprocess, inference and decode latency."""

    if iterations < 1:
        raise ValueError("iterations must be positive")
    if warmup_iterations < 0:
        raise ValueError("warmup_iterations must not be negative")
    expected_identity = (frame.view, frame.frame_id, frame.timestamp_ns)

    for _ in range(warmup_iterations):
        pose = estimator.infer(frame)
        if (pose.view, pose.frame_id, pose.timestamp_ns) != expected_identity:
            raise ValueError("pose estimator did not preserve benchmark frame identity")

    samples: list[int] = []
    for _ in range(iterations):
        start = clock_ns()
        pose = estimator.infer(frame)
        end = clock_ns()
        if (pose.view, pose.frame_id, pose.timestamp_ns) != expected_identity:
            raise ValueError("pose estimator did not preserve benchmark frame identity")
        elapsed = end - start
        if elapsed <= 0:
            raise ValueError("benchmark clock must advance for every inference")
        samples.append(elapsed)

    ordered = sorted(samples)
    mean_ns = sum(samples) / len(samples)
    return PoseBenchmarkResult(
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        mean_ms=mean_ns / 1_000_000,
        p50_ms=_nearest_rank(ordered, 0.50) / 1_000_000,
        p95_ms=_nearest_rank(ordered, 0.95) / 1_000_000,
        max_ms=ordered[-1] / 1_000_000,
        throughput_fps=1_000_000_000 / mean_ns,
    )


def _nearest_rank(ordered: list[int], quantile: float) -> int:
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]
