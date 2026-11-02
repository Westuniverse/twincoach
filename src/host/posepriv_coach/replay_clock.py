"""Deterministic processing time for replay; capture timestamps stay untouched."""


class ReplayClock:
    """Advance after source capture time without measuring the host's speed."""

    def __init__(self) -> None:
        self._next = 0

    def begin(self, timestamp_ns: int) -> None:
        self._next = max(self._next, timestamp_ns + 1)

    def __call__(self) -> int:
        value = self._next
        self._next += 1
        return value
