"""位精确的时序运动特征 AXI4-Stream 软件契约。"""

from __future__ import annotations

from dataclasses import dataclass
from struct import Struct

from .pl_metrics import AXIS_BEAT_BYTES, METRIC_MASK, AxisBeat


MOTION_STATS_COUNT_BITS = 16
MOTION_STATS_SUM_BITS = 35 + MOTION_STATS_COUNT_BITS
MOTION_STATS_SUM_MASK = (1 << MOTION_STATS_SUM_BITS) - 1
_MOTION_REQUEST_STRUCT = Struct("<6hBB2x")


@dataclass(frozen=True, slots=True)
class MotionMetricRequest:
    point_t0_x: int
    point_t0_y: int
    point_t1_x: int
    point_t1_y: int
    point_t2_x: int
    point_t2_y: int
    joint_id: int
    view_id: int


@dataclass(frozen=True, slots=True)
class MotionMetricResult:
    velocity_t1_sq: int
    velocity_t2_sq: int
    acceleration_sq: int
    joint_id: int
    view_id: int
    last: bool


@dataclass(frozen=True, slots=True)
class MotionBatchStatsResult:
    """One compact summary emitted by ``temporal_motion_stats_axis``."""

    sum_acceleration_sq: int
    max_acceleration_sq: int
    sample_count: int
    first_joint_id: int
    view_id: int
    last: bool


def _validate_request(request: MotionMetricRequest) -> None:
    if not (
        -32768 <= request.point_t0_x <= 32767
        and -32768 <= request.point_t0_y <= 32767
        and -32768 <= request.point_t1_x <= 32767
        and -32768 <= request.point_t1_y <= 32767
        and -32768 <= request.point_t2_x <= 32767
        and -32768 <= request.point_t2_y <= 32767
    ):
        raise ValueError("quantized coordinates must fit signed 16 bit")
    if not 0 <= request.joint_id <= 0xFF:
        raise ValueError("joint_id must fit 8 bit")
    if request.view_id not in (0, 1):
        raise ValueError("view_id must be 0 or 1")


def pack_motion_request(request: MotionMetricRequest, *, last: bool = False) -> AxisBeat:
    """按 temporal_motion_axis 输入位布局打包一个三时刻关键点。"""

    _validate_request(request)
    data = _MOTION_REQUEST_STRUCT.pack(
        request.point_t0_x,
        request.point_t0_y,
        request.point_t1_x,
        request.point_t1_y,
        request.point_t2_x,
        request.point_t2_y,
        request.joint_id,
        request.view_id,
    )
    return AxisBeat(data, last)


def unpack_motion_request(beat: AxisBeat) -> MotionMetricRequest:
    if len(beat.data) != AXIS_BEAT_BYTES:
        raise ValueError("AXI beat must contain 16 bytes")
    values = _MOTION_REQUEST_STRUCT.unpack(beat.data)
    return MotionMetricRequest(
        *values[:6],
        joint_id=values[6],
        view_id=values[7] & 0x1,
    )


def reference_motion_output(input_beat: AxisBeat) -> AxisBeat:
    """生成与 RTL 相同的速度平方和加速度平方输出。"""

    request = unpack_motion_request(input_beat)
    velocity_t1_x = request.point_t1_x - request.point_t0_x
    velocity_t1_y = request.point_t1_y - request.point_t0_y
    velocity_t2_x = request.point_t2_x - request.point_t1_x
    velocity_t2_y = request.point_t2_y - request.point_t1_y
    acceleration_x = velocity_t2_x - velocity_t1_x
    acceleration_y = velocity_t2_y - velocity_t1_y
    velocity_t1_sq = velocity_t1_x**2 + velocity_t1_y**2
    velocity_t2_sq = velocity_t2_x**2 + velocity_t2_y**2
    acceleration_sq = acceleration_x**2 + acceleration_y**2

    word = velocity_t1_sq & METRIC_MASK
    word |= (velocity_t2_sq & METRIC_MASK) << 35
    word |= (acceleration_sq & METRIC_MASK) << 70
    word |= request.joint_id << 105
    word |= request.view_id << 113
    return AxisBeat(word.to_bytes(AXIS_BEAT_BYTES, "little"), input_beat.last)


