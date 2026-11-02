"""Explicit copy-based shared-DMA lighting consumer; no device ownership.

The caller lends an exchange_bytes(op, payload) function. Production defaults
and pose timestamps are unaffected. Protocol/transport failures never become
numerical CPU fallback, and no golden arithmetic is executed by this module.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from threading import Lock
from typing import Callable

from .avatar_triangle_lighting_packet import (
    decode_output_batch, pack_input_batch, prepare_lighting_face, select_face_color,
)

LIGHTING_OPERATOR = 3
MAX_FACES_PER_TRANSFER = 4096


@dataclass(frozen=True, slots=True)
class LightingBatchStatistics:
    face_count: int
    transferred_face_count: int
    selected_pl_face_count: int
    fallback_reasons: tuple[tuple[str, int], ...]
    transactions: int
    tx_bytes: int
    rx_bytes: int


@dataclass(frozen=True, slots=True)
class LightingBatchOutput:
    colors_bgr8: tuple[tuple[int, int, int], ...]
    light_intensities: tuple[float, ...]
    statistics: LightingBatchStatistics


class SharedTriangleLightingBackend:
    def __init__(self, exchange_bytes: Callable[[int, bytes], bytes]):
        if not callable(exchange_bytes):
            raise TypeError("borrowed exchange_bytes callable required")
        self._exchange_bytes = exchange_bytes
        self._lock = Lock()
        self._faulted = False
        self._completed_frames = 0
        self._totals = Counter({name: 0 for name in (
            "face_count", "transferred_face_count", "selected_pl_face_count",
            "transactions", "tx_bytes", "rx_bytes",
        )})
        self._fallback_totals = Counter()

    def stats(self) -> dict:
        """Complete lighting frames only; not rendered/HDMI/hardware evidence."""
        with self._lock:
            return {"completed_frames": self._completed_frames, "faulted": self._faulted,
                    **self._totals, "fallback_reasons": dict(sorted(self._fallback_totals.items()))}

    def shade(self, faces: tuple) -> LightingBatchOutput:
        """Shade an immutable ordered frame of (camera_points, RGB8) pairs.

        All faces are validated before any transaction. IDs are local to each
        4096-face group, including unsent out-of-range positions. Output is
        published only after the entire frame succeeds. No cross-frame cache.
        """
        with self._lock:
            if self._faulted:
                raise RuntimeError("lighting backend faulted; explicit device/PL recovery required")
            if type(faces) is not tuple:
                raise ValueError("immutable tuple of lighting faces required")
            prepared = tuple(prepare_lighting_face(points, rgb, index % MAX_FACES_PER_TRANSFER)
                             for index, (points, rgb) in enumerate(faces))
            colors, intensities = [], []
            reasons = Counter()
            transactions = tx_bytes = rx_bytes = transferred = selected_pl = 0
            started = False
            try:
                for offset in range(0, len(prepared), MAX_FACES_PER_TRANSFER):
                    group = prepared[offset:offset + MAX_FACES_PER_TRANSFER]
                    requests = tuple(face.request for face in group if face.request is not None)
                    if requests:
                        payload = pack_input_batch(requests)
                        started = True
                        raw = self._exchange_bytes(LIGHTING_OPERATOR, payload)
                        results = iter(decode_output_batch(raw, requests))
                        transactions += 1
                        transferred += len(requests)
                        tx_bytes += len(payload)
                        rx_bytes += len(raw)
                    else:
                        results = iter(())
                    for face in group:
                        result = next(results) if face.request is not None else None
                        selection = select_face_color(face, result)
                        colors.append(selection.color_bgr8)
                        intensities.append(selection.light_intensity)
                        if selection.fallback_reason is None:
                            selected_pl += 1
                        else:
                            reasons[selection.fallback_reason] += 1
                statistics = LightingBatchStatistics(
                    len(prepared), transferred, selected_pl, tuple(sorted(reasons.items())),
                    transactions, tx_bytes, rx_bytes)
                output = LightingBatchOutput(tuple(colors), tuple(intensities), statistics)
                totals = self._totals.copy()
                for name in totals:
                    totals[name] += getattr(statistics, name)
                fallback_totals = self._fallback_totals.copy()
                fallback_totals.update(reasons)
                completed_frames = self._completed_frames + 1
            except BaseException:
                # Once hardware may have accepted data, neither a later packet
                # nor a new frame is retried. Do not return partial frame colors.
                if started:
                    self._faulted = True
                raise
            self._completed_frames = completed_frames
            self._totals, self._fallback_totals = totals, fallback_totals
            return output
