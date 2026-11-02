"""校正双摄三角测量的参考与字节传输后端。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
from time import monotonic_ns
from typing import Callable, Protocol

from .rectified_stereo import (
    AXIS_BEAT_BYTES,
    MIN_CONFIDENCE_RAW,
    MIN_DISPARITY_RAW,
    RectifiedStereoRequest,
    RectifiedStereoResult,
    pack_rectified_stereo_request,
    reference_rectified_stereo_output,
    unpack_rectified_stereo_result,
)


class RectifiedStereoBackend(Protocol):
    """一次批量交换校正关键点请求和结果。"""

    def exchange(
        self,
        requests: tuple[RectifiedStereoRequest, ...],
    ) -> tuple[RectifiedStereoResult, ...]: ...


class ReferenceRectifiedStereoBackend:
    """与 ``rectified_triangulation_axis`` 位精确一致的软件后端。"""

    backend_name = "reference"

    def __init__(
        self,
        *,
        minimum_disparity: int = MIN_DISPARITY_RAW,
        minimum_confidence: int = MIN_CONFIDENCE_RAW,
        clock_ns: Callable[[], int] = monotonic_ns,
        latency_window_size: int = 1024,
    ) -> None:
        if not 1 <= minimum_disparity <= (1 << 17) - 1:
            raise ValueError("minimum_disparity must fit positive 17-bit raw units")
        if not 0 <= minimum_confidence <= 255:
            raise ValueError("minimum_confidence must fit uint8 raw units")
        if latency_window_size <= 0:
            raise ValueError("rectified backend latency_window_size must be positive")
        self.minimum_disparity = minimum_disparity
        self.minimum_confidence = minimum_confidence
        self.clock_ns = clock_ns
        self.latency_window_size = latency_window_size
        self._batches = 0
        self._input_beats = 0
        self._tx_bytes = 0
        self._rx_bytes = 0
        self._total_ns = 0
        self._max_ns = 0
        self._latency_samples_ns: deque[int] = deque(maxlen=latency_window_size)

    def exchange(
        self,
        requests: tuple[RectifiedStereoRequest, ...],
    ) -> tuple[RectifiedStereoResult, ...]:
        if not requests:
            return ()
        start_ns = self.clock_ns()
        outputs = tuple(
            reference_rectified_stereo_output(
                request,
                minimum_disparity=self.minimum_disparity,
                minimum_confidence=self.minimum_confidence,
            )
            for request in requests
        )
        elapsed_ns = self.clock_ns() - start_ns
        if elapsed_ns < 0:
            raise ValueError("rectified backend clock must be monotonic")
        self._batches += 1
        self._input_beats += len(requests)
        self._tx_bytes += len(requests) * AXIS_BEAT_BYTES
        self._rx_bytes += len(outputs) * AXIS_BEAT_BYTES
        self._total_ns += elapsed_ns
        self._max_ns = max(self._max_ns, elapsed_ns)
        self._latency_samples_ns.append(elapsed_ns)
        return outputs

    def stats(self) -> RectifiedStereoBackendStats:
        return RectifiedStereoBackendStats(
            batches=self._batches,
            input_beats=self._input_beats,
            tx_bytes=self._tx_bytes,
            rx_bytes=self._rx_bytes,
            total_ns=self._total_ns,
            max_ns=self._max_ns,
            latency_window_size=self.latency_window_size,
            latency_sample_count=len(self._latency_samples_ns),
            p95_ns=_nearest_rank_p95(self._latency_samples_ns),
        )


@dataclass(frozen=True, slots=True)
class RectifiedStereoBackendStats:
    batches: int
    input_beats: int
    tx_bytes: int
    rx_bytes: int
    total_ns: int
    max_ns: int
    latency_window_size: int
    latency_sample_count: int
    p95_ns: int | None

    @property
    def mean_ms(self) -> float | None:
        return None if self.batches == 0 else self.total_ns / self.batches / 1_000_000

    @property
    def p95_ms(self) -> float | None:
        return None if self.p95_ns is None else self.p95_ns / 1_000_000

    def to_record(self) -> dict[str, int | float | None]:
        """返回会话报告使用的稳定字段。"""

        return {
            "batches": self.batches,
            "input_beats": self.input_beats,
            "tx_bytes": self.tx_bytes,
            "rx_bytes": self.rx_bytes,
            "total_ms": self.total_ns / 1_000_000,
            "mean_ms": self.mean_ms,
            "max_ms": self.max_ns / 1_000_000,
            "p95_ms": self.p95_ms,
            "latency_sample_count": self.latency_sample_count,
            "latency_window_size": self.latency_window_size,
        }


@dataclass(slots=True)
class PackedDmaRectifiedStereoBackend:
    """将等长字节传输适配为校正三角测量后端。"""

    transfer: Callable[[bytes], bytes]
    close_callback: Callable[[], None] | None = None
    clock_ns: Callable[[], int] = monotonic_ns
    latency_window_size: int = 1024
    backend_name: str = "packed-dma"
    _batches: int = field(init=False, default=0)
    _input_beats: int = field(init=False, default=0)
    _tx_bytes: int = field(init=False, default=0)
    _rx_bytes: int = field(init=False, default=0)
    _total_ns: int = field(init=False, default=0)
    _max_ns: int = field(init=False, default=0)
    _latency_samples_ns: deque[int] = field(init=False)

    def __post_init__(self) -> None:
        if self.latency_window_size <= 0:
            raise ValueError("rectified backend latency_window_size must be positive")
        self._latency_samples_ns = deque(maxlen=self.latency_window_size)

    def exchange(
        self,
        requests: tuple[RectifiedStereoRequest, ...],
    ) -> tuple[RectifiedStereoResult, ...]:
        if not requests:
            return ()
        payload = b"".join(pack_rectified_stereo_request(request) for request in requests)
        start_ns = self.clock_ns()
        output = self.transfer(payload)
        elapsed_ns = self.clock_ns() - start_ns
        if elapsed_ns < 0:
            raise ValueError("rectified backend clock must be monotonic")
        if len(output) != len(payload) or len(output) % AXIS_BEAT_BYTES != 0:
            raise ValueError("rectified DMA output length does not match input beats")
        results = tuple(
            unpack_rectified_stereo_result(
                output[index * AXIS_BEAT_BYTES : (index + 1) * AXIS_BEAT_BYTES],
                last=index == len(requests) - 1,
            )
            for index in range(len(requests))
        )
        if any(result.joint_id != request.joint_id for request, result in zip(requests, results, strict=True)):
            raise ValueError("rectified DMA result joint_id does not match request")
        self._batches += 1
        self._input_beats += len(requests)
        self._tx_bytes += len(payload)
        self._rx_bytes += len(output)
        self._total_ns += elapsed_ns
        self._max_ns = max(self._max_ns, elapsed_ns)
        self._latency_samples_ns.append(elapsed_ns)
        return results

    def close(self) -> None:
        if self.close_callback is not None:
            self.close_callback()

    def stats(self) -> RectifiedStereoBackendStats:
        return RectifiedStereoBackendStats(
            batches=self._batches,
            input_beats=self._input_beats,
            tx_bytes=self._tx_bytes,
            rx_bytes=self._rx_bytes,
            total_ns=self._total_ns,
            max_ns=self._max_ns,
            latency_window_size=self.latency_window_size,
            latency_sample_count=len(self._latency_samples_ns),
            p95_ns=_nearest_rank_p95(self._latency_samples_ns),
        )


def _nearest_rank_p95(samples: deque[int]) -> int | None:
    if not samples:
        return None
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


__all__ = [
    "PackedDmaRectifiedStereoBackend",
    "ReferenceRectifiedStereoBackend",
    "RectifiedStereoBackend",
    "RectifiedStereoBackendStats",
]
