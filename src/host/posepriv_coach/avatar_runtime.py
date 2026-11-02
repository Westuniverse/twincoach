"""把同步三维姿态帧接到蒙皮后端和显示状态的薄运行时。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import math
from threading import Condition, Thread
from time import monotonic_ns, perf_counter_ns
from typing import Callable

from .avatar_display import AvatarDisplayModel
from .avatar_pipeline import AvatarFramePlan, AvatarFramePlanner, AvatarFrameResult
from .pose_avatar_preview import PoseAvatarPreviewPlanner
from .avatar_skinning import (
    MATRIX_COUNT,
    AffineMatrixQ1_14,
    MatrixWrite,
    VertexSkinningBackend,
    VertexSkinningRequest,
)
from .contracts import SyncedPosePair
from .mesh_asset import AvatarMeshAsset
from .joint_filter import JointFilterStats
from .rectified_backend import RectifiedStereoBackendStats
from .session import SquatSessionUpdate


@dataclass(frozen=True, slots=True)
class AvatarRuntimeTiming:
    """一帧数字人路径的分段单调时钟耗时，单位为纳秒。"""

    plan_ns: int
    skinning_ns: int
    display_ns: int
    total_ns: int

    @property
    def total_ms(self) -> float:
        return self.total_ns / 1_000_000


@dataclass(frozen=True, slots=True)
class AvatarRuntimeStats:
    """数字人路径累计耗时摘要，不包含摄像头和模型冷启动。"""

    frames: int
    total_plan_ns: int
    total_skinning_ns: int
    total_display_ns: int
    total_ns: int
    max_total_ns: int
    total_vertex_count: int = 0
    total_packet_tx_bytes: int = 0
    total_packet_rx_bytes: int = 0
    joint_filter: JointFilterStats | None = None
    latency_window_size: int = 0
    latency_sample_count: int = 0
    p95_plan_ns: int | None = None
    p95_skinning_ns: int | None = None
    p95_display_ns: int | None = None
    p95_total_ns: int | None = None
    held_matrix_uses: int = 0
    expired_matrix_uses: int = 0
    triangulated_point_count: int = 0
    valid_triangulated_point_count: int = 0
    finite_ray_gap_count: int = 0
    total_ray_gap_world_units: float = 0.0
    p95_ray_gap_world_units: float | None = None
    ray_angle_sample_count: int = 0
    total_ray_angle_degrees: float = 0.0
    p05_ray_angle_degrees: float | None = None
    triangulation_geometry_backend: str | None = None
    triangulation_accelerator_backend: str | None = None
    triangulation_backend_stats: RectifiedStereoBackendStats | None = None

    @property
    def mean_total_ms(self) -> float | None:
        return None if self.frames == 0 else self.total_ns / self.frames / 1_000_000

    @property
    def p95_plan_ms(self) -> float | None:
        return _optional_ms(self.p95_plan_ns)

    @property
    def p95_skinning_ms(self) -> float | None:
        return _optional_ms(self.p95_skinning_ns)

    @property
    def p95_display_ms(self) -> float | None:
        return _optional_ms(self.p95_display_ns)

    @property
    def p95_total_ms(self) -> float | None:
        return _optional_ms(self.p95_total_ns)

    @property
    def valid_triangulated_percent(self) -> float | None:
        if self.triangulated_point_count == 0:
            return None
        return 100.0 * self.valid_triangulated_point_count / self.triangulated_point_count

    @property
    def finite_ray_gap_percent(self) -> float | None:
        """全部候选点中能形成非平行、有限最近距离射线对的比例。"""

        if self.triangulated_point_count == 0:
            return None
        return 100.0 * self.finite_ray_gap_count / self.triangulated_point_count

    @property
    def accepted_finite_ray_percent(self) -> float | None:
        """有限射线对中通过最大射线间距阈值的比例。"""

        if self.finite_ray_gap_count == 0:
            return None
        return 100.0 * self.valid_triangulated_point_count / self.finite_ray_gap_count

    @property
    def mean_ray_gap_world_units(self) -> float | None:
        if self.finite_ray_gap_count == 0:
            return None
        return self.total_ray_gap_world_units / self.finite_ray_gap_count

    @property
    def mean_ray_angle_degrees(self) -> float | None:
        if self.ray_angle_sample_count == 0:
            return None
        return self.total_ray_angle_degrees / self.ray_angle_sample_count


@dataclass(frozen=True, slots=True)
class AvatarDispatchStats:
    """数字人异步最新帧调度摘要。"""

    submitted_pairs: int
    processed_pairs: int
    dropped_pairs: int
    failed: bool
    measurement_window_ns: int = 0
    total_content_age_ns: int = 0
    max_content_age_ns: int = 0
    latency_window_size: int = 1024
    content_age_pairs: int = 0
    content_age_sample_count: int = 0
    p95_content_age_ns: int | None = None
    invalid_content_age_samples: int = 0
    missing_content_timestamp_pairs: int = 0

    @property
    def observed_process_fps(self) -> float | None:
        if self.measurement_window_ns <= 0 or self.processed_pairs <= 0:
            return None
        return self.processed_pairs * 1_000_000_000 / self.measurement_window_ns

    @property
    def mean_content_age_ms(self) -> float | None:
        if self.content_age_pairs <= 0:
            return None
        return self.total_content_age_ns / self.content_age_pairs / 1_000_000

    @property
    def max_content_age_ms(self) -> float | None:
        if self.content_age_pairs <= 0:
            return None
        return self.max_content_age_ns / 1_000_000

    @property
    def p95_content_age_ms(self) -> float | None:
        return _optional_ms(self.p95_content_age_ns)


class LatestFrameAvatarDispatcher:
    """在数字人后端较慢时只保留最新同步对，避免主循环堆积旧帧。

    该调度器只改变数字人显示/蒙皮支路，不改变动作评分使用的
    ``SyncedPosePair``。默认入口不启用；启用后关闭时会等待最后一对已提交姿态完成，
    便于把完整统计写入报告。
    """

    def __init__(
        self,
        runtime: "AvatarRuntime",
        *,
        thread_name: str = "posepriv-avatar",
        latency_window_size: int = 1024,
        clock_ns: Callable[[], int] = monotonic_ns,
    ) -> None:
        if latency_window_size < 1:
            raise ValueError("avatar dispatch latency_window_size must be positive")
        self.runtime = runtime
        self._latency_window_size = latency_window_size
        self._clock_ns = clock_ns
        self._condition = Condition()
        self._latest_pair: SyncedPosePair | None = None
        self._stopping = False
        self._closed = False
        self._failure: BaseException | None = None
        self._submitted_pairs = 0
        self._processed_pairs = 0
        self._dropped_pairs = 0
        self._measurement_start_ns: int | None = None
        self._measurement_end_ns: int | None = None
        self._total_content_age_ns = 0
        self._max_content_age_ns = 0
        self._content_age_pairs = 0
        self._content_age_samples_ns: deque[int] = deque(maxlen=latency_window_size)
        self._invalid_content_age_samples = 0
        self._missing_content_timestamp_pairs = 0
        self._thread = Thread(target=self._run, name=thread_name, daemon=True)
        self._thread.start()

    def __call__(self, pair: SyncedPosePair) -> None:
        self.submit(pair)

    def submit(self, pair: SyncedPosePair) -> None:
        """提交一对同步姿态；若工作线程忙，只覆盖尚未开始的一对。"""

        submitted_ns = self._clock_ns()
        with self._condition:
            if self._closed or self._stopping:
                raise RuntimeError("avatar dispatcher is closed")
            if self._failure is not None:
                raise RuntimeError("avatar dispatcher failed") from self._failure
            if (
                self._measurement_start_ns is not None
                and submitted_ns < self._measurement_start_ns
            ):
                raise RuntimeError("avatar dispatch clock must be monotonic")
            self._submitted_pairs += 1
            if self._latest_pair is not None:
                self._dropped_pairs += 1
            self._latest_pair = pair
            if self._measurement_start_ns is None:
                self._measurement_start_ns = submitted_ns
            self._condition.notify()

    def close(self, *, drain: bool = True) -> None:
        """停止工作线程；默认处理完最后一对未开始的同步姿态。"""

        with self._condition:
            if self._closed:
                failure = self._failure
            else:
                self._stopping = True
                if not drain and self._latest_pair is not None:
                    self._latest_pair = None
                    self._dropped_pairs += 1
                self._condition.notify_all()
                failure = None
        if not self._closed:
            self._thread.join()
            with self._condition:
                self._closed = True
                failure = self._failure
        if failure is not None:
            raise RuntimeError("avatar dispatcher worker failed") from failure

    def raise_if_failed(self) -> None:
        with self._condition:
            failure = self._failure
        if failure is not None:
            raise RuntimeError("avatar dispatcher worker failed") from failure

    def stats(self) -> AvatarDispatchStats:
        with self._condition:
            measurement_window_ns = (
                self._measurement_end_ns - self._measurement_start_ns
                if self._measurement_start_ns is not None
                and self._measurement_end_ns is not None
                else 0
            )
            return AvatarDispatchStats(
                submitted_pairs=self._submitted_pairs,
                processed_pairs=self._processed_pairs,
                dropped_pairs=self._dropped_pairs,
                failed=self._failure is not None,
                measurement_window_ns=measurement_window_ns,
                total_content_age_ns=self._total_content_age_ns,
                max_content_age_ns=self._max_content_age_ns,
                latency_window_size=self._latency_window_size,
                content_age_pairs=self._content_age_pairs,
                content_age_sample_count=len(self._content_age_samples_ns),
                p95_content_age_ns=_p95_nearest_rank(
                    self._content_age_samples_ns
                ),
                invalid_content_age_samples=self._invalid_content_age_samples,
                missing_content_timestamp_pairs=(
                    self._missing_content_timestamp_pairs
                ),
            )

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._latest_pair is None and not self._stopping:
                    self._condition.wait()
                if self._latest_pair is None and self._stopping:
                    return
                pair = self._latest_pair
                self._latest_pair = None
            assert pair is not None
            try:
                self.runtime.process_pair(pair)
                processed_ns = self._clock_ns()
            except BaseException as exc:
                with self._condition:
                    self._failure = exc
                    self._stopping = True
                    self._latest_pair = None
                    self._condition.notify_all()
                return
            with self._condition:
                if (
                    self._measurement_start_ns is not None
                    and processed_ns < self._measurement_start_ns
                ):
                    self._failure = RuntimeError(
                        "avatar dispatch clock must be monotonic"
                    )
                    self._stopping = True
                    self._latest_pair = None
                    self._condition.notify_all()
                    return
                self._processed_pairs += 1
                self._measurement_end_ns = processed_ns
                content_timestamp_ns = _pair_timestamp_ns(pair)
                if content_timestamp_ns is None:
                    self._missing_content_timestamp_pairs += 1
                else:
                    content_age_ns = processed_ns - content_timestamp_ns
                    if content_age_ns < 0:
                        self._invalid_content_age_samples += 1
                    else:
                        self._content_age_pairs += 1
                        self._total_content_age_ns += content_age_ns
                        self._max_content_age_ns = max(
                            self._max_content_age_ns,
                            content_age_ns,
                        )
                        self._content_age_samples_ns.append(content_age_ns)
                self._condition.notify_all()


@dataclass(frozen=True, slots=True)
class AvatarRuntimeFrame:
    """一帧蒙皮结果及其对应的三维显示模型引用。"""

    plan: AvatarFramePlan
    result: AvatarFrameResult
    timing: AvatarRuntimeTiming
    held_matrix_count: int = 0
    expired_matrix_count: int = 0


class AvatarRuntime:
    """执行一对同步姿态并更新可供 HDMI 消费的数字人状态。

    该类不拥有摄像头队列，也不重新计算动作评分。动作管线产生
    ``SquatSessionUpdate.pose_pair`` 后，应用层调用 ``process_update`` 即可。
    """

    def __init__(
        self,
        planner: AvatarFramePlanner | PoseAvatarPreviewPlanner,
        backend: VertexSkinningBackend,
        *,
        vertex_requests: tuple[VertexSkinningRequest, ...] = (),
        mesh_asset: AvatarMeshAsset | None = None,
        program_matrices: Callable[[tuple], None] | None = None,
        display: AvatarDisplayModel | None = None,
        clock_ns: Callable[[], int] = perf_counter_ns,
        latency_window_size: int = 1024,
        matrix_hold_frames: int = 6,
    ) -> None:
        if latency_window_size <= 0:
            raise ValueError("avatar latency_window_size must be positive")
        if matrix_hold_frames < 0:
            raise ValueError("avatar matrix_hold_frames must be non-negative")
        self.planner = planner
        self.backend = backend
        if mesh_asset is not None and vertex_requests:
            raise ValueError("provide mesh_asset or vertex_requests, not both")
        self.mesh_asset = mesh_asset
        self.bone_topology_indices: tuple[int, ...] = ()
        if mesh_asset is not None:
            configured_bones = tuple(link.name for link in planner.config.bone_links)
            configured_indices = {
                name: index for index, name in enumerate(configured_bones)
            }
            unknown_bones = tuple(
                name for name in mesh_asset.bone_names if name not in configured_indices
            )
            if unknown_bones:
                raise ValueError(
                    "mesh contains bones outside the configured topology: "
                    + ", ".join(unknown_bones)
                )
            self.bone_topology_indices = tuple(
                configured_indices[name] for name in mesh_asset.bone_names
            )
        self.vertex_requests = (
            mesh_asset.skinning_requests()
            if mesh_asset is not None
            else tuple(vertex_requests)
        )
        self.program_matrices = program_matrices
        self.display = display if display is not None else AvatarDisplayModel()
        self._clock_ns = clock_ns
        self._matrix_hold_frames = matrix_hold_frames
        self._last_valid_matrices: list[AffineMatrixQ1_14 | None] = [None] * MATRIX_COUNT
        self._matrix_invalid_ages = [0] * MATRIX_COUNT
        self.last_frame: AvatarRuntimeFrame | None = None
        self._frames = 0
        self._total_plan_ns = 0
        self._total_skinning_ns = 0
        self._total_display_ns = 0
        self._total_ns = 0
        self._max_total_ns = 0
        self._total_vertex_count = 0
        self._total_packet_tx_bytes = 0
        self._total_packet_rx_bytes = 0
        self._held_matrix_uses = 0
        self._expired_matrix_uses = 0
        self._triangulated_point_count = 0
        self._valid_triangulated_point_count = 0
        self._finite_ray_gap_count = 0
        self._total_ray_gap_world_units = 0.0
        self._ray_angle_sample_count = 0
        self._total_ray_angle_degrees = 0.0
        self._latency_window_size = latency_window_size
        self._plan_samples_ns: deque[int] = deque(maxlen=latency_window_size)
        self._skinning_samples_ns: deque[int] = deque(maxlen=latency_window_size)
        self._display_samples_ns: deque[int] = deque(maxlen=latency_window_size)
        self._total_samples_ns: deque[int] = deque(maxlen=latency_window_size)
        self._ray_gap_samples: deque[float] = deque(maxlen=latency_window_size)
        self._ray_angle_samples: deque[float] = deque(maxlen=latency_window_size)

    def process_pair(self, pair: SyncedPosePair) -> AvatarRuntimeFrame:
        """规划并执行一对同步姿态，随后刷新数字人显示状态。"""

        total_start_ns = self._clock_ns()
        plan_start_ns = total_start_ns
        plan = self.planner.build(
            pair,
            vertex_requests=self.vertex_requests,
            bone_bind_lengths_q1_14=(
                self.mesh_asset.bone_bind_lengths_q1_14
                if self.mesh_asset is not None
                else ()
            ),
            inverse_bind_matrices_q1_14=(
                self.mesh_asset.inverse_bind_matrices_q1_14
                if self.mesh_asset is not None
                else ()
            ),
            bone_topology_indices=self.bone_topology_indices,
        )
        plan, held_matrix_count, expired_matrix_count = self._hold_invalid_matrices(plan)
        plan_end_ns = self._clock_ns()
        programmer = self.program_matrices
        if programmer is None:
            candidate = getattr(self.backend, "program_matrices", None)
            if callable(candidate):
                programmer = candidate
        result = AvatarFrameResult(
            plan=plan,
            output_beats=plan.execute(self.backend, program_matrices=programmer),
        )
        skinning_end_ns = self._clock_ns()
        self.display.update(result, self.mesh_asset)
        display_end_ns = self._clock_ns()
        timing = AvatarRuntimeTiming(
            plan_ns=plan_end_ns - plan_start_ns,
            skinning_ns=skinning_end_ns - plan_end_ns,
            display_ns=display_end_ns - skinning_end_ns,
            total_ns=display_end_ns - total_start_ns,
        )
        if min(
            timing.plan_ns,
            timing.skinning_ns,
            timing.display_ns,
            timing.total_ns,
        ) < 0:
            raise ValueError("avatar runtime clock must be monotonic")
        self._frames += 1
        self._total_plan_ns += timing.plan_ns
        self._total_skinning_ns += timing.skinning_ns
        self._total_display_ns += timing.display_ns
        self._total_ns += timing.total_ns
        self._max_total_ns = max(self._max_total_ns, timing.total_ns)
        self._total_vertex_count += len(plan.vertex_requests)
        self._total_packet_tx_bytes += plan.packet_tx_bytes
        self._total_packet_rx_bytes += plan.packet_rx_bytes
        self._plan_samples_ns.append(timing.plan_ns)
        self._skinning_samples_ns.append(timing.skinning_ns)
        self._display_samples_ns.append(timing.display_ns)
        self._total_samples_ns.append(timing.total_ns)
        self._held_matrix_uses += held_matrix_count
        self._expired_matrix_uses += expired_matrix_count
        points = tuple(plan.points.values())
        self._triangulated_point_count += len(points)
        self._valid_triangulated_point_count += sum(point.valid for point in points)
        finite_ray_gaps = tuple(
            point.ray_gap for point in points if math.isfinite(point.ray_gap)
        )
        self._finite_ray_gap_count += len(finite_ray_gaps)
        self._total_ray_gap_world_units += sum(finite_ray_gaps)
        self._ray_gap_samples.extend(finite_ray_gaps)
        ray_angles = tuple(
            point.ray_angle_degrees
            for point in points
            if point.ray_angle_degrees > 0.0
        )
        self._ray_angle_sample_count += len(ray_angles)
        self._total_ray_angle_degrees += sum(ray_angles)
        self._ray_angle_samples.extend(ray_angles)
        frame = AvatarRuntimeFrame(
            plan=plan,
            result=result,
            timing=timing,
            held_matrix_count=held_matrix_count,
            expired_matrix_count=expired_matrix_count,
        )
        self.last_frame = frame
        return frame

    def process_update(self, update: SquatSessionUpdate) -> AvatarRuntimeFrame:
        """消费动作会话已经确认的同步姿态对。"""

        return self.process_pair(update.pose_pair)

    def stats(self) -> AvatarRuntimeStats:
        """返回当前数字人路径的累计耗时摘要。"""

        config = getattr(self.planner, "config", None)
        triangulation_backend = getattr(config, "rectified_backend", None)
        triangulation_stats = None
        stats_method = getattr(triangulation_backend, "stats", None)
        if callable(stats_method):
            triangulation_stats = stats_method()

        return AvatarRuntimeStats(
            frames=self._frames,
            total_plan_ns=self._total_plan_ns,
            total_skinning_ns=self._total_skinning_ns,
            total_display_ns=self._total_display_ns,
            total_ns=self._total_ns,
            max_total_ns=self._max_total_ns,
            total_vertex_count=self._total_vertex_count,
            total_packet_tx_bytes=self._total_packet_tx_bytes,
            total_packet_rx_bytes=self._total_packet_rx_bytes,
            joint_filter=(
                self.planner.joint_filter.stats()
                if self.planner.joint_filter is not None
                else None
            ),
            latency_window_size=self._latency_window_size,
            latency_sample_count=len(self._total_samples_ns),
            p95_plan_ns=_p95_nearest_rank(self._plan_samples_ns),
            p95_skinning_ns=_p95_nearest_rank(self._skinning_samples_ns),
            p95_display_ns=_p95_nearest_rank(self._display_samples_ns),
            p95_total_ns=_p95_nearest_rank(self._total_samples_ns),
            held_matrix_uses=self._held_matrix_uses,
            expired_matrix_uses=self._expired_matrix_uses,
            triangulated_point_count=self._triangulated_point_count,
            valid_triangulated_point_count=self._valid_triangulated_point_count,
            finite_ray_gap_count=self._finite_ray_gap_count,
            total_ray_gap_world_units=self._total_ray_gap_world_units,
            p95_ray_gap_world_units=_p95_nearest_rank(self._ray_gap_samples),
            ray_angle_sample_count=self._ray_angle_sample_count,
            total_ray_angle_degrees=self._total_ray_angle_degrees,
            p05_ray_angle_degrees=_p05_nearest_rank(self._ray_angle_samples),
            triangulation_geometry_backend=getattr(
                config, "triangulation_backend", None
            ),
            triangulation_accelerator_backend=(
                getattr(triangulation_backend, "backend_name", None)
                if triangulation_backend is not None
                else None
            ),
            triangulation_backend_stats=triangulation_stats,
        )

    def _hold_invalid_matrices(
        self,
        plan: AvatarFramePlan,
    ) -> tuple[AvatarFramePlan, int, int]:
        """短时复用上一有效矩阵，同时保留本帧原始有效位。"""

        active_count = (
            len(self.bone_topology_indices)
            if self.bone_topology_indices
            else min(len(plan.segments), MATRIX_COUNT)
        )
        writes = list(plan.matrix_writes)
        held = 0
        expired = 0
        for index in range(active_count):
            if plan.matrix_valid[index]:
                self._last_valid_matrices[index] = writes[index].matrix
                self._matrix_invalid_ages[index] = 0
                continue
            previous = self._last_valid_matrices[index]
            if previous is None:
                continue
            age = self._matrix_invalid_ages[index]
            if age < self._matrix_hold_frames:
                writes[index] = MatrixWrite(index, previous)
                self._matrix_invalid_ages[index] = age + 1
                held += 1
            else:
                expired += 1
        if held == 0:
            return plan, held, expired
        return replace(plan, matrix_writes=tuple(writes)), held, expired


def _pair_timestamp_ns(pair: object) -> int | None:
    if not isinstance(pair, SyncedPosePair):
        return None
    return max(pair.front.timestamp_ns, pair.side.timestamp_ns)


def _p95_nearest_rank(samples: deque[int] | deque[float]) -> int | float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    rank = (95 * len(ordered) + 99) // 100
    return ordered[rank - 1]


def _p05_nearest_rank(samples: deque[float]) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    rank = max(1, (5 * len(ordered) + 99) // 100)
    return ordered[rank - 1]


def _optional_ms(value_ns: int | None) -> float | None:
    return None if value_ns is None else value_ns / 1_000_000


__all__ = [
    "AvatarDispatchStats",
    "AvatarRuntime",
    "AvatarRuntimeFrame",
    "AvatarRuntimeStats",
    "AvatarRuntimeTiming",
    "LatestFrameAvatarDispatcher",
]
