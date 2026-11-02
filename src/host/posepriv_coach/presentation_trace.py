"""Bounded optional confirmation samples with explicit truncation accounting.

Project GPL-2.0-only; no sample clock, source identity or confirmation is changed.
Only append is exposed so dropped oldest samples cannot go uncounted. Retained
dicts remain mutable for the renderer's existing post-append clock annotations.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Mapping, Sequence

DEFAULT_CAPACITY = 4096
MIN_CAPACITY = 64
MAX_CAPACITY = 65536


class PresentationTrace(Sequence[dict]):
    """Single-owner append-only trace; overflow retains the newest ``maxlen``.

    Explicit capacities 64..65536 limit diagnostic memory. For a 600-second,
    30-pair/s request, 32768 leaves room beyond the 18000 requested samples.
    Callers must check ``truncated`` as well as retained count when certifying
    complete history; a larger capacity never grants a performance acceptance.
    """

    __slots__ = ("_appended", "_samples")

    def __init__(self, capacity: int = DEFAULT_CAPACITY):
        if type(capacity) is not int or not MIN_CAPACITY <= capacity <= MAX_CAPACITY:
            raise ValueError(
                f"benchmark_presentation_capacity must be a builtin int in {MIN_CAPACITY}..{MAX_CAPACITY}"
            )
        self._samples: deque[dict] = deque(maxlen=capacity)
        self._appended = 0

    @property
    def maxlen(self) -> int:
        return self._samples.maxlen

    def append(self, sample: dict) -> None:
        self._samples.append(sample)
        self._appended += 1

    def __len__(self) -> int:
        return len(self._samples)

    def __iter__(self) -> Iterator[dict]:
        return iter(self._samples)

    def __getitem__(self, index: int) -> dict:
        return self._samples[index]

    def metadata(self) -> dict:
        retained = len(self._samples)
        dropped = self._appended - retained
        return {
            "schema": "posepriv.presentation_trace.v1",
            "capacity": self.maxlen,
            "appended": self._appended,
            "retained": retained,
            "dropped": dropped,
            "truncated": dropped > 0,
        }


def make_presentation_trace(cfg: Mapping) -> PresentationTrace | None:
    """Allocate only for explicit ``benchmark_presentations is True``.

    A capacity in disabled configuration is inactive and neither validated nor
    allocated. Existing omitted/false configurations therefore remain quiet.
    """
    if cfg.get("benchmark_presentations", False) is not True:
        return None
    return PresentationTrace(cfg.get("benchmark_presentation_capacity", DEFAULT_CAPACITY))
