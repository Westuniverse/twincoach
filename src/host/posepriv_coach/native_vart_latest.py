"""Bound a live native result stream without moving GTK off the main thread."""

from __future__ import annotations

from threading import Condition, Thread
from typing import Iterable, Iterator

from .native_vart_stream import (
    NativeVartPairRecord,
    NativeVartStats,
    NativeVartStreamError,
    parse_native_vart_pair,
    parse_native_vart_stats,
)


def iter_native_vart_records(
    lines: Iterable[str],
) -> Iterator[NativeVartPairRecord | NativeVartStats]:
    """Validate every record, including pairs subsequently superseded."""
    for line in lines:
        if not line.strip():
            continue
        try:
            pair = parse_native_vart_pair(line)
            if pair is None:
                yield parse_native_vart_stats(line)
                return
            yield pair
        except NativeVartStreamError as exc:
            raise RuntimeError(f"native VART JSONL error: {exc}") from exc


class LatestNativeVartReader(Iterator[NativeVartPairRecord | NativeVartStats]):
    """Continuously drain stdout; retain one complete pair plus final stats.

    Only an unread pair is replaced. View identities, source frame IDs, points
    and capture timestamps are never rewritten, and malformed discarded records
    still fail. The owner must terminate a live upstream process before close:
    a thread flag cannot interrupt a blocking pipe read.
    """

    def __init__(self, lines: Iterable[str]) -> None:
        self._condition = Condition()
        self._latest: NativeVartPairRecord | None = None
        self._final_stats: NativeVartStats | None = None
        self._stats_delivered = False
        self._finished = False
        self._stopping = False
        self._failure: BaseException | None = None
        self._received = 0
        self._delivered = 0
        self._superseded = 0
        self._lines = lines
        self._thread = Thread(target=self._read, name="posepriv-native-reader", daemon=True)
        self._thread.start()

    def __iter__(self) -> LatestNativeVartReader:
        return self

    def __next__(self) -> NativeVartPairRecord | NativeVartStats:
        with self._condition:
            while self._latest is None and not self._finished and not self._stopping:
                self._condition.wait()
            if self._failure is not None:
                raise RuntimeError("native VART reader failed") from self._failure
            if self._stopping:
                raise StopIteration
            if self._latest is not None:
                pair = self._latest
                self._latest = None
                self._delivered += 1
                return pair
            if self._final_stats is not None and not self._stats_delivered:
                self._stats_delivered = True
                return self._final_stats
            raise StopIteration

    def _read(self) -> None:
        try:
            for record in iter_native_vart_records(self._lines):
                with self._condition:
                    if self._stopping:
                        return
                    if isinstance(record, NativeVartStats):
                        self._final_stats = record
                        return
                    self._received += 1
                    if self._latest is not None:
                        self._superseded += 1
                    self._latest = record
                    self._condition.notify_all()
        except BaseException as exc:
            with self._condition:
                self._failure = exc
        finally:
            with self._condition:
                self._finished = True
                self._condition.notify_all()

    def close(self, *, join_timeout_s: float = 5.0) -> None:
        if join_timeout_s <= 0:
            raise ValueError("reader join timeout must be positive")
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self._thread.join(timeout=join_timeout_s)
        if self._thread.is_alive():
            raise RuntimeError("native reader did not exit after upstream stopped")

    def stats(self) -> dict[str, int | bool | str]:
        """Accounting only; never expose camera pixels or keypoint coordinates."""
        with self._condition:
            return {
                "policy": "latest_complete_pair",
                "pairs_received": self._received,
                "pairs_delivered": self._delivered,
                "superseded_pairs": self._superseded,
                "pending_pairs": int(self._latest is not None),
                "native_stats_received": self._final_stats is not None,
                "failed": self._failure is not None,
                "capture_timestamps": "unchanged",
            }
