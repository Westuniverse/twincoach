"""Selectable host backends for the temporal-motion AXI stream."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from functools import partial
import math
from pathlib import Path
from time import monotonic_ns
from typing import Callable, Protocol

from .linux_dma_device import DeviceOpener
from .linux_shared_pose_dma import LinuxSharedPoseDma
from .linux_motion_dma import (
    LinuxMotionDmaProxyTransport,
    LinuxMotionDmaTransport,
)
from .pl_metrics import AXIS_BEAT_BYTES, AxisBeat
from .pl_motion import (
    reference_motion_output,
    reference_motion_stats_output,
    unpack_motion_stats_result,
)
from .pl_motion_summary import (
    MOTION_SUMMARY_MAX_INPUT_BEATS,
    reference_motion_summary_output,
)
from .verilator_transport import VerilatorAxisTransport, VerilatorBridgeError


MOTION_BACKEND_NAMES = ("reference", "verilator", "linux-dma", "linux-dma-proxy")
MOTION_STATS_BACKEND_NAMES = ("reference", "linux-dma", "linux-dma-proxy")
MOTION_SUMMARY_BACKEND_NAMES = ("reference", "linux-dma-proxy", "linux-shared-dma")


class MotionMetricBackend(Protocol):
    """Ordered batch interface shared by reference, Verilator and DMA paths."""

    def __call__(self, input_beats: tuple[AxisBeat, ...]) -> tuple[AxisBeat, ...]: ...


class MotionStatsAggregator(Protocol):
    """Aggregate one completed temporal-motion output batch."""

    def __call__(self, output_beats: tuple[AxisBeat, ...]) -> AxisBeat: ...


class MotionSummaryBackend(Protocol):
    """Summarize coordinate requests, NOT already computed motion results."""

    def __call__(self, input_beats: tuple[AxisBeat, ...]) -> AxisBeat: ...


@dataclass(frozen=True, slots=True)
class MotionMetricBackendStats:
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


@dataclass(frozen=True, slots=True)
class MotionStatsBackendStats:
    """传输 ``temporal_motion_stats_axis`` 单拍结果时的可解释遥测。"""

    backend: str
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

    def to_record(self) -> dict[str, int | float | str | None]:
        return {
            "backend": self.backend,
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


class ReferenceMotionMetricBackend:
    """Bit-accurate software implementation of ``temporal_motion_axis``."""

    def __call__(self, input_beats: tuple[AxisBeat, ...]) -> tuple[AxisBeat, ...]:
        return tuple(reference_motion_output(beat) for beat in input_beats)


@dataclass(slots=True)
class ReferenceMotionStatsAggregator:
    """软件黄金模型，保持与 ``temporal_motion_stats_axis`` 位布局一致。"""

    latency_window_size: int = 1024
    _batches: int = field(init=False, default=0)
    _input_beats: int = field(init=False, default=0)

    backend_name: str = field(init=False, default="reference")

    def __post_init__(self) -> None:
        if self.latency_window_size <= 0:
            raise ValueError("motion stats latency_window_size must be positive")

    def __call__(self, output_beats: tuple[AxisBeat, ...]) -> AxisBeat:
        summary = reference_motion_stats_output(output_beats)
        self._batches += 1
        self._input_beats += len(output_beats)
        return summary

    def stats(self) -> MotionStatsBackendStats:
        return MotionStatsBackendStats(
            backend=self.backend_name,
            batches=self._batches,
            input_beats=self._input_beats,
            tx_bytes=self._input_beats * AXIS_BEAT_BYTES,
            rx_bytes=self._batches * AXIS_BEAT_BYTES,
            total_ns=0,
            max_ns=0,
            latency_window_size=self.latency_window_size,
            latency_sample_count=0,
            p95_ns=None,
        )


@dataclass(slots=True)
class ReferenceMotionSummaryBackend(ReferenceMotionStatsAggregator):
    """Coordinate-to-summary golden model; counters are not DMA measurements."""

    backend_name: str = field(init=False, default="motion-summary-reference")

    def __call__(self, input_beats: tuple[AxisBeat, ...]) -> AxisBeat:
        summary = reference_motion_summary_output(input_beats)
        self._batches += 1
        self._input_beats += len(input_beats)
        return summary


@dataclass(slots=True)
class PackedDmaMotionMetricBackend:
    """Adapt a same-length byte transport to the motion batch protocol."""

    transfer: Callable[[bytes], bytes]
    close_callback: Callable[[], None] | None = None
    clock_ns: Callable[[], int] = monotonic_ns
    latency_window_size: int = 1024
    _batches: int = field(init=False, default=0)
    _input_beats: int = field(init=False, default=0)
    _tx_bytes: int = field(init=False, default=0)
    _rx_bytes: int = field(init=False, default=0)
    _total_ns: int = field(init=False, default=0)
    _max_ns: int = field(init=False, default=0)
    _latency_samples_ns: deque[int] = field(init=False)

    def __post_init__(self) -> None:
        if self.latency_window_size <= 0:
            raise ValueError("motion backend latency_window_size must be positive")
        self._latency_samples_ns = deque(maxlen=self.latency_window_size)

    def __call__(self, input_beats: tuple[AxisBeat, ...]) -> tuple[AxisBeat, ...]:
        if not input_beats:
            return ()
        if any(len(beat.data) != AXIS_BEAT_BYTES for beat in input_beats):
            raise ValueError("motion backend input must contain 16-byte AXI beats")
        payload = b"".join(beat.data for beat in input_beats)
        start_ns = self.clock_ns()
        output = self.transfer(payload)
        elapsed_ns = self.clock_ns() - start_ns
        if elapsed_ns < 0:
            raise ValueError("motion backend clock must be monotonic")
        if len(output) != len(payload):
            raise ValueError("motion DMA output length does not match input length")
        self._batches += 1
        self._input_beats += len(input_beats)
        self._tx_bytes += len(payload)
        self._rx_bytes += len(output)
        self._total_ns += elapsed_ns
        self._max_ns = max(self._max_ns, elapsed_ns)
        self._latency_samples_ns.append(elapsed_ns)
        return tuple(
            AxisBeat(
                output[index * AXIS_BEAT_BYTES : (index + 1) * AXIS_BEAT_BYTES],
                last=index == len(input_beats) - 1,
            )
            for index in range(len(input_beats))
        )

    def close(self) -> None:
        if self.close_callback is not None:
            self.close_callback()

    def stats(self) -> MotionMetricBackendStats:
        return MotionMetricBackendStats(
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


@dataclass(slots=True)
class PackedDmaMotionStatsAggregator:
    """把聚合核接到可变输入、固定 16 字节输出的 DMA 事务。"""

    transfer: Callable[[bytes], bytes]
    close_callback: Callable[[], None] | None = None
    clock_ns: Callable[[], int] = monotonic_ns
    latency_window_size: int = 1024
    backend_name: str = "linux-dma"
    _batches: int = field(init=False, default=0)
    _input_beats: int = field(init=False, default=0)
    _tx_bytes: int = field(init=False, default=0)
    _rx_bytes: int = field(init=False, default=0)
    _total_ns: int = field(init=False, default=0)
    _max_ns: int = field(init=False, default=0)
    _latency_samples_ns: deque[int] = field(init=False)

    def __post_init__(self) -> None:
        if self.latency_window_size <= 0:
            raise ValueError("motion stats latency_window_size must be positive")
        if not self.backend_name:
            raise ValueError("motion stats backend name must not be empty")
        self._latency_samples_ns = deque(maxlen=self.latency_window_size)

    def __call__(self, output_beats: tuple[AxisBeat, ...]) -> AxisBeat:
        return self._exchange(output_beats)

    def _exchange(
        self,
        output_beats: tuple[AxisBeat, ...],
        *,
        validate_reply: Callable[[AxisBeat], None] | None = None,
    ) -> AxisBeat:
        if not output_beats:
            raise ValueError("motion stats input batch must not be empty")
        if any(len(beat.data) != AXIS_BEAT_BYTES for beat in output_beats):
            raise ValueError("motion stats input must contain 16-byte AXI beats")
        payload = b"".join(beat.data for beat in output_beats)
        start_ns = self.clock_ns()
        output = self.transfer(payload)
        elapsed_ns = self.clock_ns() - start_ns
        if elapsed_ns < 0:
            raise ValueError("motion stats clock must be monotonic")
        if len(output) != AXIS_BEAT_BYTES:
            raise ValueError("motion stats DMA output must contain one 16-byte beat")
        summary = AxisBeat(output, last=True)
        if validate_reply is not None:
            validate_reply(summary)
        self._batches += 1
        self._input_beats += len(output_beats)
        self._tx_bytes += len(payload)
        self._rx_bytes += len(output)
        self._total_ns += elapsed_ns
        self._max_ns = max(self._max_ns, elapsed_ns)
        self._latency_samples_ns.append(elapsed_ns)
        return summary

    def close(self) -> None:
        if self.close_callback is not None:
            self.close_callback()

    def stats(self) -> MotionStatsBackendStats:
        return MotionStatsBackendStats(
            backend=self.backend_name,
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


@dataclass(slots=True)
class PackedDmaMotionSummaryBackend(PackedDmaMotionStatsAggregator):
    """MOTION-SUMMARYv1: coordinate TX, fixed 16-byte summary RX.

    Malformed inputs never reach DMA. After an exchange/response failure the
    instance cannot retry an uncertain transaction; close and recreate it.
    Validation checks the ABI, not a hidden software recomputation of the sum.
    """

    backend_name: str = "motion-summary-linux-dma"
    _failed: bool = field(init=False, default=False)

    def __call__(self, input_beats: tuple[AxisBeat, ...]) -> AxisBeat:
        if self._failed:
            raise RuntimeError("motion summary backend failed; close and recreate it")
        _validate_summary_requests(input_beats)

        def validate_reply(beat: AxisBeat) -> None:
            result = unpack_motion_stats_result(beat)
            if result.sample_count != len(input_beats):
                raise ValueError("motion summary changed sample count")
            if result.first_joint_id != input_beats[0].data[12]:
                raise ValueError("motion summary changed first joint metadata")
            if result.view_id != input_beats[0].data[13]:
                raise ValueError("motion summary changed view metadata")
            if not (
                result.max_acceleration_sq <= result.sum_acceleration_sq
                <= result.max_acceleration_sq * result.sample_count
            ):
                raise ValueError("motion summary sum/max range is inconsistent")

        try:
            return self._exchange(input_beats, validate_reply=validate_reply)
        except Exception:
            self._failed = True
            raise


def _validate_summary_requests(input_beats: tuple[AxisBeat, ...]) -> None:
    if not 1 <= len(input_beats) <= MOTION_SUMMARY_MAX_INPUT_BEATS:
        raise ValueError("motion summary requires 1..8192 coordinate beats")
    view = None
    for index, beat in enumerate(input_beats):
        if len(beat.data) != AXIS_BEAT_BYTES:
            raise ValueError("motion summary input must contain 16-byte AXI beats")
        if beat.data[13] > 1 or any(beat.data[14:]):
            raise ValueError("motion summary coordinate reserved bits must be zero")
        if view is None:
            view = beat.data[13]
        if beat.data[13] != view:
            raise ValueError("motion summary requires one view per batch")
        if beat.last != (index == len(input_beats) - 1):
            raise ValueError("motion summary requires TLAST only on the final beat")


def load_motion_backend(
    name: str,
    *,
    verilator_executable: Path | None = None,
    work_dir: Path | None = None,
    dma_device: str | Path | None = None,
    dma_device_opener: DeviceOpener | None = None,
    dma_proxy_tx_device: str | Path | None = None,
    dma_proxy_rx_device: str | Path | None = None,
) -> MotionMetricBackend:
    """Create a selectable temporal-motion backend without changing scoring rules."""

    if name == "reference":
        return ReferenceMotionMetricBackend()
    if name == "linux-dma":
        if dma_device is None:
            raise ValueError("linux-dma motion backend requires a device path")
        options = {}
        if dma_device_opener is not None:
            options["opener"] = dma_device_opener
        transport = LinuxMotionDmaTransport(dma_device, **options)
        return PackedDmaMotionMetricBackend(transport.transfer, transport.close)
    if name == "linux-dma-proxy":
        if dma_proxy_tx_device is None or dma_proxy_rx_device is None:
            raise ValueError("linux-dma-proxy motion backend requires TX and RX paths")
        transport = LinuxMotionDmaProxyTransport(
            dma_proxy_tx_device,
            dma_proxy_rx_device,
        )
        return PackedDmaMotionMetricBackend(transport.transfer, transport.close)
    if name != "verilator":
        raise ValueError(f"unknown motion backend: {name}")
    if verilator_executable is None or work_dir is None:
        raise ValueError("Verilator motion backend requires executable and work_dir")
    executable = verilator_executable.resolve()
    if not executable.is_file():
        raise VerilatorBridgeError(
            f"temporal-motion Verilator bridge not found: {executable}"
        )
    transport = VerilatorAxisTransport(executable, work_dir)
    return PackedDmaMotionMetricBackend(transport.transfer)


def load_motion_stats_backend(
    name: str,
    *,
    dma_device: str | Path | None = None,
    dma_device_opener: DeviceOpener | None = None,
    dma_proxy_tx_device: str | Path | None = None,
    dma_proxy_rx_device: str | Path | None = None,
) -> MotionStatsAggregator | None:
    """创建可选的批次聚合后端；``None`` 表示保持原逐关节路径。"""

    if name == "none":
        return None
    if name == "reference":
        return ReferenceMotionStatsAggregator()
    if name == "linux-dma":
        if dma_device is None:
            raise ValueError("linux-dma motion stats backend requires a device path")
        options = {}
        if dma_device_opener is not None:
            options["opener"] = dma_device_opener
        transport = LinuxMotionDmaTransport(dma_device, **options)
        return PackedDmaMotionStatsAggregator(
            lambda payload: transport.transfer(payload, expected_bytes=AXIS_BEAT_BYTES),
            transport.close,
            backend_name="linux-dma",
        )
    if name == "linux-dma-proxy":
        if dma_proxy_tx_device is None or dma_proxy_rx_device is None:
            raise ValueError(
                "linux-dma-proxy motion stats backend requires TX and RX paths"
            )
        transport = LinuxMotionDmaProxyTransport(
            dma_proxy_tx_device,
            dma_proxy_rx_device,
        )
        return PackedDmaMotionStatsAggregator(
            lambda payload: transport.transfer(payload, expected_bytes=AXIS_BEAT_BYTES),
            transport.close,
            backend_name="linux-dma-proxy",
        )
    raise ValueError(f"unknown motion stats backend: {name}")


def _nearest_rank_p95(samples: deque[int]) -> int | None:
    if not samples:
        return None
    ordered = sorted(samples)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def load_motion_summary_backend(
    name: str,
    *,
    dma_proxy_tx_device: str | Path | None = None,
    dma_proxy_rx_device: str | Path | None = None,
    shared_dma: LinuxSharedPoseDma | None = None,
) -> MotionSummaryBackend | None:
    """Explicit opt-in for the coordinate ABI; never autodetect a raw-stats IP."""
    if name == "none":
        return None
    if name == "reference":
        return ReferenceMotionSummaryBackend()
    if name == "linux-shared-dma":
        if shared_dma is None:
            raise ValueError("linux-shared-dma requires an application-owned shared_dma transport")
        return PackedDmaMotionSummaryBackend(
            partial(shared_dma.exchange_bytes, 1), backend_name="motion-summary-linux-shared-dma",
        )
    if name == "linux-dma-proxy":
        if dma_proxy_tx_device is None or dma_proxy_rx_device is None:
            raise ValueError("linux-dma-proxy motion summary requires TX and RX paths")
        transport = LinuxMotionDmaProxyTransport(
            dma_proxy_tx_device, dma_proxy_rx_device,
        )
        return PackedDmaMotionSummaryBackend(
            lambda payload: transport.transfer(payload, expected_bytes=AXIS_BEAT_BYTES),
            transport.close,
            backend_name="motion-summary-linux-dma-proxy",
        )
    raise ValueError(f"unknown motion summary backend: {name}")


__all__ = [
    "MOTION_BACKEND_NAMES",
    "MOTION_STATS_BACKEND_NAMES",
    "MOTION_SUMMARY_BACKEND_NAMES",
    "MotionMetricBackend",
    "MotionMetricBackendStats",
    "MotionStatsAggregator",
    "MotionStatsBackendStats",
    "MotionSummaryBackend",
    "PackedDmaMotionMetricBackend",
    "PackedDmaMotionStatsAggregator",
    "PackedDmaMotionSummaryBackend",
    "ReferenceMotionMetricBackend",
    "ReferenceMotionStatsAggregator",
    "ReferenceMotionSummaryBackend",
    "load_motion_backend",
    "load_motion_stats_backend",
    "load_motion_summary_backend",
]
