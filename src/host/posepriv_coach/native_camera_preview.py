# SPDX-License-Identifier: MIT
"""Opt-in Linux-only, bounded native BGR IPC; no camera or saved-image access."""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque
import errno
import mmap
import os
import struct
import sys
from math import ceil, isfinite
from threading import Lock
from time import monotonic_ns
from typing import Any, Callable

from .contracts import CameraView, SyncedPosePair

GLOBAL_HEADER = struct.Struct("<4sIIIIIII32x")
SLOT_HEADER = struct.Struct("<4sIQQIIII24x")
MAX_RING_BYTES = 256 * 1024 * 1024


class NativePreviewError(RuntimeError):
    """A preview lifecycle or ABI error; not a missing/superseded frame."""


def _integer(value: int, lower: int, upper: int, name: str) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{name} must be an integer within [{lower}, {upper}]")
    return value


@dataclass(frozen=True, slots=True)
class PreviewRingLayout:
    max_width: int
    max_height: int
    slots_per_view: int = 4

    def __post_init__(self) -> None:
        _integer(self.max_width, 1, 4096, "max_width")
        _integer(self.max_height, 1, 2160, "max_height")
        _integer(self.slots_per_view, 1, 8, "slots_per_view")
        if self.total_bytes > MAX_RING_BYTES:
            raise ValueError("preview ring exceeds 256 MiB")

    @property
    def capacity(self) -> int:
        return self.max_width * self.max_height * 3

    @property
    def slot_span(self) -> int:
        return (SLOT_HEADER.size + self.capacity + 63) // 64 * 64

    @property
    def total_bytes(self) -> int:
        return GLOBAL_HEADER.size + 2 * self.slots_per_view * self.slot_span

    def header(self) -> bytes:
        return GLOBAL_HEADER.pack(b"NPV1", 1, self.slots_per_view, self.max_width,
                                  self.max_height, self.capacity, self.slot_span, self.total_bytes)

    def slot_offset(self, view: CameraView, frame_id: int) -> int:
        if not isinstance(view, CameraView):
            raise ValueError("preview view must be CameraView")
        _integer(frame_id, 0, 2**64 - 1, "frame_id")
        view_number = 0 if view is CameraView.FRONT else 1
        return GLOBAL_HEADER.size + (
            view_number * self.slots_per_view + frame_id % self.slots_per_view
        ) * self.slot_span


