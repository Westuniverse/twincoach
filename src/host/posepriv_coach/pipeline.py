"""Synchronous host pipeline joining capture, pose inference and exercise session."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from time import monotonic_ns
from typing import Any, Callable, Protocol

from .capture import CapturedFrame, LatestFrameScheduler
from .contracts import CameraView, PoseFrame
from .movenet_preprocess import MoveNetInference
from .pose_adapter import ResizeWithPadTransform, decode_movenet_singlepose
from .session import SquatSessionProcessor, SquatSessionUpdate
from .sync import DualViewSyncStats


class PoseEstimator(Protocol):
    """Replaceable host or board pose-inference boundary."""

    def infer(self, frame: CapturedFrame[Any]) -> PoseFrame: ...


class PoseFramePassthroughEstimator:
    """Use a recorded PoseFrame payload while preserving the normal pipeline."""

    def infer(self, frame: CapturedFrame[Any]) -> PoseFrame:
        pose = frame.payload
        if not isinstance(pose, PoseFrame):
            raise TypeError("pose replay payload must be a PoseFrame")
        return pose


@dataclass(frozen=True, slots=True)
class MoveNetPoseEstimator:
    """Decode MoveNet output and undo its standard square padded input space."""

    run_model: Callable[[CapturedFrame[Any]], Any]
    model_width: int = 192
    model_height: int = 192
    output_uses_resize_with_pad: bool = True

    def infer(self, frame: CapturedFrame[Any]) -> PoseFrame:
        result = self.run_model(frame)
        if isinstance(result, MoveNetInference):
            output = result.output
            transform = result.transform
        elif self.output_uses_resize_with_pad:
            output = result
            transform = ResizeWithPadTransform(
                source_width=frame.width,
                source_height=frame.height,
                target_width=self.model_width,
                target_height=self.model_height,
            )
        else:
            output = result
            transform = None
        return decode_movenet_singlepose(
            output,
            frame,
            resize_transform=transform,
        )


@dataclass(frozen=True, slots=True)
class PipelineTiming:
    inference_ns: int
    session_ns: int
    end_to_end_ns: int


@dataclass(frozen=True, slots=True)
class PipelineStep:
    captured: CapturedFrame[Any]
    pose: PoseFrame
    session_update: SquatSessionUpdate | None
    timing: PipelineTiming


@dataclass(frozen=True, slots=True)
class PipelineStats:
    processed_frames: int
    processed_front_frames: int
    processed_side_frames: int
    synchronized_updates: int
    dropped_front: int
    dropped_side: int
    total_inference_ns: int
    total_session_ns: int
    total_end_to_end_ns: int
    max_end_to_end_ns: int
    measurement_window_ns: int
    pose_sync: DualViewSyncStats
    latency_window_size: int = 0
    latency_sample_count: int = 0
    p95_inference_ns: int | None = None
    p95_session_ns: int | None = None
    p95_end_to_end_ns: int | None = None
    total_front_inference_ns: int = 0
    total_side_inference_ns: int = 0
    p95_front_inference_ns: int | None = None
    p95_side_inference_ns: int | None = None

    @property
    def mean_inference_ms(self) -> float | None:
        return self._mean_ms(self.total_inference_ns)

    @property
    def mean_session_ms(self) -> float | None:
        return self._mean_ms(self.total_session_ns)

    @property
    def mean_end_to_end_ms(self) -> float | None:
        return self._mean_ms(self.total_end_to_end_ns)

    @property
    def p95_inference_ms(self) -> float | None:
        return self._optional_ms(self.p95_inference_ns)

    @property
    def p95_session_ms(self) -> float | None:
        return self._optional_ms(self.p95_session_ns)

    @property
    def p95_end_to_end_ms(self) -> float | None:
        return self._optional_ms(self.p95_end_to_end_ns)

    @property
    def mean_front_inference_ms(self) -> float | None:
        return self._view_mean_ms(
            self.total_front_inference_ns,
            self.processed_front_frames,
        )

    @property
    def mean_side_inference_ms(self) -> float | None:
        return self._view_mean_ms(
            self.total_side_inference_ns,
            self.processed_side_frames,
        )

    @property
    def p95_front_inference_ms(self) -> float | None:
        return self._optional_ms(self.p95_front_inference_ns)

    @property
    def p95_side_inference_ms(self) -> float | None:
        return self._optional_ms(self.p95_side_inference_ns)

    @property
    def observed_processing_fps(self) -> float | None:
        return self._rate(self.processed_frames)

    @property
    def observed_front_fps(self) -> float | None:
        return self._rate(self.processed_front_frames)

    @property
    def observed_side_fps(self) -> float | None:
        return self._rate(self.processed_side_frames)

    @property
    def observed_synchronized_pair_fps(self) -> float | None:
        return self._rate(self.synchronized_updates)

    def _rate(self, count: int) -> float | None:
        if self.measurement_window_ns <= 0:
            return None
        return count * 1_000_000_000 / self.measurement_window_ns

    def _mean_ms(self, total_ns: int) -> float | None:
        if self.processed_frames == 0:
            return None
        return total_ns / self.processed_frames / 1_000_000

    @staticmethod
    def _view_mean_ms(total_ns: int, frames: int) -> float | None:
        return None if frames == 0 else total_ns / frames / 1_000_000

    @staticmethod
    def _optional_ms(value_ns: int | None) -> float | None:
        return None if value_ns is None else value_ns / 1_000_000


class DualViewCoachPipeline:
    """Process the newest fair-scheduled frame without accumulating latency."""

    def __init__(
        self,
        estimator: PoseEstimator,
        session: SquatSessionProcessor,
        *,
        clock_ns: Callable[[], int] = monotonic_ns,
        latency_window_size: int = 1024,
    ) -> None:
        if latency_window_size <= 0:
            raise ValueError("latency_window_size must be positive")
        self.estimator = estimator
        self.session = session
        self.scheduler: LatestFrameScheduler[Any] = LatestFrameScheduler()
        self._clock_ns = clock_ns
        self._latency_window_size = latency_window_size
        self._inference_samples_ns: deque[int] = deque(maxlen=latency_window_size)
        self._front_inference_samples_ns: deque[int] = deque(maxlen=latency_window_size)
        self._side_inference_samples_ns: deque[int] = deque(maxlen=latency_window_size)
        self._session_samples_ns: deque[int] = deque(maxlen=latency_window_size)
        self._end_to_end_samples_ns: deque[int] = deque(maxlen=latency_window_size)
        self._processed_frames = 0
        self._processed_front_frames = 0
        self._processed_side_frames = 0
        self._synchronized_updates = 0
        self._total_inference_ns = 0
        self._total_front_inference_ns = 0
        self._total_side_inference_ns = 0
        self._total_session_ns = 0
        self._total_end_to_end_ns = 0
        self._max_end_to_end_ns = 0
        self._measurement_start_ns: int | None = None
        self._measurement_end_ns: int | None = None

    def submit(self, frame: CapturedFrame[Any]) -> None:
        self.scheduler.push(frame)

    def process_next(self) -> PipelineStep | None:
        captured = self.scheduler.pop_next()
        if captured is None:
            return None
        inference_start_ns = self._clock_ns()
        pose = self.estimator.infer(captured)
        inference_end_ns = self._clock_ns()
        expected_identity = (
            captured.view,
            captured.frame_id,
            captured.sync_timestamp_ns,
            captured.width,
            captured.height,
        )
        actual_identity = (
            pose.view,
            pose.frame_id,
            pose.timestamp_ns,
            pose.source_width,
            pose.source_height,
        )
        if actual_identity != expected_identity:
            raise ValueError("pose estimator did not preserve capture identity and dimensions")
        session_update = self.session.push(pose)
        session_end_ns = self._clock_ns()
        timing = PipelineTiming(
            inference_ns=inference_end_ns - inference_start_ns,
            session_ns=session_end_ns - inference_end_ns,
            end_to_end_ns=session_end_ns - captured.timestamp_ns,
        )
        if min(timing.inference_ns, timing.session_ns, timing.end_to_end_ns) < 0:
            raise ValueError("pipeline clock must share the capture monotonic time domain")
        self._record_timing(
            timing,
            inference_start_ns,
            session_end_ns,
            captured.view,
            session_update,
        )
        return PipelineStep(
            captured=captured,
            pose=pose,
            session_update=session_update,
            timing=timing,
        )

    def dropped_captures(self, view: CameraView) -> int:
        return self.scheduler.dropped(view)

    def stats(self) -> PipelineStats:
        measurement_window_ns = (
            self._measurement_end_ns - self._measurement_start_ns
            if self._measurement_start_ns is not None
            and self._measurement_end_ns is not None
            else 0
        )
        return PipelineStats(
            processed_frames=self._processed_frames,
            processed_front_frames=self._processed_front_frames,
            processed_side_frames=self._processed_side_frames,
            synchronized_updates=self._synchronized_updates,
            dropped_front=self.scheduler.dropped(CameraView.FRONT),
            dropped_side=self.scheduler.dropped(CameraView.SIDE),
            total_inference_ns=self._total_inference_ns,
            total_session_ns=self._total_session_ns,
            total_end_to_end_ns=self._total_end_to_end_ns,
            max_end_to_end_ns=self._max_end_to_end_ns,
            measurement_window_ns=measurement_window_ns,
            pose_sync=self.session.synchronizer.stats(),
            latency_window_size=self._latency_window_size,
            latency_sample_count=len(self._end_to_end_samples_ns),
            p95_inference_ns=_p95_nearest_rank(self._inference_samples_ns),
            p95_session_ns=_p95_nearest_rank(self._session_samples_ns),
            p95_end_to_end_ns=_p95_nearest_rank(self._end_to_end_samples_ns),
            total_front_inference_ns=self._total_front_inference_ns,
            total_side_inference_ns=self._total_side_inference_ns,
            p95_front_inference_ns=_p95_nearest_rank(
                self._front_inference_samples_ns
            ),
            p95_side_inference_ns=_p95_nearest_rank(
                self._side_inference_samples_ns
            ),
        )

    def _record_timing(
        self,
        timing: PipelineTiming,
        start_ns: int,
        end_ns: int,
        view: CameraView,
        session_update: SquatSessionUpdate | None,
    ) -> None:
        if self._measurement_start_ns is None:
            self._measurement_start_ns = start_ns
        self._measurement_end_ns = end_ns
        self._processed_frames += 1
        if view is CameraView.FRONT:
            self._processed_front_frames += 1
            self._total_front_inference_ns += timing.inference_ns
            self._front_inference_samples_ns.append(timing.inference_ns)
        else:
            self._processed_side_frames += 1
            self._total_side_inference_ns += timing.inference_ns
            self._side_inference_samples_ns.append(timing.inference_ns)
        if session_update is not None:
            self._synchronized_updates += 1
        self._total_inference_ns += timing.inference_ns
        self._total_session_ns += timing.session_ns
        self._total_end_to_end_ns += timing.end_to_end_ns
        self._max_end_to_end_ns = max(self._max_end_to_end_ns, timing.end_to_end_ns)
        self._inference_samples_ns.append(timing.inference_ns)
        self._session_samples_ns.append(timing.session_ns)
        self._end_to_end_samples_ns.append(timing.end_to_end_ns)


def _p95_nearest_rank(samples: deque[int]) -> int | None:
    """Return the nearest-rank P95 for the bounded recent-sample window."""

    if not samples:
        return None
    ordered = sorted(samples)
    rank = (95 * len(ordered) + 99) // 100
    return ordered[rank - 1]
