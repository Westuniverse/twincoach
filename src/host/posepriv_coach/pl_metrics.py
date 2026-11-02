"""Bit-accurate host contract for the joint-metric AXI4-Stream core."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field, replace
from time import monotonic_ns
from typing import Callable, Iterable, Protocol

from .contracts import CameraView, Keypoint, PoseFrame
from .geometry import to_isotropic_image_coordinates


AXIS_BEAT_BYTES = 16
Q_FRACTION_BITS = 14
Q_SCALE = 1 << Q_FRACTION_BITS
Q_MIN = -(1 << 15)
Q_MAX = (1 << 15) - 1
METRIC_BITS = 35
METRIC_MASK = (1 << METRIC_BITS) - 1


@dataclass(frozen=True, slots=True)
class AxisBeat:
    data: bytes
    last: bool = False


@dataclass(frozen=True, slots=True)
class JointSpec:
    joint_id: int
    point_a: str
    vertex: str
    point_c: str


@dataclass(frozen=True, slots=True)
class JointMetricRequest:
    point_a_x: int
    point_a_y: int
    point_b_x: int
    point_b_y: int
    point_c_x: int
    point_c_y: int
    joint_id: int
    view_id: int


@dataclass(frozen=True, slots=True)
class PreparedJoint:
    frame_id: int
    confidence: float
    request: JointMetricRequest
    beat: AxisBeat


@dataclass(frozen=True, slots=True)
class JointMetricResult:
    dot_product: int
    magnitude_sq_ab: int
    magnitude_sq_cb: int
    joint_id: int
    view_id: int
    last: bool


@dataclass(frozen=True, slots=True)
class ResolvedJointMetric:
    frame_id: int
    confidence: float
    result: JointMetricResult


@dataclass(frozen=True, slots=True)
class JointMetricBackendStats:
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


class JointMetricBackend(Protocol):
    """Ordered transaction interface shared by reference and DMA backends."""

    def exchange(self, input_beats: tuple[AxisBeat, ...]) -> tuple[AxisBeat, ...]: ...


class ReferenceJointMetricBackend:
    """Bit-accurate software implementation of the RTL transaction."""

    def exchange(self, input_beats: tuple[AxisBeat, ...]) -> tuple[AxisBeat, ...]:
        return tuple(reference_joint_output(beat) for beat in input_beats)


@dataclass(slots=True)
class PackedDmaJointMetricBackend:
    """Adapt a same-length byte payload transport to the joint backend protocol."""

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
            raise ValueError("joint backend latency_window_size must be positive")
        self._latency_samples_ns = deque(maxlen=self.latency_window_size)

    def exchange(self, input_beats: tuple[AxisBeat, ...]) -> tuple[AxisBeat, ...]:
        if not input_beats:
            return ()
        payload = b"".join(beat.data for beat in input_beats)
        start_ns = self.clock_ns()
        output = self.transfer(payload)
        elapsed_ns = self.clock_ns() - start_ns
        if elapsed_ns < 0:
            raise ValueError("joint backend clock must be monotonic")
        if len(output) != len(payload):
            raise ValueError("DMA output length does not match input length")
        self._batches += 1
        self._input_beats += len(input_beats)
        self._tx_bytes += len(payload)
        self._rx_bytes += len(output)
        self._total_ns += elapsed_ns
        self._max_ns = max(self._max_ns, elapsed_ns)
        self._latency_samples_ns.append(elapsed_ns)
        beat_count = len(input_beats)
        return tuple(
            AxisBeat(
                output[index * AXIS_BEAT_BYTES : (index + 1) * AXIS_BEAT_BYTES],
                last=index == beat_count - 1,
            )
            for index in range(beat_count)
        )

    def close(self) -> None:
        if self.close_callback is not None:
            self.close_callback()

    def stats(self) -> JointMetricBackendStats:
        return JointMetricBackendStats(
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
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def quantize_q1_14(value: float) -> int:
    """Quantize with nearest rounding and saturation to signed 16 bit."""

    if not math.isfinite(value):
        raise ValueError("coordinate must be finite")
    scaled = value * Q_SCALE
    rounded = math.floor(scaled + 0.5) if scaled >= 0.0 else math.ceil(scaled - 0.5)
    return min(Q_MAX, max(Q_MIN, rounded))


def dequantize_q1_14(value: int) -> float:
    return value / Q_SCALE


def _view_id(view: CameraView) -> int:
    return 0 if view is CameraView.FRONT else 1


def _signed(raw: int, width: int) -> int:
    sign_bit = 1 << (width - 1)
    return raw - (1 << width) if raw & sign_bit else raw


def _check_request_fields(request: JointMetricRequest) -> None:
    coordinates = (
        request.point_a_x,
        request.point_a_y,
        request.point_b_x,
        request.point_b_y,
        request.point_c_x,
        request.point_c_y,
    )
    if any(value < Q_MIN or value > Q_MAX for value in coordinates):
        raise ValueError("quantized coordinates must fit signed 16 bit")
    if not 0 <= request.joint_id <= 0xFF:
        raise ValueError("joint_id must fit 8 bit")
    if request.view_id not in (0, 1):
        raise ValueError("view_id must be 0 or 1")


def pack_joint_request(request: JointMetricRequest, *, last: bool = False) -> AxisBeat:
    """Pack one request using the RTL TDATA layout and little-endian memory order."""

    _check_request_fields(request)
    word = 0
    for shift, value in zip(
        (0, 16, 32, 48, 64, 80),
        (
            request.point_a_x,
            request.point_a_y,
            request.point_b_x,
            request.point_b_y,
            request.point_c_x,
            request.point_c_y,
        ),
        strict=True,
    ):
        word |= (value & 0xFFFF) << shift
    word |= request.joint_id << 96
    word |= request.view_id << 104
    return AxisBeat(word.to_bytes(AXIS_BEAT_BYTES, "little"), last)


def unpack_joint_request(beat: AxisBeat) -> JointMetricRequest:
    if len(beat.data) != AXIS_BEAT_BYTES:
        raise ValueError("AXI beat must contain 16 bytes")
    word = int.from_bytes(beat.data, "little")
    coordinates = tuple(_signed((word >> shift) & 0xFFFF, 16) for shift in range(0, 96, 16))
    return JointMetricRequest(
        *coordinates,
        joint_id=(word >> 96) & 0xFF,
        view_id=(word >> 104) & 0x1,
    )


def reference_joint_output(input_beat: AxisBeat) -> AxisBeat:
    """Return the exact output beat expected from the current RTL arithmetic."""

    request = unpack_joint_request(input_beat)
    ab_x = request.point_a_x - request.point_b_x
    ab_y = request.point_a_y - request.point_b_y
    cb_x = request.point_c_x - request.point_b_x
    cb_y = request.point_c_y - request.point_b_y
    dot_product = ab_x * cb_x + ab_y * cb_y
    magnitude_sq_ab = ab_x * ab_x + ab_y * ab_y
    magnitude_sq_cb = cb_x * cb_x + cb_y * cb_y

    word = dot_product & METRIC_MASK
    word |= (magnitude_sq_ab & METRIC_MASK) << 35
    word |= (magnitude_sq_cb & METRIC_MASK) << 70
    word |= request.joint_id << 105
    word |= request.view_id << 113
    return AxisBeat(word.to_bytes(AXIS_BEAT_BYTES, "little"), input_beat.last)


def unpack_joint_result(beat: AxisBeat) -> JointMetricResult:
    if len(beat.data) != AXIS_BEAT_BYTES:
        raise ValueError("AXI beat must contain 16 bytes")
    word = int.from_bytes(beat.data, "little")
    return JointMetricResult(
        dot_product=_signed(word & METRIC_MASK, METRIC_BITS),
        magnitude_sq_ab=(word >> 35) & METRIC_MASK,
        magnitude_sq_cb=(word >> 70) & METRIC_MASK,
        joint_id=(word >> 105) & 0xFF,
        view_id=(word >> 113) & 0x1,
        last=beat.last,
    )


def joint_angle_degrees_from_result(result: JointMetricResult) -> float | None:
    """Convert the PL dot products to the smaller joint angle on the PS."""

    magnitude_product_sq = result.magnitude_sq_ab * result.magnitude_sq_cb
    if magnitude_product_sq == 0:
        return None
    cosine = result.dot_product / math.sqrt(magnitude_product_sq)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def resolve_joint_results(
    prepared: Iterable[PreparedJoint],
    output_beats: Iterable[AxisBeat],
) -> tuple[ResolvedJointMetric, ...]:
    """Join ordered PL results with frame IDs and confidence retained by the PS."""

    pending = tuple(prepared)
    results = tuple(unpack_joint_result(beat) for beat in output_beats)
    if len(pending) != len(results):
        raise ValueError("PL result count does not match the submitted joint count")

    resolved = []
    for submitted, result in zip(pending, results, strict=True):
        if (result.joint_id, result.view_id) != (
            submitted.request.joint_id,
            submitted.request.view_id,
        ):
            raise ValueError("PL result metadata does not match submission order")
        if result.last != submitted.beat.last:
            raise ValueError("PL result TLAST does not match the submitted frame boundary")
        resolved.append(ResolvedJointMetric(submitted.frame_id, submitted.confidence, result))
    return tuple(resolved)


def combine_joint_batches(
    *batches: Iterable[PreparedJoint],
) -> tuple[PreparedJoint, ...]:
    """Combine pose batches into one DMA transaction with one final TLAST."""

    flattened = tuple(item for batch in batches for item in batch)
    return tuple(
        replace(
            item,
            beat=AxisBeat(item.beat.data, last=index == len(flattened) - 1),
        )
        for index, item in enumerate(flattened)
    )


def run_joint_metrics(
    prepared: Iterable[PreparedJoint],
    backend: JointMetricBackend,
) -> tuple[ResolvedJointMetric, ...]:
    """Execute one prepared transaction and restore its host-side metadata."""

    transaction = tuple(prepared)
    if not transaction:
        return ()
    outputs = backend.exchange(tuple(item.beat for item in transaction))
    return resolve_joint_results(transaction, outputs)


def prepare_pose_joints(
    frame: PoseFrame,
    specs: Iterable[JointSpec],
    *,
    minimum_keypoint_confidence: float = 0.0,
) -> tuple[PreparedJoint, ...]:
    """Build one ordered PL transaction batch, skipping unavailable low-confidence joints."""

    pending: list[tuple[JointMetricRequest, float]] = []
    for spec in specs:
        try:
            point_a = frame.keypoints[spec.point_a]
            vertex = frame.keypoints[spec.vertex]
            point_c = frame.keypoints[spec.point_c]
        except KeyError:
            continue
        confidence = min(point_a.confidence, vertex.confidence, point_c.confidence)
        if confidence < minimum_keypoint_confidence:
            continue
        point_a = to_isotropic_image_coordinates(
            point_a,
            frame.source_width,
            frame.source_height,
        )
        vertex = to_isotropic_image_coordinates(
            vertex,
            frame.source_width,
            frame.source_height,
        )
        point_c = to_isotropic_image_coordinates(
            point_c,
            frame.source_width,
            frame.source_height,
        )
        pending.append(
            (
                JointMetricRequest(
                    quantize_q1_14(point_a.x),
                    quantize_q1_14(point_a.y),
                    quantize_q1_14(vertex.x),
                    quantize_q1_14(vertex.y),
                    quantize_q1_14(point_c.x),
                    quantize_q1_14(point_c.y),
                    spec.joint_id,
                    _view_id(frame.view),
                ),
                confidence,
            )
        )

    prepared = []
    for index, (request, confidence) in enumerate(pending):
        beat = pack_joint_request(request, last=index == len(pending) - 1)
        prepared.append(PreparedJoint(frame.frame_id, confidence, request, beat))
    return tuple(prepared)
