"""Timestamp pairing for independently inferred front and side poses."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from functools import lru_cache
import math

from .contracts import CameraView, PoseFrame, SyncedPosePair


@dataclass(frozen=True, slots=True)
class DualViewSyncStats:
    paired_frames: int
    dropped_front: int
    dropped_side: int
    queue_overflow_front: int
    queue_overflow_side: int
    skew_rejected_front: int
    skew_rejected_side: int
    skew_sample_count: int
    mean_skew_ms: float | None
    p95_skew_ms: float | None
    max_skew_ms: float | None
    superseded_front: int = 0
    superseded_side: int = 0


class DualViewSynchronizer:
    """Pair the closest queued frames and never reuse or reorder a matched frame."""

    def __init__(
        self,
        max_skew_ms: float = 50.0,
        queue_depth: int = 4,
        stats_window: int = 2048,
    ) -> None:
        if max_skew_ms < 0:
            raise ValueError("max_skew_ms must be non-negative")
        if queue_depth < 1:
            raise ValueError("queue_depth must be at least one")
        if stats_window < 1:
            raise ValueError("stats_window must be at least one")
        self.max_skew_ns = int(max_skew_ms * 1_000_000)
        self._front: deque[PoseFrame] = deque(maxlen=queue_depth)
        self._side: deque[PoseFrame] = deque(maxlen=queue_depth)
        self._skew_samples: deque[int] = deque(maxlen=stats_window)
        self._paired_frames = 0
        self.dropped_front = 0
        self.dropped_side = 0
        self._queue_overflow_front = 0
        self._queue_overflow_side = 0
        self._skew_rejected_front = 0
        self._skew_rejected_side = 0
        self._superseded_front = 0
        self._superseded_side = 0

    def push(self, frame: PoseFrame) -> SyncedPosePair | None:
        queue = self._front if frame.view is CameraView.FRONT else self._side
        if queue and frame.timestamp_ns < queue[-1].timestamp_ns:
            raise ValueError(f"non-monotonic timestamp for {frame.view.value} camera")
        if len(queue) == queue.maxlen:
            if frame.view is CameraView.FRONT:
                self.dropped_front += 1
                self._queue_overflow_front += 1
            else:
                self.dropped_side += 1
                self._queue_overflow_side += 1
        queue.append(frame)
        return self._match()

    def stats(self) -> DualViewSyncStats:
        samples = tuple(self._skew_samples)
        if samples:
            ordered = sorted(samples)
            p95_index = math.ceil(0.95 * len(ordered)) - 1
            mean_skew_ms = sum(samples) / len(samples) / 1_000_000
            p95_skew_ms = ordered[p95_index] / 1_000_000
            max_skew_ms = ordered[-1] / 1_000_000
        else:
            mean_skew_ms = None
            p95_skew_ms = None
            max_skew_ms = None
        return DualViewSyncStats(
            paired_frames=self._paired_frames,
            dropped_front=self.dropped_front,
            dropped_side=self.dropped_side,
            queue_overflow_front=self._queue_overflow_front,
            queue_overflow_side=self._queue_overflow_side,
            skew_rejected_front=self._skew_rejected_front,
            skew_rejected_side=self._skew_rejected_side,
            skew_sample_count=len(samples),
            mean_skew_ms=mean_skew_ms,
            p95_skew_ms=p95_skew_ms,
            max_skew_ms=max_skew_ms,
            superseded_front=self._superseded_front,
            superseded_side=self._superseded_side,
        )

    def _match(self) -> SyncedPosePair | None:
        while self._front and self._side:
            matching = _optimal_matching(
                tuple(self._front),
                tuple(self._side),
                self.max_skew_ns,
            )
            if matching:
                front_index, side_index, skew_ns = matching[0]
                self._discard_superseded(front_index, side_index)
                front = self._front.popleft()
                side = self._side.popleft()
                self._paired_frames += 1
                self._skew_samples.append(skew_ns)
                return SyncedPosePair(front=front, side=side, skew_ns=skew_ns)
            if self._front[0].timestamp_ns < self._side[0].timestamp_ns:
                self._front.popleft()
                self.dropped_front += 1
                self._skew_rejected_front += 1
            else:
                self._side.popleft()
                self.dropped_side += 1
                self._skew_rejected_side += 1
        return None

    def _discard_superseded(self, front_count: int, side_count: int) -> None:
        for _ in range(front_count):
            self._front.popleft()
            self.dropped_front += 1
            self._superseded_front += 1
        for _ in range(side_count):
            self._side.popleft()
            self.dropped_side += 1
            self._superseded_side += 1


def _optimal_matching(
    front: tuple[PoseFrame, ...],
    side: tuple[PoseFrame, ...],
    max_skew_ns: int,
) -> tuple[tuple[int, int, int], ...]:
    """Maximize ordered pair count, then minimize total timestamp skew."""

    @lru_cache(maxsize=None)
    def solve(
        front_index: int,
        side_index: int,
    ) -> tuple[tuple[int, int, int], ...]:
        if front_index == len(front) or side_index == len(side):
            return ()
        candidates = [
            solve(front_index + 1, side_index),
            solve(front_index, side_index + 1),
        ]
        skew_ns = abs(
            front[front_index].timestamp_ns - side[side_index].timestamp_ns
        )
        if skew_ns <= max_skew_ns:
            candidates.append(
                ((front_index, side_index, skew_ns),)
                + solve(front_index + 1, side_index + 1)
            )
        return min(
            candidates,
            key=lambda matching: (
                -len(matching),
                sum(item[2] for item in matching),
                tuple((item[0], item[1]) for item in matching),
            ),
        )

    return solve(0, 0)
