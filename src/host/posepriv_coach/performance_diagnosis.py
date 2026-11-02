"""把会话摘要转换为可读的局部实时性诊断。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class StageBudgetAssessment:
    """一个运行阶段相对于局部帧预算的诊断结果。"""

    key: str
    label: str
    p95_ms: float | None
    budget_ms: float
    sample_count: int

    @property
    def utilization_percent(self) -> float | None:
        if self.p95_ms is None:
            return None
        return self.p95_ms / self.budget_ms * 100.0

    @property
    def status(self) -> str:
        if self.p95_ms is None or self.sample_count < 1:
            return "未测量"
        return "预算内" if self.p95_ms <= self.budget_ms else "超预算"


@dataclass(frozen=True, slots=True)
class SyncFlowObservation:
    """同步器已结算入口帧的成对利用率和分类丢帧账本。"""

    paired_frames: int | None
    dropped_front: int | None
    dropped_side: int | None
    queue_overflow_front: int | None
    queue_overflow_side: int | None
    skew_rejected_front: int | None
    skew_rejected_side: int | None
    superseded_front: int | None
    superseded_side: int | None

    @property
    def front_pairing_percent(self) -> float | None:
        return _paired_view_percent(self.paired_frames, self.dropped_front)

    @property
    def side_pairing_percent(self) -> float | None:
        return _paired_view_percent(self.paired_frames, self.dropped_side)

    @property
    def input_pairing_percent(self) -> float | None:
        """同步器已结算的两路帧中，最终参与成功配对的帧比例。"""

        paired = self.paired_frames
        dropped_front = self.dropped_front
        dropped_side = self.dropped_side
        if paired is None or dropped_front is None or dropped_side is None:
            return None
        total_input_frames = 2 * paired + dropped_front + dropped_side
        if total_input_frames == 0:
            return None
        return 2 * paired / total_input_frames * 100.0

    @property
    def drop_accounting_complete(self) -> bool | None:
        dropped_front = self.dropped_front
        dropped_side = self.dropped_side
        queue_overflow_front = self.queue_overflow_front
        queue_overflow_side = self.queue_overflow_side
        skew_rejected_front = self.skew_rejected_front
        skew_rejected_side = self.skew_rejected_side
        superseded_front = self.superseded_front
        superseded_side = self.superseded_side
        if (
            dropped_front is None
            or dropped_side is None
            or queue_overflow_front is None
            or queue_overflow_side is None
            or skew_rejected_front is None
            or skew_rejected_side is None
            or superseded_front is None
            or superseded_side is None
        ):
            return None
        return (
            dropped_front
            == queue_overflow_front + skew_rejected_front + superseded_front
            and dropped_side
            == queue_overflow_side + skew_rejected_side + superseded_side
        )


@dataclass(frozen=True, slots=True)
class DispatchFlowObservation:
    """latest-only 输出支路的提交、完成和覆盖丢弃账本。"""

    submitted: int | None
    completed: int | None
    dropped: int | None
    failed: bool | None

    @property
    def completion_percent(self) -> float | None:
        submitted = self.submitted
        completed = self.completed
        if submitted is None or completed is None or submitted == 0:
            return None
        return completed / submitted * 100.0

    @property
    def drop_accounting_complete(self) -> bool | None:
        submitted = self.submitted
        completed = self.completed
        dropped = self.dropped
        if submitted is None or completed is None or dropped is None:
            return None
        return submitted == completed + dropped


@dataclass(frozen=True, slots=True)
class SessionPerformanceDiagnosis:
    target_fps_per_camera: float
    stages: tuple[StageBudgetAssessment, ...]
    observed_pair_fps: float | None = None
    p95_end_to_end_ms: float | None = None
    avatar_vertices_per_frame: float | None = None
    avatar_vertices_per_second: float | None = None
    avatar_tx_mib_per_second: float | None = None
    avatar_rx_mib_per_second: float | None = None
    valid_triangulated_percent: float | None = None
    finite_ray_gap_percent: float | None = None
    p95_ray_gap_world_units: float | None = None
    p05_ray_angle_degrees: float | None = None
    held_matrices_per_frame: float | None = None
    expired_matrices_per_frame: float | None = None
    native_front_capture_fps: float | None = None
    native_side_capture_fps: float | None = None
    native_e2e_pair_fps: float | None = None
    native_pair_fps: float | None = None
    native_pipeline_capture: bool | None = None
    native_capture_prefetched_pairs: int | None = None
    native_post_distance_evaluated: int | None = None
    native_post_distance_pruned: int | None = None
    native_post_distance_prune_percent: float | None = None
    native_front_preprocess_avg_ms: float | None = None
    native_side_preprocess_avg_ms: float | None = None
    native_front_fourcc: str | None = None
    native_side_fourcc: str | None = None
    scheduler_dropped_front: int | None = None
    scheduler_dropped_side: int | None = None
    sync_flow: SyncFlowObservation | None = None
    avatar_dispatch: DispatchFlowObservation | None = None
    display_dispatch: DispatchFlowObservation | None = None
    avatar_observed_process_fps: float | None = None
    avatar_mean_content_age_ms: float | None = None
    avatar_p95_content_age_ms: float | None = None
    avatar_max_content_age_ms: float | None = None
    avatar_content_age_pairs: int | None = None
    avatar_content_age_sample_count: int | None = None
    avatar_latency_window_size: int | None = None
    avatar_invalid_content_age_samples: int | None = None
    avatar_missing_content_timestamp_pairs: int | None = None
    avatar_content_timestamp_semantics: str | None = None
    display_observed_render_fps: float | None = None
    display_mean_content_age_ms: float | None = None
    display_p95_content_age_ms: float | None = None
    display_max_content_age_ms: float | None = None
    display_content_age_frames: int | None = None
    display_content_age_sample_count: int | None = None
    display_latency_window_size: int | None = None
    display_invalid_content_age_samples: int | None = None
    display_missing_content_timestamp_frames: int | None = None
    display_content_timestamp_semantics: str | None = None

    @property
    def target_pair_period_ms(self) -> float:
        return 1000.0 / self.target_fps_per_camera

    @property
    def pair_fps_target_percent(self) -> float | None:
        if self.observed_pair_fps is None:
            return None
        return self.observed_pair_fps / self.target_fps_per_camera * 100.0

    @property
    def native_capture_fps_target_percent(self) -> float | None:
        """原生 VART 两路取帧中较慢一路相对目标的比例。"""

        observed = [
            value
            for value in (
                self.native_front_capture_fps,
                self.native_side_capture_fps,
            )
            if value is not None
        ]
        if not observed:
            return None
        return min(observed) / self.target_fps_per_camera * 100.0

    @property
    def native_capture_is_input_limited(self) -> bool:
        """只根据实际采集 FPS 判断是否存在输入端低于目标的事实。"""

        percent = self.native_capture_fps_target_percent
        return percent is not None and percent < 100.0

    @property
    def measured_stages(self) -> tuple[StageBudgetAssessment, ...]:
        return tuple(stage for stage in self.stages if stage.utilization_percent is not None)

    @property
    def likely_bottleneck(self) -> StageBudgetAssessment | None:
        measured = self.measured_stages
        if not measured:
            return None
        measured_keys = {stage.key for stage in measured}
        if {"display_compose", "display_sink"} <= measured_keys:
            measured = tuple(stage for stage in measured if stage.key != "display_render")
        return max(measured, key=lambda stage: stage.utilization_percent or 0.0)


def diagnose_session_performance(
    summary: Mapping[str, Any],
    *,
    target_fps_per_camera: float = 30.0,
) -> SessionPerformanceDiagnosis:
    """按双摄串行推理和同步对更新频率给出局部预算诊断。"""

    if target_fps_per_camera <= 0.0:
        raise ValueError("target_fps_per_camera must be positive")
    inference_budget_ms = 1000.0 / (2.0 * target_fps_per_camera)
    pair_budget_ms = 1000.0 / target_fps_per_camera
    pipeline = _mapping(summary.get("pipeline"))
    capture = _mapping(summary.get("capture"))
    dpu = _mapping(summary.get("dpu_runtime"))
    avatar = _mapping(summary.get("avatar_runtime"))
    display = _mapping(summary.get("display_render"))
    joint = _mapping(summary.get("joint_metric_runtime"))
    native_vart = _mapping(summary.get("native_vart"))
    pose_sync = _mapping(summary.get("pose_sync"))
    avatar_dispatch = _mapping(summary.get("avatar_dispatch"))
    display_dispatch = _mapping(summary.get("display_dispatch"))
    observed_pair_fps = _optional_number(
        pipeline.get("observed_synchronized_pair_fps")
    )
    avatar_frames = _positive_count(avatar.get("frames"))
    avatar_vertices = _positive_count(avatar.get("total_vertex_count"))
    avatar_tx_bytes = _positive_count(avatar.get("total_packet_tx_bytes"))
    avatar_rx_bytes = _positive_count(avatar.get("total_packet_rx_bytes"))
    held_matrix_uses = _non_negative_count(avatar.get("held_matrix_uses"))
    expired_matrix_uses = _non_negative_count(avatar.get("expired_matrix_uses"))
    vertices_per_frame = (
        avatar_vertices / avatar_frames
        if avatar_frames is not None and avatar_vertices is not None
        else None
    )
    stages = (
        _assessment(
            "pipeline_inference",
            "完整单帧推理（含预处理）",
            pipeline,
            "p95_inference_ms",
            inference_budget_ms,
            "latency_sample_count",
        ),
        _assessment(
            "dpu_runtime",
            "DPU 执行（VART 提交到等待返回）",
            dpu,
            "p95_ms",
            inference_budget_ms,
            "latency_sample_count",
        ),
        _assessment(
            "joint_metric_runtime",
            "关节指标 PL 批处理",
            joint,
            "p95_ms",
            pair_budget_ms,
            "latency_sample_count",
        ),
        _assessment(
            "avatar_runtime",
            "数字人规划、蒙皮与状态更新",
            avatar,
            "p95_total_ms",
            pair_budget_ms,
            "latency_sample_count",
        ),
        _assessment(
            "rectified_triangulation_runtime",
            "校正三角测量批处理/DMA",
            _mapping(avatar.get("triangulation_backend_stats")),
            "p95_ms",
            pair_budget_ms,
            "latency_sample_count",
        ),
        _assessment(
            "display_render",
            "画布合成与写屏",
            display,
            "p95_ms",
            pair_budget_ms,
            "latency_sample_count",
        ),
        _assessment(
            "display_compose",
            "显示画布合成",
            display,
            "compose_p95_ms",
            pair_budget_ms,
            "latency_sample_count",
        ),
        _assessment(
            "display_sink",
            "窗口/framebuffer 写入",
            display,
            "sink_p95_ms",
            pair_budget_ms,
            "latency_sample_count",
        ),
        _assessment(
            "capture_front_read",
            "正面摄像头/UVC读取",
            _mapping(capture.get("front_read_timing")),
            "p95_ms",
            pair_budget_ms,
            "latency_sample_count",
        ),
        _assessment(
            "capture_side_read",
            "侧面摄像头/UVC读取",
            _mapping(capture.get("side_read_timing")),
            "p95_ms",
            pair_budget_ms,
            "latency_sample_count",
        ),
        _assessment(
            "native_capture_front_read",
            "原生 VART 正面摄像头读取",
            native_vart,
            "front_capture_read_p95_ms",
            pair_budget_ms,
            "frames",
        ),
        _assessment(
            "native_capture_side_read",
            "原生 VART 侧面摄像头读取",
            native_vart,
            "side_capture_read_p95_ms",
            pair_budget_ms,
            "frames",
        ),
    )
    return SessionPerformanceDiagnosis(
        target_fps_per_camera,
        stages,
        observed_pair_fps=observed_pair_fps,
        p95_end_to_end_ms=_optional_number(pipeline.get("p95_end_to_end_ms")),
        avatar_vertices_per_frame=vertices_per_frame,
        avatar_vertices_per_second=(
            vertices_per_frame * observed_pair_fps
            if vertices_per_frame is not None and observed_pair_fps is not None
            else None
        ),
        avatar_tx_mib_per_second=_rate_mib_per_second(
            avatar_tx_bytes, avatar_frames, observed_pair_fps
        ),
        avatar_rx_mib_per_second=_rate_mib_per_second(
            avatar_rx_bytes, avatar_frames, observed_pair_fps
        ),
        valid_triangulated_percent=_optional_percentage(
            avatar.get("valid_triangulated_percent")
        ),
        finite_ray_gap_percent=_optional_percentage(
            avatar.get("finite_ray_gap_percent")
        ),
        p95_ray_gap_world_units=_optional_number(
            avatar.get("p95_ray_gap_world_units")
        ),
        p05_ray_angle_degrees=_optional_number(
            avatar.get("p05_ray_angle_degrees")
        ),
        held_matrices_per_frame=_per_frame(held_matrix_uses, avatar_frames),
        expired_matrices_per_frame=_per_frame(expired_matrix_uses, avatar_frames),
        native_front_capture_fps=_optional_number(native_vart.get("front_capture_fps")),
        native_side_capture_fps=_optional_number(native_vart.get("side_capture_fps")),
        native_e2e_pair_fps=_optional_number(native_vart.get("e2e_pair_fps")),
        native_pair_fps=_optional_number(native_vart.get("pair_fps")),
        native_pipeline_capture=(
            native_vart.get("pipeline_capture")
            if isinstance(native_vart.get("pipeline_capture"), bool)
            else None
        ),
        native_capture_prefetched_pairs=_non_negative_count(
            native_vart.get("capture_prefetched_pairs")
        ),
        native_post_distance_evaluated=_non_negative_count(
            native_vart.get("post_distance_evaluated")
        ),
        native_post_distance_pruned=_non_negative_count(
            native_vart.get("post_distance_pruned")
        ),
        native_post_distance_prune_percent=_optional_percentage(
            native_vart.get("post_distance_prune_percent")
        ),
        native_front_preprocess_avg_ms=_optional_number(
            native_vart.get("front_preprocess_avg_ms")
        ),
        native_side_preprocess_avg_ms=_optional_number(
            native_vart.get("side_preprocess_avg_ms")
        ),
        native_front_fourcc=_optional_fourcc(native_vart.get("front_fourcc")),
        native_side_fourcc=_optional_fourcc(native_vart.get("side_fourcc")),
        scheduler_dropped_front=_non_negative_count(pipeline.get("dropped_front")),
        scheduler_dropped_side=_non_negative_count(pipeline.get("dropped_side")),
        sync_flow=_sync_flow_observation(pose_sync),
        avatar_dispatch=_dispatch_flow_observation(
            avatar_dispatch,
            submitted_key="submitted_pairs",
            completed_key="processed_pairs",
            dropped_key="dropped_pairs",
        ),
        avatar_observed_process_fps=_optional_number(
            avatar_dispatch.get("observed_process_fps")
        ),
        avatar_mean_content_age_ms=_optional_number(
            avatar_dispatch.get("mean_content_age_ms")
        ),
        avatar_p95_content_age_ms=_optional_number(
            avatar_dispatch.get("p95_content_age_ms")
        ),
        avatar_max_content_age_ms=_optional_number(
            avatar_dispatch.get("max_content_age_ms")
        ),
        avatar_content_age_pairs=_non_negative_count(
            avatar_dispatch.get("content_age_pairs")
        ),
        avatar_content_age_sample_count=_non_negative_count(
            avatar_dispatch.get("content_age_sample_count")
        ),
        avatar_latency_window_size=_positive_count(
            avatar_dispatch.get("latency_window_size")
        ),
        avatar_invalid_content_age_samples=_non_negative_count(
            avatar_dispatch.get("invalid_content_age_samples")
        ),
        avatar_missing_content_timestamp_pairs=_non_negative_count(
            avatar_dispatch.get("missing_content_timestamp_pairs")
        ),
        avatar_content_timestamp_semantics=_optional_string(
            avatar_dispatch.get("content_timestamp_semantics")
        ),
        display_dispatch=_dispatch_flow_observation(
            display_dispatch,
            submitted_key="submitted_frames",
            completed_key="rendered_frames",
            dropped_key="dropped_frames",
        ),
        display_observed_render_fps=_optional_number(
            display_dispatch.get("observed_render_fps")
        ),
        display_mean_content_age_ms=_optional_number(
            display_dispatch.get("mean_content_age_ms")
        ),
        display_p95_content_age_ms=_optional_number(
            display_dispatch.get("p95_content_age_ms")
        ),
        display_max_content_age_ms=_optional_number(
            display_dispatch.get("max_content_age_ms")
        ),
        display_content_age_frames=_non_negative_count(
            display_dispatch.get("content_age_frames")
        ),
        display_content_age_sample_count=_non_negative_count(
            display_dispatch.get("content_age_sample_count")
        ),
        display_latency_window_size=_positive_count(
            display_dispatch.get("latency_window_size")
        ),
        display_invalid_content_age_samples=_non_negative_count(
            display_dispatch.get("invalid_content_age_samples")
        ),
        display_missing_content_timestamp_frames=_non_negative_count(
            display_dispatch.get("missing_content_timestamp_frames")
        ),
        display_content_timestamp_semantics=_optional_string(
            display_dispatch.get("content_timestamp_semantics")
        ),
    )


def render_diagnosis_markdown(diagnosis: SessionPerformanceDiagnosis) -> str:
    """生成可以直接保存或粘贴到验收记录中的中文 Markdown。"""

    lines = [
        "# PosePriv-Coach 会话性能诊断",
        "",
        f"目标：双摄每路 {diagnosis.target_fps_per_camera:g} FPS。以下是局部预算诊断，不是整机达标声明。",
        "",
        "## 整体观测",
        "",
        _overall_observation(diagnosis),
        "",
        "## 原生 VART 输入边界",
        "",
        _native_capture_observation(diagnosis),
        "",
        "## 同步、丢帧与输出支路",
        "",
        _flow_observation(diagnosis),
        "",
        "## 分阶段局部预算",
        "",
        "| 阶段 | P95 | 局部预算 | 预算占用 | 结论 | 样本数 |",
        "|---|---:|---:|---:|---|---:|",
    ]
    for stage in diagnosis.stages:
        p95 = "—" if stage.p95_ms is None else f"{stage.p95_ms:.3f} ms"
        utilization = stage.utilization_percent
        utilization_text = "—" if utilization is None else f"{utilization:.1f}%"
        lines.append(
            f"| {stage.label} | {p95} | {stage.budget_ms:.3f} ms | "
            f"{utilization_text} | {stage.status} | {stage.sample_count} |"
        )
    bottleneck = diagnosis.likely_bottleneck
    lines.extend(["", "## 解释", ""])
    if bottleneck is None:
        lines.append("当前报告没有可用的 P95 样本，需先完成一次真实会话。")
    else:
        lines.append(
            f"当前局部预算占用最高的是“{bottleneck.label}”"
            f"（{bottleneck.utilization_percent:.1f}%），应优先检查这一阶段。"
        )
    measured_keys = {stage.key for stage in diagnosis.measured_stages}
    if {"display_compose", "display_sink"} <= measured_keys:
        lines.append(
            "显示总边界保留用于预算观察；瓶颈排序使用同会话 compose/sink 分段，"
            "避免总项遮住可执行的显示热点。"
        )
    lines.extend(
        [
            "",
            "DPU 和完整推理使用每秒 60 次推理的 16.667 ms 局部预算；数字人与显示按每秒 30 个同步对使用 33.333 ms 局部预算。",
            "各阶段可能存在流水和重叠，统计边界与样本单位也不同，因此不能把表中 P95 直接相加。最终实时性仍以真实双摄端到端 FPS、同步对率和端到端 P95 为准。",
            "",
        ]
    )
    lines.extend(
        [
            "## 双目3D质量观测",
            "",
            _geometry_observation(diagnosis),
            "",
            "这些数值用于区分标定、同步、遮挡和骨骼保持问题，不参与上面的局部性能预算判定。",
            "",
        ]
    )
    return "\n".join(lines)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number >= 0.0 else None


def _optional_fourcc(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 4:
        return None
    return value


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value


def _positive_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _non_negative_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _optional_percentage(value: object) -> float | None:
    number = _optional_number(value)
    return number if number is not None and number <= 100.0 else None


def _paired_view_percent(paired: int | None, dropped: int | None) -> float | None:
    if paired is None or dropped is None or paired + dropped == 0:
        return None
    return paired / (paired + dropped) * 100.0


def _sync_flow_observation(
    record: Mapping[str, Any],
) -> SyncFlowObservation | None:
    if not record:
        return None
    return SyncFlowObservation(
        paired_frames=_non_negative_count(record.get("paired_frames")),
        dropped_front=_non_negative_count(record.get("dropped_front")),
        dropped_side=_non_negative_count(record.get("dropped_side")),
        queue_overflow_front=_non_negative_count(record.get("queue_overflow_front")),
        queue_overflow_side=_non_negative_count(record.get("queue_overflow_side")),
        skew_rejected_front=_non_negative_count(record.get("skew_rejected_front")),
        skew_rejected_side=_non_negative_count(record.get("skew_rejected_side")),
        superseded_front=_non_negative_count(record.get("superseded_front")),
        superseded_side=_non_negative_count(record.get("superseded_side")),
    )


def _dispatch_flow_observation(
    record: Mapping[str, Any],
    *,
    submitted_key: str,
    completed_key: str,
    dropped_key: str,
) -> DispatchFlowObservation | None:
    if not record:
        return None
    failed = record.get("failed")
    return DispatchFlowObservation(
        submitted=_non_negative_count(record.get(submitted_key)),
        completed=_non_negative_count(record.get(completed_key)),
        dropped=_non_negative_count(record.get(dropped_key)),
        failed=failed if isinstance(failed, bool) else None,
    )


def _per_frame(total: int | None, frames: int | None) -> float | None:
    if total is None or frames is None:
        return None
    return total / frames


def _rate_mib_per_second(
    total_bytes: int | None,
    frames: int | None,
    observed_fps: float | None,
) -> float | None:
    if total_bytes is None or frames is None or observed_fps is None:
        return None
    return total_bytes / frames * observed_fps / (1024.0 * 1024.0)


def _overall_observation(diagnosis: SessionPerformanceDiagnosis) -> str:
    fps = diagnosis.observed_pair_fps
    latency = diagnosis.p95_end_to_end_ms
    fps_text = "未测量" if fps is None else f"{fps:.3f} FPS"
    fps_percent = diagnosis.pair_fps_target_percent
    percent_text = "未测量" if fps_percent is None else f"{fps_percent:.1f}%"
    latency_text = "未测量" if latency is None else f"{latency:.3f} ms"
    avatar_text = "未测量"
    if diagnosis.avatar_vertices_per_frame is not None:
        throughput = (
            "未测量"
            if diagnosis.avatar_vertices_per_second is None
            else f"{diagnosis.avatar_vertices_per_second:,.0f} 顶点/秒"
        )
        tx = (
            "未测量"
            if diagnosis.avatar_tx_mib_per_second is None
            else f"{diagnosis.avatar_tx_mib_per_second:.3f} MiB/s"
        )
        rx = (
            "未测量"
            if diagnosis.avatar_rx_mib_per_second is None
            else f"{diagnosis.avatar_rx_mib_per_second:.3f} MiB/s"
        )
        avatar_text = (
            f"{diagnosis.avatar_vertices_per_frame:,.0f} 顶点/帧，"
            f"{throughput}，TX {tx} / RX {rx}"
        )
    return (
        f"- 实际同步对吞吐：{fps_text}（目标完成度 {percent_text}）\n"
        f"- 端到端 P95：{latency_text}；目标帧周期 "
        f"{diagnosis.target_pair_period_ms:.3f} ms\n"
        f"- 数字人实际负载：{avatar_text}"
    )


def _native_capture_observation(diagnosis: SessionPerformanceDiagnosis) -> str:
    front = diagnosis.native_front_capture_fps
    side = diagnosis.native_side_capture_fps
    front_preprocess = diagnosis.native_front_preprocess_avg_ms
    side_preprocess = diagnosis.native_side_preprocess_avg_ms
    front_fourcc = diagnosis.native_front_fourcc
    side_fourcc = diagnosis.native_side_fourcc
    e2e = diagnosis.native_e2e_pair_fps
    processing = diagnosis.native_pair_fps
    if (
        front is None
        and side is None
        and e2e is None
        and processing is None
        and diagnosis.native_pipeline_capture is None
        and diagnosis.native_capture_prefetched_pairs is None
        and diagnosis.native_post_distance_evaluated is None
        and diagnosis.native_post_distance_pruned is None
        and diagnosis.native_post_distance_prune_percent is None
        and front_preprocess is None
        and side_preprocess is None
        and front_fourcc is None
        and side_fourcc is None
    ):
        return "报告没有原生 VART 采集统计；不能据此判断 USB、解码或相机入口是否成为瓶颈。"
    front_text = "—" if front is None else f"{front:.3f} FPS"
    side_text = "—" if side is None else f"{side:.3f} FPS"
    e2e_text = "—" if e2e is None else f"{e2e:.3f} 对/秒"
    processing_text = "—" if processing is None else f"{processing:.3f} 对/秒"
    lines = [
        f"正面实际取帧：{front_text}；侧面实际取帧：{side_text}；"
        f"原生端到端：{e2e_text}；处理循环：{processing_text}。",
    ]
    if front_preprocess is not None or side_preprocess is not None:
        front_preprocess_text = (
            "未提供" if front_preprocess is None else f"{front_preprocess:.3f} ms"
        )
        side_preprocess_text = (
            "未提供" if side_preprocess is None else f"{side_preprocess:.3f} ms"
        )
        lines.append(
            "输入预处理平均耗时："
            f"正面 {front_preprocess_text}、侧面 {side_preprocess_text}；"
            "该字段是 CPU 工作量，不等同于启用并行 DPU 时的墙钟延迟。"
        )
    if front_fourcc is not None or side_fourcc is not None:
        lines.append(
            "实际协商格式："
            f"正面 {front_fourcc or '未提供'}、侧面 {side_fourcc or '未提供'}；"
            "若不是期望的 MJPG，应先处理 UVC 模式协商和 USB 带宽。"
        )
    if (
        diagnosis.native_pipeline_capture is not None
        or diagnosis.native_capture_prefetched_pairs is not None
    ):
        mode = (
            "未提供"
            if diagnosis.native_pipeline_capture is None
            else ("启用" if diagnosis.native_pipeline_capture else "关闭")
        )
        prefetched = diagnosis.native_capture_prefetched_pairs
        lines.append(
            f"单对采集预取：{mode}；已启动预取对数："
            f"{'未提供' if prefetched is None else prefetched}。"
            "该字段只描述流水策略，不等同于端到端性能提升。"
        )
    if (
        diagnosis.native_post_distance_evaluated is not None
        or diagnosis.native_post_distance_pruned is not None
        or diagnosis.native_post_distance_prune_percent is not None
    ):
        evaluated = diagnosis.native_post_distance_evaluated
        pruned = diagnosis.native_post_distance_pruned
        percent = diagnosis.native_post_distance_prune_percent
        evaluated_text = "未提供" if evaluated is None else str(evaluated)
        pruned_text = "未提供" if pruned is None else str(pruned)
        percent_text = "未提供" if percent is None else f"{percent:.2f}%"
        lines.append(
            "后处理距离评估："
            f"实际开方 {evaluated_text} 次，"
            f"安全剪枝 {pruned_text} 次，"
            f"剪枝率 {percent_text}。"
            "该剪枝保持原评分排序，只减少不可能胜出的候选开方。"
        )
    if diagnosis.native_capture_is_input_limited:
        percent = diagnosis.native_capture_fps_target_percent
        lines.append(
            f"较慢一路只有目标的 {percent:.1f}%（目标 {diagnosis.target_fps_per_camera:g} FPS），"
            "应优先检查 USB 根总线、UVC 协商、MJPEG 解码和驱动缓冲；这条证据不能通过增加 DPU/PL 算子解决。"
        )
    else:
        lines.append("两路实际取帧均未低于目标；若端到端仍不足，再比较 DPU、后处理、同步和显示阶段。")
    return "\n".join(lines)


def _flow_observation(diagnosis: SessionPerformanceDiagnosis) -> str:
    scheduler_front = diagnosis.scheduler_dropped_front
    scheduler_side = diagnosis.scheduler_dropped_side
    sync = diagnosis.sync_flow
    avatar = diagnosis.avatar_dispatch
    display = diagnosis.display_dispatch
    if (
        scheduler_front is None
        and scheduler_side is None
        and sync is None
        and avatar is None
        and display is None
    ):
        return "报告没有同步或 latest-only 调度统计，不能区分入口覆盖、同步淘汰和输出支路丢弃。"

    lines: list[str] = []
    if scheduler_front is not None or scheduler_side is not None:
        lines.append(
            "推理前 LatestFrameScheduler 覆盖旧帧："
            f"正面 {_count_text(scheduler_front)}、侧面 {_count_text(scheduler_side)}。"
        )
    if sync is not None:
        paired = _count_text(sync.paired_frames)
        input_percent = _percent_text(sync.input_pairing_percent)
        front_percent = _percent_text(sync.front_pairing_percent)
        side_percent = _percent_text(sync.side_pairing_percent)
        lines.append(
            f"同步器成功 {paired} 对；已结算输入配对利用率 {input_percent}，"
            f"正面 {front_percent}、侧面 {side_percent}。"
        )
        lines.append(
            "同步淘汰分类（正/侧）："
            f"队列溢出 {_count_pair_text(sync.queue_overflow_front, sync.queue_overflow_side)}，"
            f"时间偏差拒绝 {_count_pair_text(sync.skew_rejected_front, sync.skew_rejected_side)}，"
            f"更优匹配覆盖 {_count_pair_text(sync.superseded_front, sync.superseded_side)}；"
            f"总淘汰 {_count_pair_text(sync.dropped_front, sync.dropped_side)}，"
            f"分类账{_accounting_text(sync.drop_accounting_complete)}。"
        )
    if avatar is not None:
        lines.append(_dispatch_observation("数字人", avatar, "对"))
    avatar_freshness = _avatar_freshness_observation(diagnosis)
    if avatar_freshness is not None:
        lines.append(avatar_freshness)
    if display is not None:
        lines.append(_dispatch_observation("HDMI/显示", display, "帧"))
    display_freshness = _display_freshness_observation(diagnosis)
    if display_freshness is not None:
        lines.append(display_freshness)
    lines.append(
        "上述计数边界彼此独立：入口覆盖和输出支路 latest-only 丢弃用于限制旧帧积压，"
        "不能与同步器淘汰相加后当作相机丢帧率。"
    )
    return "\n".join(lines)


def _dispatch_observation(
    label: str,
    observation: DispatchFlowObservation,
    unit: str,
) -> str:
    failed = (
        "未提供"
        if observation.failed is None
        else ("是" if observation.failed else "否")
    )
    return (
        f"{label} latest-only 支路：提交 {_count_text(observation.submitted)} {unit}，"
        f"完成 {_count_text(observation.completed)} {unit}，"
        f"覆盖丢弃 {_count_text(observation.dropped)} {unit}，"
        f"完成率 {_percent_text(observation.completion_percent)}，"
        f"失败={failed}，分类账{_accounting_text(observation.drop_accounting_complete)}。"
    )


def _display_freshness_observation(
    diagnosis: SessionPerformanceDiagnosis,
) -> str | None:
    values = (
        diagnosis.display_observed_render_fps,
        diagnosis.display_mean_content_age_ms,
        diagnosis.display_p95_content_age_ms,
        diagnosis.display_max_content_age_ms,
        diagnosis.display_content_age_frames,
        diagnosis.display_content_age_sample_count,
        diagnosis.display_latency_window_size,
        diagnosis.display_invalid_content_age_samples,
        diagnosis.display_missing_content_timestamp_frames,
        diagnosis.display_content_timestamp_semantics,
    )
    if all(value is None for value in values):
        return None
    render_fps = (
        "未提供"
        if diagnosis.display_observed_render_fps is None
        else f"{diagnosis.display_observed_render_fps:.3f} FPS"
    )
    mean_age = _milliseconds_text(diagnosis.display_mean_content_age_ms)
    p95_age = _milliseconds_text(diagnosis.display_p95_content_age_ms)
    max_age = _milliseconds_text(diagnosis.display_max_content_age_ms)
    sample_count = _count_text(diagnosis.display_content_age_sample_count)
    window_size = _count_text(diagnosis.display_latency_window_size)
    semantics = diagnosis.display_content_timestamp_semantics
    if semantics == "newer_sync_adjusted_pose_timestamp_to_render_complete":
        semantics_text = "同步修正后的较新姿态时间戳到写屏完成"
    elif semantics is None:
        semantics_text = "未提供"
    else:
        semantics_text = semantics
    return (
        f"HDMI/显示实际呈现率 {render_fps}；内容年龄平均/P95/最大值 "
        f"{mean_age}/{p95_age}/{max_age}，有效帧 "
        f"{_count_text(diagnosis.display_content_age_frames)}，近期样本 "
        f"{sample_count}/{window_size}；未来时间戳样本 "
        f"{_count_text(diagnosis.display_invalid_content_age_samples)}，缺失时间戳帧 "
        f"{_count_text(diagnosis.display_missing_content_timestamp_frames)}。"
        f"内容时间戳口径：{semantics_text}；"
        "不代表传感器曝光到显示的延迟。"
    )


def _avatar_freshness_observation(
    diagnosis: SessionPerformanceDiagnosis,
) -> str | None:
    values = (
        diagnosis.avatar_observed_process_fps,
        diagnosis.avatar_mean_content_age_ms,
        diagnosis.avatar_p95_content_age_ms,
        diagnosis.avatar_max_content_age_ms,
        diagnosis.avatar_content_age_pairs,
        diagnosis.avatar_content_age_sample_count,
        diagnosis.avatar_latency_window_size,
        diagnosis.avatar_invalid_content_age_samples,
        diagnosis.avatar_missing_content_timestamp_pairs,
        diagnosis.avatar_content_timestamp_semantics,
    )
    if all(value is None for value in values):
        return None
    process_fps = (
        "未提供"
        if diagnosis.avatar_observed_process_fps is None
        else f"{diagnosis.avatar_observed_process_fps:.3f} FPS"
    )
    mean_age = _milliseconds_text(diagnosis.avatar_mean_content_age_ms)
    p95_age = _milliseconds_text(diagnosis.avatar_p95_content_age_ms)
    max_age = _milliseconds_text(diagnosis.avatar_max_content_age_ms)
    semantics = diagnosis.avatar_content_timestamp_semantics
    if semantics == "newer_sync_adjusted_pose_timestamp_to_avatar_process_complete":
        semantics_text = "同步修正后的较新姿态时间戳到数字人处理完成"
    elif semantics is None:
        semantics_text = "未提供"
    else:
        semantics_text = semantics
    return (
        f"数字人实际处理率 {process_fps}；内容年龄平均/P95/最大值 "
        f"{mean_age}/{p95_age}/{max_age}，有效同步对 "
        f"{_count_text(diagnosis.avatar_content_age_pairs)}，近期样本 "
        f"{_count_text(diagnosis.avatar_content_age_sample_count)}/"
        f"{_count_text(diagnosis.avatar_latency_window_size)}；未来时间戳样本 "
        f"{_count_text(diagnosis.avatar_invalid_content_age_samples)}，缺失时间戳对 "
        f"{_count_text(diagnosis.avatar_missing_content_timestamp_pairs)}。"
        f"内容时间戳口径：{semantics_text}；"
        "不代表传感器曝光到数字人处理完成的延迟。"
    )


def _count_text(value: int | None) -> str:
    return "未提供" if value is None else str(value)


def _count_pair_text(front: int | None, side: int | None) -> str:
    return f"{_count_text(front)}/{_count_text(side)}"


def _percent_text(value: float | None) -> str:
    return "未提供" if value is None else f"{value:.2f}%"


def _milliseconds_text(value: float | None) -> str:
    return "未提供" if value is None else f"{value:.3f} ms"


def _accounting_text(value: bool | None) -> str:
    if value is None:
        return "未提供"
    return "一致" if value else "不一致"


def _geometry_observation(diagnosis: SessionPerformanceDiagnosis) -> str:
    def value(number: float | None, suffix: str, digits: int = 2) -> str:
        return "未测量" if number is None else f"{number:.{digits}f}{suffix}"

    return (
        "有效三维点比例："
        f"{value(diagnosis.valid_triangulated_percent, '%')}；"
        "有限射线对比例："
        f"{value(diagnosis.finite_ray_gap_percent, '%')}；"
        "射线间距 P95："
        f"{value(diagnosis.p95_ray_gap_world_units, '')}；"
        "视差角 P05："
        f"{value(diagnosis.p05_ray_angle_degrees, '°')}；"
        "每帧保持/失效矩阵："
        f"{value(diagnosis.held_matrices_per_frame, '')}/"
        f"{value(diagnosis.expired_matrices_per_frame, '')}。"
    )


def _assessment(
    key: str,
    label: str,
    record: Mapping[str, Any],
    p95_key: str,
    budget_ms: float,
    count_key: str,
) -> StageBudgetAssessment:
    raw_p95 = record.get(p95_key)
    p95_ms = float(raw_p95) if isinstance(raw_p95, (int, float)) else None
    raw_count = record.get(count_key, 0)
    sample_count = int(raw_count) if isinstance(raw_count, int) and raw_count >= 0 else 0
    return StageBudgetAssessment(key, label, p95_ms, budget_ms, sample_count)
