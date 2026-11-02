"""把连续姿态帧组织成时序运动算子可消费的三时刻窗口。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from typing import Any, Callable, Mapping

from .contracts import CameraView, PoseFrame
from .motion_backend import MotionStatsAggregator, MotionSummaryBackend
from .pl_motion import (
    MotionMetricRequest,
    pack_motion_request,
    reference_motion_output,
    unpack_motion_stats_result,
    unpack_motion_result,
)
from .pl_metrics import AxisBeat


MotionBatchBackend = Callable[[tuple[AxisBeat, ...]], tuple[AxisBeat, ...]]


@dataclass(frozen=True, slots=True)
class MotionFeature:
    """一个关节在三个连续采样点上的定点运动特征。"""

    view: CameraView
    joint_name: str
    frame_id: int
    timestamp_ns: int
    confidence: float
    velocity_t1_sq: int
    velocity_t2_sq: int
    acceleration_sq: int
    delta_t1_ns: int
    delta_t2_ns: int
    velocity_t1_per_s_sq: float
    velocity_t2_per_s_sq: float
    acceleration_per_s2_sq: float


@dataclass(frozen=True, slots=True)
class MotionWindowStats:
    frames: int
    feature_count: int
    front_feature_count: int
    side_feature_count: int
    total_acceleration_per_s2_sq: float
    max_acceleration_per_s2_sq: float
    backend: Mapping[str, Any] | None = None
    batch_stats: Mapping[str, Any] | None = None

    @property
    def mean_acceleration_per_s2_sq(self) -> float | None:
        if self.feature_count == 0:
            return None
        return self.total_acceleration_per_s2_sq / self.feature_count


class TemporalMotionWindow:
    """维护有界三时刻窗口，并保留可替换的 Python/PL 批处理边界。

    输出是逐帧差分的平方域特征，不直接解释为米/秒，也不直接触发语音。
    这样在真实相机帧率和时间戳稳定前，不会把采样频率变化误报为“吃力”。
    """

    def __init__(
        self,
        joint_names: tuple[str, ...],
        *,
        minimum_confidence: float = 0.5,
        backend: MotionBatchBackend | None = None,
        stats_aggregator: MotionStatsAggregator | None = None,
        summary_backend: MotionSummaryBackend | None = None,
        coordinate_scale: int = 1 << 14,
    ) -> None:
        if not joint_names or len(set(joint_names)) != len(joint_names):
            raise ValueError("joint_names must be non-empty and unique")
        if any(not name for name in joint_names):
            raise ValueError("joint_names must not contain empty names")
        if not 0.0 <= minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence must be within [0, 1]")
        if coordinate_scale < 1 or coordinate_scale > 32767:
            raise ValueError("coordinate_scale must fit the signed input range")
        if summary_backend is not None and stats_aggregator is not None:
            raise ValueError("summary_backend and stats_aggregator are mutually exclusive")
        self.joint_names = tuple(joint_names)
        self.minimum_confidence = minimum_confidence
        self.coordinate_scale = coordinate_scale
        self.backend = backend or _reference_batch
        self.stats_aggregator = stats_aggregator
        self.summary_backend = summary_backend
        self._history: dict[
            tuple[CameraView, str], deque[tuple[int, int, int]]
        ] = {}
        self._last_frame_id: dict[CameraView, int] = {}
        self._last_timestamp_ns: dict[CameraView, int] = {}
        self._frames = 0
        self._feature_count = 0
        self._view_feature_count = {CameraView.FRONT: 0, CameraView.SIDE: 0}
        self._total_acceleration_per_s2_sq = 0.0
        self._max_acceleration_per_s2_sq = 0.0
        self._batch_stats_batches = 0
        self._batch_stats_sample_count = 0
        self._batch_stats_sum_acceleration_sq = 0
        self._batch_stats_max_acceleration_sq = 0

    def reset(self) -> None:
        """清空窗口；摄像头重连或时间轴切换时调用。"""

        self._history.clear()
        self._last_frame_id.clear()
        self._last_timestamp_ns.clear()
        self._frames = 0
        self._feature_count = 0
        self._view_feature_count = {CameraView.FRONT: 0, CameraView.SIDE: 0}
        self._total_acceleration_per_s2_sq = 0.0
        self._max_acceleration_per_s2_sq = 0.0
        self._batch_stats_batches = 0
        self._batch_stats_sample_count = 0
        self._batch_stats_sum_acceleration_sq = 0
        self._batch_stats_max_acceleration_sq = 0

    def push(self, frame: PoseFrame) -> tuple[MotionFeature, ...]:
        """加入一帧姿态并返回已形成三时刻窗口的关节特征。"""

        previous_frame = self._last_frame_id.get(frame.view)
        previous_timestamp = self._last_timestamp_ns.get(frame.view)
        if previous_frame is not None and frame.frame_id <= previous_frame:
            raise ValueError(f"{frame.view.value} motion frame_id must increase")
        if previous_timestamp is not None and frame.timestamp_ns <= previous_timestamp:
            raise ValueError(f"{frame.view.value} motion timestamp must increase")
        self._last_frame_id[frame.view] = frame.frame_id
        self._last_timestamp_ns[frame.view] = frame.timestamp_ns
        self._frames += 1

        requests: list[MotionMetricRequest] = []
        metadata: list[tuple[str, float, int, int]] = []
        view_id = 0 if frame.view is CameraView.FRONT else 1
        for joint_id, joint_name in enumerate(self.joint_names):
            key = (frame.view, joint_name)
            keypoint = frame.keypoints.get(joint_name)
            if keypoint is None or keypoint.confidence < self.minimum_confidence:
                self._history.pop(key, None)
                continue
            current = (
                frame.timestamp_ns,
                _quantize_coordinate(keypoint.x, self.coordinate_scale),
                _quantize_coordinate(keypoint.y, self.coordinate_scale),
            )
            history = self._history.setdefault(key, deque(maxlen=3))
            history.append(current)
            if len(history) < 3:
                continue
            (timestamp_t0, t0_x, t0_y), (timestamp_t1, t1_x, t1_y), (
                timestamp_t2,
                t2_x,
                t2_y,
            ) = history
            requests.append(
                MotionMetricRequest(
                    t0_x,
                    t0_y,
                    t1_x,
                    t1_y,
                    t2_x,
                    t2_y,
                    joint_id,
                    view_id,
                )
            )
            metadata.append(
                (
                    joint_name,
                    keypoint.confidence,
                    timestamp_t1 - timestamp_t0,
                    timestamp_t2 - timestamp_t1,
                )
            )

        if not requests:
            return ()
        beats = tuple(
            pack_motion_request(request, last=index == len(requests) - 1)
            for index, request in enumerate(requests)
        )
        output = tuple(self.backend(beats))
        if len(output) != len(requests):
            raise ValueError("motion backend returned an unexpected beat count")
        batch_stats_result = None
        if self.summary_backend is not None or self.stats_aggregator is not None:
            batch_stats_result = unpack_motion_stats_result(
                self.summary_backend(beats)
                if self.summary_backend is not None
                else self.stats_aggregator(output)
            )
            if not batch_stats_result.last:
                raise ValueError("motion stats aggregator must assert batch TLAST")
            if batch_stats_result.sample_count != len(output):
                raise ValueError("motion stats aggregator changed sample count")
            if batch_stats_result.view_id != view_id:
                raise ValueError("motion stats aggregator changed view metadata")
            if batch_stats_result.first_joint_id != requests[0].joint_id:
                raise ValueError("motion stats aggregator changed first joint metadata")
        features: list[MotionFeature] = []
        for index, (
            request,
            beat,
            (joint_name, confidence, delta_t1_ns, delta_t2_ns),
        ) in enumerate(
            zip(requests, output, metadata, strict=True)
        ):
            result = unpack_motion_result(beat)
            if result.joint_id != request.joint_id or result.view_id != request.view_id:
                raise ValueError("motion backend changed joint/view metadata")
            if result.last != (index == len(requests) - 1):
                raise ValueError("motion backend changed batch TLAST")
            normalized = _time_normalized_motion(
                request,
                delta_t1_ns=delta_t1_ns,
                delta_t2_ns=delta_t2_ns,
                coordinate_scale=self.coordinate_scale,
            )
            features.append(
                MotionFeature(
                    view=frame.view,
                    joint_name=joint_name,
                    frame_id=frame.frame_id,
                    timestamp_ns=frame.timestamp_ns,
                    confidence=confidence,
                    velocity_t1_sq=result.velocity_t1_sq,
                    velocity_t2_sq=result.velocity_t2_sq,
                    acceleration_sq=result.acceleration_sq,
                    delta_t1_ns=delta_t1_ns,
                    delta_t2_ns=delta_t2_ns,
                    velocity_t1_per_s_sq=normalized[0],
                    velocity_t2_per_s_sq=normalized[1],
                    acceleration_per_s2_sq=normalized[2],
                )
            )
            self._feature_count += 1
            self._view_feature_count[frame.view] += 1
            self._total_acceleration_per_s2_sq += normalized[2]
            self._max_acceleration_per_s2_sq = max(
                self._max_acceleration_per_s2_sq,
                normalized[2],
            )
        if batch_stats_result is not None:
            self._batch_stats_batches += 1
            self._batch_stats_sample_count += batch_stats_result.sample_count
            self._batch_stats_sum_acceleration_sq += (
                batch_stats_result.sum_acceleration_sq
            )
            self._batch_stats_max_acceleration_sq = max(
                self._batch_stats_max_acceleration_sq,
                batch_stats_result.max_acceleration_sq,
            )
        return tuple(features)

    def stats(self) -> MotionWindowStats:
        backend_stats = getattr(self.backend, "stats", None)
        backend_record = None
        if callable(backend_stats):
            value = backend_stats()
            to_record = getattr(value, "to_record", None)
            backend_record = (
                dict(to_record())
                if callable(to_record)
                else dict(value)
                if isinstance(value, Mapping)
                else None
            )
        batch_stats_record = None
        aggregator = (
            self.summary_backend
            if self.summary_backend is not None else self.stats_aggregator
        )
        if aggregator is not None:
            aggregator_stats = getattr(aggregator, "stats", None)
            aggregator_record = None
            if callable(aggregator_stats):
                value = aggregator_stats()
                to_record = getattr(value, "to_record", None)
                aggregator_record = (
                    dict(to_record())
                    if callable(to_record)
                    else dict(value)
                    if isinstance(value, Mapping)
                    else None
                )
            batch_stats_record = {
                "batches": self._batch_stats_batches,
                "sample_count": self._batch_stats_sample_count,
                "sum_acceleration_sq": self._batch_stats_sum_acceleration_sq,
                "max_acceleration_sq": self._batch_stats_max_acceleration_sq,
            }
            if aggregator_record is not None:
                batch_stats_record["backend"] = aggregator_record
            if self.summary_backend is not None:
                batch_stats_record["input_kind"] = "coordinates"
        return MotionWindowStats(
            frames=self._frames,
            feature_count=self._feature_count,
            front_feature_count=self._view_feature_count[CameraView.FRONT],
            side_feature_count=self._view_feature_count[CameraView.SIDE],
            total_acceleration_per_s2_sq=self._total_acceleration_per_s2_sq,
            max_acceleration_per_s2_sq=self._max_acceleration_per_s2_sq,
            backend=backend_record,
            batch_stats=batch_stats_record,
        )


def _reference_batch(beats: tuple[AxisBeat, ...]) -> tuple[AxisBeat, ...]:
    return tuple(reference_motion_output(beat) for beat in beats)


def _quantize_coordinate(value: float, scale: int) -> int:
    if not math.isfinite(value):
        raise ValueError("motion coordinate must be finite")
    quantized = round(value * scale)
    if quantized < -32768 or quantized > 32767:
        raise ValueError("motion coordinate exceeds signed 16-bit range")
    return int(quantized)


def _time_normalized_motion(
    request: MotionMetricRequest,
    *,
    delta_t1_ns: int,
    delta_t2_ns: int,
    coordinate_scale: int,
) -> tuple[float, float, float]:
    if delta_t1_ns <= 0 or delta_t2_ns <= 0:
        raise ValueError("motion sample intervals must be positive")
    dt1 = delta_t1_ns / 1_000_000_000
    dt2 = delta_t2_ns / 1_000_000_000
    inverse_scale = 1.0 / coordinate_scale
    velocity_t1_x = (
        (request.point_t1_x - request.point_t0_x) * inverse_scale / dt1
    )
    velocity_t1_y = (
        (request.point_t1_y - request.point_t0_y) * inverse_scale / dt1
    )
    velocity_t2_x = (
        (request.point_t2_x - request.point_t1_x) * inverse_scale / dt2
    )
    velocity_t2_y = (
        (request.point_t2_y - request.point_t1_y) * inverse_scale / dt2
    )
    interval_sum = dt1 + dt2
    acceleration_x = 2.0 * (velocity_t2_x - velocity_t1_x) / interval_sum
    acceleration_y = 2.0 * (velocity_t2_y - velocity_t1_y) / interval_sum
    return (
        sum((velocity_t1_x * velocity_t1_x, velocity_t1_y * velocity_t1_y)),
        sum((velocity_t2_x * velocity_t2_x, velocity_t2_y * velocity_t2_y)),
        sum((acceleration_x * acceleration_x, acceleration_y * acceleration_y)),
    )


__all__ = [
    "MotionFeature",
    "MotionStatsAggregator",
    "MotionWindowStats",
    "TemporalMotionWindow",
]
