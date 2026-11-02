"""Model-free dual-camera timing probe built on the production capture path."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any

from .capture import CapturePumpStats, CapturedFrame, LatestFrameScheduler
from .contracts import CameraView, PoseFrame
from .sync import DualViewSynchronizer


@dataclass(frozen=True, slots=True)
class CameraProbeResult:
    elapsed_seconds: float
    captured_front_frames: int
    captured_side_frames: int
    processed_front_frames: int
    processed_side_frames: int
    scheduler_dropped_front: int
    scheduler_dropped_side: int
    observed_front_modes: tuple[str, ...]
    observed_side_modes: tuple[str, ...]
    capture_front_fps: float
    capture_side_fps: float
    processed_front_fps: float
    processed_side_fps: float
    front_interval_sample_count: int
    side_interval_sample_count: int
    mean_front_interval_ms: float | None
    mean_side_interval_ms: float | None
    p95_front_interval_ms: float | None
    p95_side_interval_ms: float | None
    max_front_interval_ms: float | None
    max_side_interval_ms: float | None
    synchronized_pair_fps: float
    paired_frames: int
    sync_dropped_front: int
    sync_dropped_side: int
    sync_queue_overflow_front: int
    sync_queue_overflow_side: int
    sync_skew_rejected_front: int
    sync_skew_rejected_side: int
    skew_sample_count: int
    mean_skew_ms: float | None
    p95_skew_ms: float | None
    max_skew_ms: float | None
    capture_failed: bool

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = asdict(self)
        record.update(
            {
                "schema_version": 1,
                "probe": "dual_camera_capture",
                "timestamp_semantics": (
                    "host_monotonic_after_read_with_optional_configured_sync_offset"
                ),
                "contains_raw_pixels": False,
                "includes_pose_inference": False,
            }
        )
        return record


@dataclass(frozen=True, slots=True)
class CameraProbeAcceptance:
    minimum_capture_fps: float
    minimum_synchronized_pair_fps: float
    maximum_p95_skew_ms: float
    required_mode: str
    passed: bool
    failures_zh: tuple[str, ...]

    def to_record(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CameraProbeFailureDiagnosis:
    """Classify a failed child-process probe without treating it as success."""

    code: str
    summary_zh: str
    actions_zh: tuple[str, ...]

    def to_record(self) -> dict[str, object]:
        return {
            "code": self.code,
            "summary_zh": self.summary_zh,
            "actions_zh": list(self.actions_zh),
        }


def diagnose_camera_probe_failure(text: str) -> CameraProbeFailureDiagnosis | None:
    """Turn a V4L2/OpenCV probe log into actionable, non-gating evidence.

    The classifier is intentionally small and only changes the explanation of
    a failed attempt.  It never converts a failed probe into a pass and does
    not inspect or persist camera pixels.
    """

    if not text or not text.strip():
        return None
    normalized = text.casefold()
    if "no space left on device" in normalized or "vidioc_streamon" in normalized:
        return CameraProbeFailureDiagnosis(
            code="usb_isochronous_bandwidth",
            summary_zh=(
                "摄像头 STREAMON 申请的 USB 等时传输资源不足；这不是磁盘或 DDR 空间不足。"
            ),
            actions_zh=(
                "检查 lsusb -t，确认两路摄像头是否共享同一个 USB 根总线。",
                "共享根总线时先使用 1920x1080@15 或 1280x720@30 MJPG。",
                "若仍需双路 1080p30，改用不同 USB 主控制器或 CSI-2 输入。",
            ),
        )
    if "permission denied" in normalized or "operation not permitted" in normalized:
        return CameraProbeFailureDiagnosis(
            code="camera_permission",
            summary_zh="摄像头节点存在，但当前进程没有打开或配置它的权限。",
            actions_zh=(
                "确认设备路径属于 video 组并检查当前用户权限。",
                "优先使用 /dev/v4l/by-id 或 /dev/v4l/by-path，而不是猜测 video 编号。",
            ),
        )
    if "could not open" in normalized or "failed to open" in normalized:
        return CameraProbeFailureDiagnosis(
            code="camera_open",
            summary_zh="摄像头节点无法打开，通常是路径、占用进程或驱动状态问题。",
            actions_zh=(
                "用 v4l2-ctl --list-devices 核对实际节点。",
                "关闭占用摄像头的预览程序后再做一次单路探测。",
            ),
        )
    return CameraProbeFailureDiagnosis(
        code="capture_runtime",
        summary_zh="采集探针运行失败，日志没有匹配到已知的 USB 带宽或权限模式。",
        actions_zh=(
            "保留本次日志和请求模式，先做单路采集，再恢复双路。",
            "不要把失败尝试当成端到端验收通过。",
        ),
    )


def evaluate_camera_probe_acceptance(
    result: CameraProbeResult,
    *,
    minimum_capture_fps: float,
    minimum_synchronized_pair_fps: float,
    maximum_p95_skew_ms: float,
    required_mode: str,
) -> CameraProbeAcceptance:
    """Apply explicit capture-only MVP thresholds to one measured result."""

    thresholds = (
        minimum_capture_fps,
        minimum_synchronized_pair_fps,
        maximum_p95_skew_ms,
    )
    if any(not math.isfinite(value) or value < 0.0 for value in thresholds):
        raise ValueError("camera acceptance thresholds must be finite and non-negative")
    if not required_mode:
        raise ValueError("required_mode must not be empty")

    failures = []
    if result.capture_failed:
        failures.append("采集线程报告故障")
    if result.capture_front_fps < minimum_capture_fps:
        failures.append(
            f"正面采集 {result.capture_front_fps:.2f} FPS 低于 "
            f"{minimum_capture_fps:.2f}"
        )
    if result.capture_side_fps < minimum_capture_fps:
        failures.append(
            f"侧面采集 {result.capture_side_fps:.2f} FPS 低于 "
            f"{minimum_capture_fps:.2f}"
        )
    if result.synchronized_pair_fps < minimum_synchronized_pair_fps:
        failures.append(
            f"同步 {result.synchronized_pair_fps:.2f} 对/秒低于 "
            f"{minimum_synchronized_pair_fps:.2f}"
        )
    if result.p95_skew_ms is None:
        failures.append("没有形成同步对，无法计算 P95 时间偏差")
    elif result.p95_skew_ms > maximum_p95_skew_ms:
        failures.append(
            f"P95 时间偏差 {result.p95_skew_ms:.2f} ms 超过 "
            f"{maximum_p95_skew_ms:.2f} ms"
        )
    if required_mode not in result.observed_front_modes:
        failures.append(f"正面未实际返回请求模式 {required_mode}")
    if required_mode not in result.observed_side_modes:
        failures.append(f"侧面未实际返回请求模式 {required_mode}")

    return CameraProbeAcceptance(
        minimum_capture_fps=minimum_capture_fps,
        minimum_synchronized_pair_fps=minimum_synchronized_pair_fps,
        maximum_p95_skew_ms=maximum_p95_skew_ms,
        required_mode=required_mode,
        passed=not failures,
        failures_zh=tuple(failures),
    )


class DualCameraTimingProbe:
    """Consume latest frames on one thread and measure capture-only pairing."""

    def __init__(self, *, max_skew_ms: float = 50.0) -> None:
        self.scheduler: LatestFrameScheduler[Any] = LatestFrameScheduler()
        self.synchronizer = DualViewSynchronizer(max_skew_ms=max_skew_ms)
        self._processed = {CameraView.FRONT: 0, CameraView.SIDE: 0}
        self._modes: dict[CameraView, set[str]] = {
            CameraView.FRONT: set(),
            CameraView.SIDE: set(),
        }
        self._last_capture_timestamp_ns: dict[CameraView, int] = {}
        self._capture_intervals_ns: dict[CameraView, list[int]] = {
            CameraView.FRONT: [],
            CameraView.SIDE: [],
        }

    def submit(self, frame: CapturedFrame[Any]) -> None:
        previous = self._last_capture_timestamp_ns.get(frame.view)
        if previous is not None:
            interval = frame.timestamp_ns - previous
            if interval < 0:
                raise ValueError(f"non-monotonic {frame.view.value} capture timestamp")
            self._capture_intervals_ns[frame.view].append(interval)
        self._last_capture_timestamp_ns[frame.view] = frame.timestamp_ns
        self.scheduler.push(frame)

    def drain(self) -> int:
        processed = 0
        while True:
            frame = self.scheduler.pop_next()
            if frame is None:
                return processed
            self._processed[frame.view] += 1
            self._modes[frame.view].add(
                f"{frame.width}x{frame.height}:{frame.pixel_format}"
            )
            self.synchronizer.push(
                PoseFrame(
                    view=frame.view,
                    frame_id=frame.frame_id,
                    timestamp_ns=frame.sync_timestamp_ns,
                    keypoints={},
                )
            )
            processed += 1

    def result(
        self,
        capture: CapturePumpStats,
        *,
        elapsed_seconds: float,
    ) -> CameraProbeResult:
        if elapsed_seconds <= 0.0:
            raise ValueError("camera probe elapsed_seconds must be positive")
        sync = self.synchronizer.stats()
        front_processed = self._processed[CameraView.FRONT]
        side_processed = self._processed[CameraView.SIDE]
        front_intervals = self._capture_intervals_ns[CameraView.FRONT]
        side_intervals = self._capture_intervals_ns[CameraView.SIDE]
        return CameraProbeResult(
            elapsed_seconds=elapsed_seconds,
            captured_front_frames=capture.front_frames,
            captured_side_frames=capture.side_frames,
            processed_front_frames=front_processed,
            processed_side_frames=side_processed,
            scheduler_dropped_front=self.scheduler.dropped(CameraView.FRONT),
            scheduler_dropped_side=self.scheduler.dropped(CameraView.SIDE),
            observed_front_modes=tuple(sorted(self._modes[CameraView.FRONT])),
            observed_side_modes=tuple(sorted(self._modes[CameraView.SIDE])),
            capture_front_fps=capture.front_frames / elapsed_seconds,
            capture_side_fps=capture.side_frames / elapsed_seconds,
            processed_front_fps=front_processed / elapsed_seconds,
            processed_side_fps=side_processed / elapsed_seconds,
            front_interval_sample_count=len(front_intervals),
            side_interval_sample_count=len(side_intervals),
            mean_front_interval_ms=_mean_ms(front_intervals),
            mean_side_interval_ms=_mean_ms(side_intervals),
            p95_front_interval_ms=_p95_ms(front_intervals),
            p95_side_interval_ms=_p95_ms(side_intervals),
            max_front_interval_ms=_max_ms(front_intervals),
            max_side_interval_ms=_max_ms(side_intervals),
            synchronized_pair_fps=sync.paired_frames / elapsed_seconds,
            paired_frames=sync.paired_frames,
            sync_dropped_front=sync.dropped_front,
            sync_dropped_side=sync.dropped_side,
            sync_queue_overflow_front=sync.queue_overflow_front,
            sync_queue_overflow_side=sync.queue_overflow_side,
            sync_skew_rejected_front=sync.skew_rejected_front,
            sync_skew_rejected_side=sync.skew_rejected_side,
            skew_sample_count=sync.skew_sample_count,
            mean_skew_ms=sync.mean_skew_ms,
            p95_skew_ms=sync.p95_skew_ms,
            max_skew_ms=sync.max_skew_ms,
            capture_failed=capture.failed,
        )


def _mean_ms(samples_ns: list[int]) -> float | None:
    return None if not samples_ns else sum(samples_ns) / len(samples_ns) / 1_000_000


def _p95_ms(samples_ns: list[int]) -> float | None:
    if not samples_ns:
        return None
    ordered = sorted(samples_ns)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return ordered[rank - 1] / 1_000_000


def _max_ms(samples_ns: list[int]) -> float | None:
    return None if not samples_ns else max(samples_ns) / 1_000_000