def unpack_motion_result(beat: AxisBeat) -> MotionMetricResult:
    if len(beat.data) != AXIS_BEAT_BYTES:
        raise ValueError("AXI beat must contain 16 bytes")
    word = int.from_bytes(beat.data, "little")
    return MotionMetricResult(
        velocity_t1_sq=word & METRIC_MASK,
        velocity_t2_sq=(word >> 35) & METRIC_MASK,
        acceleration_sq=(word >> 70) & METRIC_MASK,
        joint_id=(word >> 105) & 0xFF,
        view_id=(word >> 113) & 0x1,
        last=beat.last,
    )


def reference_motion_stats_output(
    input_beats: tuple[AxisBeat, ...],
) -> AxisBeat:
    """Aggregate one complete temporal-motion output batch.

    The aggregate is intentionally limited to anonymous telemetry fields.  It
    does not replace ``reference_motion_output`` when per-joint debugging or a
    future classifier needs the individual results.
    """

    if not input_beats:
        raise ValueError("motion stats batch must not be empty")
    results = tuple(unpack_motion_result(beat) for beat in input_beats)
    if not results[-1].last:
        raise ValueError("motion stats batch must end with TLAST")
    if any(result.last for result in results[:-1]):
        raise ValueError("motion stats batch contains an early TLAST")
    view_id = results[0].view_id
    if any(result.view_id != view_id for result in results):
        raise ValueError("motion stats batch must contain one view")
    sum_acceleration_sq = sum(result.acceleration_sq for result in results)
    max_acceleration_sq = max(result.acceleration_sq for result in results)
    sample_count = len(results)
    if sample_count > (1 << MOTION_STATS_COUNT_BITS) - 1:
        raise ValueError("motion stats batch exceeds sample_count field")
    if sum_acceleration_sq > MOTION_STATS_SUM_MASK:
        raise ValueError("motion stats sum exceeds output field")
    word = sum_acceleration_sq & MOTION_STATS_SUM_MASK
    word |= (max_acceleration_sq & METRIC_MASK) << MOTION_STATS_SUM_BITS
    word |= sample_count << (MOTION_STATS_SUM_BITS + 35)
    word |= results[0].joint_id << (MOTION_STATS_SUM_BITS + 35 + MOTION_STATS_COUNT_BITS)
    word |= view_id << (MOTION_STATS_SUM_BITS + 35 + MOTION_STATS_COUNT_BITS + 8)
    return AxisBeat(word.to_bytes(AXIS_BEAT_BYTES, "little"), last=True)


def unpack_motion_stats_result(beat: AxisBeat) -> MotionBatchStatsResult:
    """Decode one compact aggregate beat and validate its reserved bits."""

    if len(beat.data) != AXIS_BEAT_BYTES:
        raise ValueError("AXI beat must contain 16 bytes")
    word = int.from_bytes(beat.data, "little")
    view_shift = MOTION_STATS_SUM_BITS + 35 + MOTION_STATS_COUNT_BITS + 8
    reserved_mask = ((1 << 128) - 1) ^ ((1 << (view_shift + 1)) - 1)
    if word & reserved_mask:
        raise ValueError("motion stats reserved bits must be zero")
    return MotionBatchStatsResult(
        sum_acceleration_sq=word & MOTION_STATS_SUM_MASK,
        max_acceleration_sq=(word >> MOTION_STATS_SUM_BITS) & METRIC_MASK,
        sample_count=(word >> (MOTION_STATS_SUM_BITS + 35))
        & ((1 << MOTION_STATS_COUNT_BITS) - 1),
        first_joint_id=(word >> (MOTION_STATS_SUM_BITS + 35 + MOTION_STATS_COUNT_BITS))
        & 0xFF,
        view_id=(word >> view_shift) & 0x1,
        last=beat.last,
    )