class NativePreviewRing:
    """Own one memfd; return only exact-match, private, read-only BGR arrays.

    The owner stops the native child before close. No duplicate attachment is
    exposed: traditional POSIX record locks are process-, not thread-owned.
    """

    def __init__(self, layout: PreviewRingLayout) -> None:
        if not sys.platform.startswith("linux") or not hasattr(os, "memfd_create"):
            raise NativePreviewError("native camera preview requires Linux memfd")
        import fcntl
        import numpy as np

        if not isinstance(layout, PreviewRingLayout):
            raise TypeError("PreviewRingLayout required")
        self.layout = layout
        self._fcntl = fcntl
        self._numpy = np
        self._lock = Lock()
        self._closed = False
        self._failed = False
        self._attempts = self._matched = self._missing = self._busy = 0
        self._fd = os.memfd_create("posepriv-native-preview", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        self._mapping = None
        try:
            if self._fd < 3:
                raise NativePreviewError("preview fd must not occupy stdin/stdout/stderr")
            os.ftruncate(self._fd, layout.total_bytes)
            self._mapping = mmap.mmap(self._fd, layout.total_bytes, flags=mmap.MAP_SHARED,
                                      prot=mmap.PROT_READ | mmap.PROT_WRITE)
            self._mapping[:GLOBAL_HEADER.size] = layout.header()
            fcntl.fcntl(self._fd, fcntl.F_ADD_SEALS,
                        fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        except BaseException:
            if self._mapping is not None:
                self._mapping.close()
            os.close(self._fd)
            raise

    def fileno(self) -> int:
        with self._lock:
            self._require_open()
            return self._fd

    def _require_open(self) -> None:
        if self._closed:
            raise NativePreviewError("preview ring is closed")
        if self._failed:
            raise NativePreviewError("preview ring previously failed")

    def read(self, view: CameraView, frame_id: int, timestamp_ns: int,
             width: int, height: int) -> Any | None:
        offset = self.layout.slot_offset(view, frame_id)
        _integer(timestamp_ns, 0, 2**64 - 1, "timestamp_ns")
        _integer(width, 1, self.layout.max_width, "width")
        _integer(height, 1, self.layout.max_height, "height")
        with self._lock:
            self._require_open()
            self._attempts += 1
            try:
                if self._mapping[:GLOBAL_HEADER.size] != self.layout.header():
                    raise NativePreviewError("preview global header changed")
                try:
                    self._fcntl.lockf(self._fd, self._fcntl.LOCK_SH | self._fcntl.LOCK_NB,
                                     self.layout.slot_span, offset, os.SEEK_SET)
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    self._busy += 1
                    return None
                try:
                    header = self._mapping[offset:offset + SLOT_HEADER.size]
                    if header[:4] == b"\0" * 4:
                        self._missing += 1
                        return None
                    magic, number, actual_id, actual_ns, w, h, stride, count = SLOT_HEADER.unpack(header)
                    if (magic != b"BGR1" or number not in (0, 1) or any(header[40:])
                            or not 1 <= w <= self.layout.max_width
                            or not 1 <= h <= self.layout.max_height
                            or stride != w * 3 or count != stride * h
                            or count > self.layout.capacity):
                        raise NativePreviewError("invalid published preview slot")
                    expected_number = 0 if view is CameraView.FRONT else 1
                    if (number, actual_id, actual_ns, w, h) != (
                            expected_number, frame_id, timestamp_ns, width, height):
                        self._missing += 1
                        return None
                    start = offset + SLOT_HEADER.size
                    # mmap slicing makes one private bytes copy, not a borrowed view.
                    payload = self._mapping[start:start + count]
                finally:
                    self._fcntl.lockf(self._fd, self._fcntl.LOCK_UN,
                                     self.layout.slot_span, offset, os.SEEK_SET)
                result = self._numpy.frombuffer(payload, dtype=self._numpy.uint8).reshape(h, w, 3)
                self._matched += 1
                return result
            except BaseException:
                self._failed = True
                raise

    def stats(self) -> dict[str, int | bool | str]:
        with self._lock:
            return {"protocol": "native_preview_ring.v1", "ring_bytes": self.layout.total_bytes,
                    "slots_per_view": self.layout.slots_per_view, "attempts": self._attempts,
                    "matched": self._matched, "missing": self._missing, "busy": self._busy,
                    "failed": self._failed, "closed": self._closed,
                    "pixels_persisted": False,
                    "persistence_scope": "application_no_saved_images_not_OS_swap_policy",
                    "copy_scope": "selected_BGR8_private_snapshot"}

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                try:
                    self._mapping.close()
                finally:
                    os.close(self._fd)
                    self._closed = True

    def __enter__(self) -> NativePreviewRing:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


class NativePreviewPairSource:
    """Match both images to the actual sync result, not the latest JSON pair.

    Borrows the ring on the application's processing thread. Copy statistics
    cover the two lookups/private snapshots only, never physical scanout.
    """

    def __init__(self, ring: NativePreviewRing, *, front_offset_ms: float = 0,
                 side_offset_ms: float = 0, sample_window_size: int = 1024,
                 clock_ns: Callable[[], int] = monotonic_ns) -> None:
        _integer(sample_window_size, 1, 65536, "sample_window_size")
        if not isfinite(front_offset_ms) or not isfinite(side_offset_ms):
            raise ValueError("preview synchronization offsets must be finite")
        self._ring = ring
        self._front_offset_ns = round(front_offset_ms * 1_000_000)
        self._side_offset_ns = round(side_offset_ms * 1_000_000)
        self._clock_ns = clock_ns
        self._samples: deque[int] = deque(maxlen=sample_window_size)
        self._attempts = self._matched = self._skipped = self._faults = 0
        self._total_ns = self._max_ns = 0

    def read_pair(self, pair: SyncedPosePair) -> tuple[Any, Any] | None:
        if self._faults:
            raise NativePreviewError("preview pair source previously failed")
        start = self._clock_ns()
        self._attempts += 1
        try:
            front = self._ring.read(CameraView.FRONT, pair.front.frame_id,
                pair.front.timestamp_ns - self._front_offset_ns,
                pair.front.source_width, pair.front.source_height)
            side = self._ring.read(CameraView.SIDE, pair.side.frame_id,
                pair.side.timestamp_ns - self._side_offset_ns,
                pair.side.source_width, pair.side.source_height)
            elapsed = self._clock_ns() - start
            if elapsed < 0:
                raise NativePreviewError("preview copy clock must be monotonic")
            self._samples.append(elapsed)
            self._total_ns += elapsed
            self._max_ns = max(self._max_ns, elapsed)
            if front is None or side is None:
                self._skipped += 1
                return None
            self._matched += 1
            return front, side
        except BaseException:
            self._faults += 1
            raise

    def stats(self) -> dict[str, Any]:
        ordered = sorted(self._samples)
        completed = self._matched + self._skipped
        return {"ring": self._ring.stats(), "attempted_pairs": self._attempts,
            "matched_pairs": self._matched, "skipped_display_pairs": self._skipped,
            "faulted_pairs": self._faults,
            "read_mean_ms": self._total_ns / completed / 1_000_000 if completed else None,
            "read_max_ms": self._max_ns / 1_000_000 if completed else None,
            "read_p95_ms": ordered[ceil(0.95 * len(ordered)) - 1] / 1_000_000 if ordered else None,
            "latency_sample_count": len(ordered), "latency_window_size": self._samples.maxlen,
            "scope": "two_exact_identity_lookups_and_private_BGR_copies_not_render_or_scanout"}
