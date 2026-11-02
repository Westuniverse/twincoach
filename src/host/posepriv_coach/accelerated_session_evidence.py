"""从会话 JSONL 汇总 DPU、双 PL 与 HDMI 是否在同一进程中实际工作。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .runtime_identity import normalize_linux_boot_id


@dataclass(frozen=True, slots=True)
class AcceleratedEvidenceCheck:
    name: str
    passed: bool
    detail_zh: str


@dataclass(frozen=True, slots=True)
class AcceleratedSessionEvidence:
    passed: bool
    checks: tuple[AcceleratedEvidenceCheck, ...]
    next_action_zh: str
    linux_boot_id: str | None = None
    assessment: str = "unified_accelerated_session_evidence"

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "assessment": self.assessment,
            "passed": self.passed,
            "linux_boot_id": self.linux_boot_id,
            "checks": [asdict(check) for check in self.checks],
            "next_action_zh": self.next_action_zh,
        }


def assess_accelerated_session_report(
    path: str | Path,
    *,
    pl_golden_evidence_path: str | Path | None = None,
    deployment_readiness_path: str | Path | None = None,
    dpu_pose_comparison_path: str | Path | None = None,
) -> AcceleratedSessionEvidence:
    """检查一份已结束会话，不设置帧率或延迟性能阈值。"""

    records = _read_jsonl(path)
    header = records[0]
    summaries = [item for item in records if item.get("record_type") == "session_summary"]
    if len(summaries) != 1:
        raise ValueError("会话报告必须恰好包含一条 session_summary")
    summary = summaries[0]
    linux_boot_id = normalize_linux_boot_id(header.get("linux_boot_id"))
    configuration = _mapping(header.get("configuration"), "configuration")
    topology = _mapping(configuration.get("runtime_topology"), "runtime_topology")
    selected = _mapping(topology.get("selected"), "runtime_topology.selected")
    camera_mode_plan = configuration.get("camera_mode_plan")
    camera_mode_selection_raw = configuration.get("camera_mode_selection")
    camera_mode_selection = _optional_mapping(camera_mode_selection_raw)
    calibration_geometry = _optional_mapping(
        configuration.get("calibration_capture_geometry")
    )
    avatar_mesh = _optional_mapping(configuration.get("avatar_mesh_metadata"))
    front_mode = _optional_mapping(configuration.get("front_negotiated_mode"))
    side_mode = _optional_mapping(configuration.get("side_negotiated_mode"))
    pipeline = _mapping(summary.get("pipeline"), "pipeline")
    sync = _mapping(summary.get("pose_sync"), "pose_sync")
    capture = _optional_mapping(summary.get("capture"))
    dpu = _optional_mapping(summary.get("dpu_runtime"))
    native_vart = _optional_mapping(summary.get("native_vart"))
    joint = _optional_mapping(summary.get("joint_metric_runtime"))
    avatar = _optional_mapping(summary.get("avatar_runtime"))
    display = _optional_mapping(summary.get("display_render"))
    motion_telemetry = _optional_mapping(summary.get("motion_telemetry"))
    avatar_dispatch = _optional_mapping(summary.get("avatar_dispatch"))
    display_dispatch = _optional_mapping(summary.get("display_dispatch"))

    processed = _count(pipeline, "processed_frames")
    pairs = _count(sync, "paired_frames")
    skew_samples = _count(sync, "skew_sample_count")
    dpu_frames = _count(dpu, "frames") if dpu is not None else 0
    joint_batches = _count(joint, "batches") if joint is not None else 0
    joint_beats = _count(joint, "input_beats") if joint is not None else 0
    avatar_frames = _count(avatar, "frames") if avatar is not None else 0
    avatar_vertices_per_frame = _count(avatar_mesh, "vertex_count")
    avatar_total_vertices = _count(avatar, "total_vertex_count") if avatar is not None else 0
    triangulated_points = _count(avatar, "triangulated_point_count")
    valid_3d_points = _count(avatar, "valid_triangulated_point_count")
    finite_ray_gaps = _count(avatar, "finite_ray_gap_count")
    ray_angle_samples = _count(avatar, "ray_angle_sample_count")
    display_frames = _count(display, "frames") if display is not None else 0
    front_capture_frames = _count(capture, "front_frames") if capture is not None else 0
    side_capture_frames = _count(capture, "side_frames") if capture is not None else 0
    native_frames = _count(native_vart, "frames")
    motion_batch_stats = _optional_mapping(
        motion_telemetry.get("batch_stats") if motion_telemetry is not None else None
    )
    motion_stats_backend_stats = _optional_mapping(
        motion_batch_stats.get("backend")
        if motion_batch_stats is not None
        else None
    )
    avatar_dispatch_ok = _avatar_dispatch_is_healthy(
        avatar_dispatch,
        pairs=pairs,
        avatar_frames=avatar_frames,
    )
    avatar_freshness_ok = _avatar_freshness_is_complete(
        avatar_dispatch,
        avatar_frames=avatar_frames,
    )
    display_dispatch_ok = _display_dispatch_is_healthy(
        display_dispatch,
        pairs=pairs,
        display_frames=display_frames,
    )
    display_freshness_ok = _display_freshness_is_complete(
        display_dispatch,
        display_frames=display_frames,
    )
    native_stats_ready = _native_vart_runtime_stats_ready(native_vart)
    if front_mode is None:
        front_mode = _native_camera_mode(native_vart, configuration, "front")
    if side_mode is None:
        side_mode = _native_camera_mode(native_vart, configuration, "side")
    if calibration_geometry is None and native_vart is not None:
        calibration_geometry = _optional_mapping(
            native_vart.get("calibration_capture_geometry")
        )
    capture_completed = (
        (
            capture is not None
            and capture.get("failed") is False
            and front_capture_frames > 0
            and side_capture_frames > 0
            and front_capture_frames >= pairs
            and side_capture_frames >= pairs
        )
        or (native_stats_ready and native_frames >= pairs and pairs > 0)
    )
    dpu_completed = (
        (dpu is not None and processed > 0 and dpu_frames == processed)
        or (native_stats_ready and native_frames >= pairs and pairs > 0)
    )

    checks = [
        AcceleratedEvidenceCheck(
            "linux_boot_session_identified",
            linux_boot_id is not None,
            f"实时会话 Linux boot ID={linux_boot_id}"
            if linux_boot_id is not None
            else "实时会话报告缺少规范、非零的 Linux boot ID",
        ),
        AcceleratedEvidenceCheck(
            "unified_topology_selected",
            topology.get("unified_accelerated_candidate") is True,
            "四段联合加速后端已同时选择"
            if topology.get("unified_accelerated_candidate") is True
            else "报告头没有选择完整的 DPU、关节 PL、蒙皮 PL 和 framebuffer",
        ),
        AcceleratedEvidenceCheck(
            "dual_camera_capture_completed",
            capture_completed,
            (
                f"原生 VART 统计正面/侧面采集 {native_vart.get('front_capture_fps')} / "
                f"{native_vart.get('side_capture_fps')} FPS，同步对 {pairs}"
                if native_stats_ready and capture is None
                else f"正面采集 {front_capture_frames} 帧、侧面采集 {side_capture_frames} 帧，"
                f"同步对 {pairs}，采集失败={capture.get('failed') if capture else '未知'}"
            ),
        ),
        AcceleratedEvidenceCheck(
            "dual_camera_sync_statistics_complete",
            pairs > 0
            and skew_samples == pairs
            and _non_negative_finite(sync.get("mean_skew_ms"))
            and _non_negative_finite(sync.get("p95_skew_ms"))
            and _non_negative_finite(sync.get("max_skew_ms")),
            f"同步对/偏差样本 {pairs}/{skew_samples}，"
            f"mean/P95/max={sync.get('mean_skew_ms')}/"
            f"{sync.get('p95_skew_ms')}/{sync.get('max_skew_ms')} ms",
        ),
        AcceleratedEvidenceCheck(
            "dual_camera_modes_negotiated",
            _valid_camera_mode(front_mode) and _valid_camera_mode(side_mode),
            "正面实际模式="
            f"{_camera_mode_text(front_mode)}；侧面实际模式={_camera_mode_text(side_mode)}",
        ),
        AcceleratedEvidenceCheck(
            "dpu_processed_frames",
            dpu_completed,
            (
                f"原生 VART 处理 {native_frames} 对，管线同步对 {pairs}"
                if native_vart is not None and dpu is None
                else f"DPU 帧数 {dpu_frames}，管线处理帧数 {processed}"
            ),
        ),
        AcceleratedEvidenceCheck(
            "joint_pl_completed_batches",
            joint_batches > 0
            and joint_beats > 0
            and _positive_bytes(joint, "tx_bytes")
            and _positive_bytes(joint, "rx_bytes"),
            f"关节 PL 完成 {joint_batches} 批、{joint_beats} 拍，"
            f"TX {_count(joint, 'tx_bytes')} B / RX {_count(joint, 'rx_bytes')} B",
        ),
        AcceleratedEvidenceCheck(
            "avatar_pl_processed_pairs",
            pairs > 0
            and avatar_dispatch_ok
            and _positive_bytes(avatar, "total_packet_tx_bytes")
            and _positive_bytes(avatar, "total_packet_rx_bytes"),
            (
                f"同步对 {pairs}，数字人 PL 帧 {avatar_frames}"
                if avatar_dispatch is None
                else f"同步对 {pairs}，数字人 PL 已处理 {avatar_frames} 对，"
                f"丢弃 {max(0, _count(avatar_dispatch, 'dropped_pairs'))} 对"
            ),
        ),
        AcceleratedEvidenceCheck(
            "avatar_asset_batch_matches_runtime",
            avatar_frames > 0
            and avatar_vertices_per_frame > 0
            and avatar_total_vertices
            == avatar_frames * avatar_vertices_per_frame,
            f"资产 {avatar_vertices_per_frame} 顶点/帧 × {avatar_frames} 帧，"
            f"运行时累计 {avatar_total_vertices} 顶点",
        ),
        AcceleratedEvidenceCheck(
            "stereo_3d_geometry_observed",
            triangulated_points > 0
            and valid_3d_points > 0
            and finite_ray_gaps > 0
            and ray_angle_samples > 0
            and valid_3d_points <= finite_ray_gaps <= triangulated_points
            and ray_angle_samples <= triangulated_points
            and _percentage(avatar.get("valid_triangulated_percent") if avatar else None)
            and _percentage(avatar.get("finite_ray_gap_percent") if avatar else None)
            and _non_negative_finite(
                avatar.get("mean_ray_gap_world_units") if avatar else None
            )
            and _non_negative_finite(
                avatar.get("mean_ray_angle_degrees") if avatar else None
            ),
            f"三角测量候选/有效/有限射线/视差角样本="
            f"{triangulated_points}/{valid_3d_points}/{finite_ray_gaps}/{ray_angle_samples}",
        ),
        AcceleratedEvidenceCheck(
            "hdmi_rendered_pairs",
            pairs > 0 and display_dispatch_ok,
            (
                f"同步对 {pairs}，framebuffer 合成/提交帧 {display_frames}"
                if display_dispatch is None
                else f"同步对 {pairs}，framebuffer 实际提交帧 {display_frames}，"
                f"丢弃 {max(0, _count(display_dispatch, 'dropped_frames'))} 帧"
            ),
        ),
        AcceleratedEvidenceCheck(
            "selected_backends_match_evidence",
            selected.get("pose") in (
                "vart_dpu",
                "vart_dpu_movenet_ntd",
                "native_vart_b3136_jsonl",
            )
            and selected.get("joint_metrics") in ("linux-dma", "linux-dma-proxy")
            and selected.get("avatar_skinning") in ("linux-dma", "linux-dma-proxy")
            and selected.get("display") == "framebuffer",
            "报告头后端名称与联合硬件路径一致",
        ),
        AcceleratedEvidenceCheck(
            "calibration_matches_live_capture",
            calibration_geometry is not None
            and calibration_geometry.get("uniform_scaling_compatible") is True,
            "双目标定与两路实际采集模式可按比例兼容"
            if calibration_geometry is not None
            and calibration_geometry.get("uniform_scaling_compatible") is True
            else "报告头缺少标定/实际采集几何兼容证据",
        ),
    ]
    if camera_mode_plan is not None or camera_mode_selection_raw is not None:
        mode_provenance_passed, mode_provenance_detail = (
            _camera_mode_provenance_check(
                camera_mode_selection,
                front_mode,
                side_mode,
                camera_mode_plan,
            )
        )
        checks.append(
            AcceleratedEvidenceCheck(
                "camera_mode_plan_matches_negotiated_modes",
                mode_provenance_passed,
                mode_provenance_detail,
            )
        )
    triangulation_accelerator = selected.get("triangulation_accelerator")
    if triangulation_accelerator in ("linux-dma", "linux-dma-proxy"):
        triangulation_stats = _optional_mapping(
            avatar.get("triangulation_backend_stats") if avatar else None
        )
        checks.append(
            AcceleratedEvidenceCheck(
                "rectified_triangulation_pl_completed",
                _count(triangulation_stats, "batches") > 0
                and _count(triangulation_stats, "input_beats") > 0
                and _count(triangulation_stats, "tx_bytes") > 0
                and _count(triangulation_stats, "rx_bytes") > 0,
                "校正三角测量 PL/DMA 有非零批次和字节统计"
                if triangulation_stats is not None
                and _count(triangulation_stats, "batches") > 0
                else "报告选择了校正三角测量 PL/DMA，但缺少非零运行统计",
                )
            )
    elif triangulation_accelerator == "verilator":
        triangulation_stats = _optional_mapping(
            avatar.get("triangulation_backend_stats") if avatar else None
        )
        checks.append(
            AcceleratedEvidenceCheck(
                "rectified_triangulation_rtl_bridge_completed",
                _count(triangulation_stats, "batches") > 0
                and _count(triangulation_stats, "input_beats") > 0
                and _count(triangulation_stats, "tx_bytes") > 0
                and _count(triangulation_stats, "rx_bytes") > 0,
                "校正三角测量 Verilator RTL bridge 有非零批次和字节统计"
                if triangulation_stats is not None
                and _count(triangulation_stats, "batches") > 0
                else "报告选择了校正三角测量 Verilator bridge，但缺少非零运行统计",
            )
        )
    motion_stats_backend = selected.get("motion_stats")
    if motion_stats_backend in ("linux-dma", "linux-dma-proxy"):
        motion_stats_passed = (
            _count(motion_batch_stats, "batches") > 0
            and _count(motion_batch_stats, "sample_count") > 0
            and _count(motion_stats_backend_stats, "input_beats") > 0
            and _count(motion_stats_backend_stats, "tx_bytes") > 0
            and _count(motion_stats_backend_stats, "rx_bytes") > 0
            and motion_stats_backend_stats.get("backend") == motion_stats_backend
        )
        checks.append(
            AcceleratedEvidenceCheck(
                "motion_stats_pl_runtime_completed",
                motion_stats_passed,
                "Motion Stats PL/DMA 有非零批次、输入拍、收发字节，且后端名称匹配"
                if motion_stats_passed
                else "报告选择了 Motion Stats PL/DMA，但缺少匹配的非零运行统计",
            )
        )
    if native_vart is not None:
        checks.append(
            AcceleratedEvidenceCheck(
                "native_vart_capture_statistics",
                native_stats_ready,
                (
                    "原生 VART 统计完整：正面/侧面采集 "
                    f"{native_vart.get('front_capture_fps')} / "
                    f"{native_vart.get('side_capture_fps')} FPS，"
                    f"处理/端到端 {native_vart.get('pair_fps')} / "
                    f"{native_vart.get('e2e_pair_fps')} 对/秒"
                )
                if native_stats_ready
                else "原生 VART 统计缺少 frames、正/侧采集 FPS 或处理/端到端对率",
            )
        )
        pipeline_capture = native_vart.get("pipeline_capture")
        prefetched_pairs = native_vart.get("capture_prefetched_pairs")
        pipeline_metadata_valid = (
            pipeline_capture is None
            and prefetched_pairs is None
        ) or (
            isinstance(pipeline_capture, bool)
            and isinstance(prefetched_pairs, int)
            and not isinstance(prefetched_pairs, bool)
            and prefetched_pairs >= 0
        )
        if pipeline_capture is None and prefetched_pairs is None:
            pipeline_detail = "旧版原生 VART 报告未记录采集预取字段，不影响已有证据"
        elif pipeline_metadata_valid:
            mode = "启用" if pipeline_capture else "关闭"
            pipeline_detail = (
                f"原生 VART 单对采集预取{mode}，已启动预取对数 {prefetched_pairs}"
            )
        else:
            pipeline_detail = (
                "原生 VART 采集预取字段类型错误；应为布尔开关和非负整数计数"
            )
        checks.append(
            AcceleratedEvidenceCheck(
                "native_vart_capture_pipeline_metadata",
                pipeline_metadata_valid,
                pipeline_detail,
            )
        )
        fourcc_passed, fourcc_detail = _native_vart_fourcc_check(
            native_vart, configuration
        )
        checks.append(
            AcceleratedEvidenceCheck(
                "native_vart_negotiated_fourcc",
                fourcc_passed,
                fourcc_detail,
            )
        )
    if avatar_dispatch is not None:
        checks.append(
            AcceleratedEvidenceCheck(
                "avatar_dispatch_health",
                avatar_dispatch_ok,
                "异步数字人调度器正常退出，允许只保留最新同步对"
                if avatar_dispatch_ok
                else "异步数字人调度器失败或提交/处理计数不一致",
            )
        )
        checks.append(
            AcceleratedEvidenceCheck(
                "avatar_content_freshness_measured",
                avatar_freshness_ok,
                "异步数字人已记录实际处理率和输入姿态到处理完成的内容年龄"
                if avatar_freshness_ok
                else "异步数字人缺少完整处理率或内容年龄证据",
            )
        )
    if display_dispatch is not None:
        checks.append(
            AcceleratedEvidenceCheck(
                "display_dispatch_health",
                display_dispatch_ok,
                "异步 HDMI 调度器正常退出，允许丢弃旧画布"
                if display_dispatch_ok
                else "异步 HDMI 调度器失败或提交/渲染计数不一致",
            )
        )
        checks.append(
            AcceleratedEvidenceCheck(
                "display_content_freshness_measured",
                display_freshness_ok,
                (
                    "HDMI 实际呈现率和内容年龄分布完整，且无未来/缺失时间戳"
                    if display_freshness_ok
                    else "异步 HDMI 缺少完整的呈现率、内容年龄或时间戳覆盖证据"
                ),
            )
        )
    if pl_golden_evidence_path is not None:
        golden = _read_json_object(pl_golden_evidence_path)
        golden_boot_id = normalize_linux_boot_id(golden.get("linux_boot_id"))
        joint_golden = _optional_mapping(golden.get("joint_metrics"))
        avatar_golden = _optional_mapping(golden.get("avatar_skinning"))
        rectified_golden = _optional_mapping(golden.get("rectified_triangulation"))
        motion_stats_golden = _optional_mapping(golden.get("motion_stats"))
        acceptance = golden.get("acceptance")
        dual_golden = acceptance == "posepriv_dual_pl_dma_bit_exact"
        triple_golden = acceptance == "posepriv_triple_pl_dma_bit_exact"
        golden_passed = (
            (dual_golden or triple_golden)
            and golden.get("bit_exact") is True
            and _count(joint_golden, "transactions") > 0
            and _count(avatar_golden, "transactions") > 0
            and (
                not triple_golden
                or _count(rectified_golden, "transactions") > 0
            )
        )
        checks.append(
            AcceleratedEvidenceCheck(
                "dual_pl_bit_exact_golden",
                golden_passed,
                "两套 PL 已有连续位精确黄金对拍证据"
                if golden_passed
                else "双 PL 黄金证据缺失、格式错误或没有成功事务",
            )
        )
        same_boot_session = (
            linux_boot_id is not None
            and golden_boot_id is not None
            and golden_boot_id == linux_boot_id
        )
        checks.append(
            AcceleratedEvidenceCheck(
                "pl_golden_same_boot_session",
                same_boot_session,
                f"实时闭环与 PL 黄金对拍来自同一 boot ID {linux_boot_id}"
                if same_boot_session
                else "实时闭环与 PL 黄金对拍缺少 boot ID 或来自不同启动会话",
            )
        )
        if "motion_stats_bit_exact" in golden:
            motion_stats_passed = (
                golden.get("motion_stats_bit_exact") is True
                and _count(motion_stats_golden, "transactions") > 0
                and _count(motion_stats_golden, "rx_bytes") > 0
            )
            checks.append(
                AcceleratedEvidenceCheck(
                    "motion_stats_pl_bit_exact_golden",
                    motion_stats_passed,
                    "Motion Stats PL 已有批次位精确黄金对拍证据"
                    if motion_stats_passed
                    else "Motion Stats PL 黄金证据缺失、失败或没有有效事务",
                )
            )
    if deployment_readiness_path is not None:
        readiness_document = _read_json_object(
            deployment_readiness_path,
            label="部署预检证据",
        )
        readiness = _optional_mapping(readiness_document.get("readiness"))
        readiness_boot_id = normalize_linux_boot_id(
            readiness.get("linux_boot_id") if readiness is not None else None
        )
        readiness_checks = (
            readiness.get("checks") if readiness is not None else None
        )
        check_records = (
            readiness_checks
            if isinstance(readiness_checks, (list, tuple))
            else ()
        )
        ready_names = {
            item.get("name")
            for item in check_records
            if isinstance(item, Mapping) and item.get("ready") is True
        }
        required_names = {
            "dual_camera_devices",
            "framebuffer_output",
            "dual_pl_dma_channels",
            "dpu_model_file",
        }
        native_vart_selected = selected.get("pose") == "native_vart_b3136_jsonl"
        if native_vart_selected:
            required_names.add("native_vart_bridge")
        else:
            required_names.update(("vart_python_runtime", "movenet_xmodel_contract"))
        if selected.get("triangulation_accelerator") in (
            "linux-dma",
            "linux-dma-proxy",
        ):
            required_names.add("triple_pl_dma_channels")
        if pl_golden_evidence_path is not None:
            golden_for_readiness = _read_json_object(pl_golden_evidence_path)
            if "motion_stats_bit_exact" in golden_for_readiness:
                required_names.add("quad_pl_dma_channels")
        readiness_passed = (
            readiness is not None
            and readiness.get("highest_stage") == "dpu_pl_integration_candidate"
            and required_names.issubset(ready_names)
        )
        missing = sorted(required_names - ready_names)
        checks.append(
            AcceleratedEvidenceCheck(
                "deployment_readiness_confirmed",
                readiness_passed,
                "联合镜像预检确认双摄、DPU、双 PL 与 framebuffer 均可用"
                if readiness_passed
                else "联合镜像预检未完成：" + ("、".join(missing) or "阶段不匹配"),
            )
        )
        readiness_same_boot_session = (
            linux_boot_id is not None
            and readiness_boot_id is not None
            and readiness_boot_id == linux_boot_id
        )
        checks.append(
            AcceleratedEvidenceCheck(
                "deployment_readiness_same_boot_session",
                readiness_same_boot_session,
                f"实时闭环与部署预检来自同一 boot ID {linux_boot_id}"
                if readiness_same_boot_session
                else "实时闭环与部署预检缺少 boot ID 或来自不同启动会话",
            )
        )
    if dpu_pose_comparison_path is not None:
        comparison = _read_json_object(
            dpu_pose_comparison_path,
            label="DPU 静态姿态对照证据",
        )
        comparable = _count(comparison, "comparable_keypoints")
        minimum = _count(comparison, "minimum_comparable_keypoints")
        semantic_passed = (
            comparison.get("comparison") == "movenet_static_probe"
            and comparison.get("baseline_backend") != "vart_dpu"
            and comparison.get("candidate_backend") == "vart_dpu"
            and comparison.get("within_tolerance") is True
            and minimum > 0
            and comparable >= minimum
        )
        checks.append(
            AcceleratedEvidenceCheck(
                "dpu_pose_semantics_match_baseline",
                semantic_passed,
                f"DPU 与 CPU 基线可比较关键点 {comparable}/{minimum}，"
                f"最大归一化位置误差 {comparison.get('max_position_distance')}"
                if semantic_passed
                else "DPU 静态图像关键点未通过 CPU 基线语义对照",
            )
        )
    checks_tuple = tuple(checks)
    passed = all(check.passed for check in checks_tuple)
    failed = [check.detail_zh for check in checks_tuple if not check.passed]
    next_action = (
        "联合运行证据完整；下一步依据同一报告评估 FPS、P95、资源和功耗。"
        if passed
        else "先补齐：" + "；".join(failed)
    )
    return AcceleratedSessionEvidence(
        passed,
        checks_tuple,
        next_action,
        linux_boot_id=linux_boot_id,
    )


def assess_core_joint_session_report(
    path: str | Path,
    *,
    joint_golden_evidence_path: str | Path | None = None,
    deployment_readiness_path: str | Path | None = None,
    dpu_pose_comparison_path: str | Path | None = None,
) -> AcceleratedSessionEvidence:
    """验收 DPU、一个关节 PL、reference 数字人和 HDMI 的最小核心闭环。"""

    base = assess_accelerated_session_report(
        path,
        dpu_pose_comparison_path=dpu_pose_comparison_path,
    )
    records = _read_jsonl(path)
    header = records[0]
    summary = next(
        item for item in records if item.get("record_type") == "session_summary"
    )
    configuration = _mapping(header.get("configuration"), "configuration")
    topology = _mapping(configuration.get("runtime_topology"), "runtime_topology")
    selected = _mapping(topology.get("selected"), "runtime_topology.selected")
    sync = _mapping(summary.get("pose_sync"), "pose_sync")
    avatar = _optional_mapping(summary.get("avatar_runtime"))
    avatar_dispatch = _optional_mapping(summary.get("avatar_dispatch"))
    pairs = _count(sync, "paired_frames")
    avatar_frames = _count(avatar, "frames")
    avatar_backend = selected.get("avatar_skinning")
    pose_backend = selected.get("pose")
    core_topology_passed = (
        pose_backend
        in ("vart_dpu", "vart_dpu_movenet_ntd", "native_vart_b3136_jsonl")
        and selected.get("joint_metrics") in ("linux-dma", "linux-dma-proxy")
        and avatar_backend in ("reference", "packet-reference")
        and selected.get("display") == "framebuffer"
        and selected.get("motion_features") == "reference"
        and selected.get("motion_stats") in ("disabled", "none", "reference")
        and selected.get("triangulation_accelerator") in ("disabled", "reference")
    )
    avatar_reference_passed = (
        avatar_backend in ("reference", "packet-reference")
        and pairs > 0
        and avatar_frames > 0
        and _avatar_dispatch_is_healthy(
            avatar_dispatch,
            pairs=pairs,
            avatar_frames=avatar_frames,
        )
    )
    excluded = {
        "unified_topology_selected",
        "avatar_pl_processed_pairs",
        "selected_backends_match_evidence",
    }
    checks = [check for check in base.checks if check.name not in excluded]
    checks.extend(
        (
            AcceleratedEvidenceCheck(
                "core_joint_topology_selected",
                core_topology_passed,
                "已选择 DPU、关节 PL、reference 数字人和 framebuffer，未选择其余 PL"
                if core_topology_passed
                else "核心档位后端不匹配：只允许 DPU + 关节 PL + reference 数字人 + framebuffer",
            ),
            AcceleratedEvidenceCheck(
                "avatar_reference_processed_pairs",
                avatar_reference_passed,
                f"reference 数字人处理 {avatar_frames} 帧；该结果不属于 PL 证据"
                if avatar_reference_passed
                else "reference 数字人未处理有效同步对，或异步调度统计不一致",
            ),
        )
    )
    if joint_golden_evidence_path is not None:
        golden = _read_json_object(
            joint_golden_evidence_path,
            label="关节 PL 黄金证据",
        )
        joint_golden = _optional_mapping(golden.get("joint_metrics"))
        joint_golden_passed = (
            golden.get("acceptance") == "posepriv_joint_pl_dma_bit_exact"
            and golden.get("bit_exact") is True
            and _count(joint_golden, "transactions") >= 2
            and _count(joint_golden, "tx_bytes") > 0
            and _count(joint_golden, "rx_bytes") > 0
        )
        checks.append(
            AcceleratedEvidenceCheck(
                "joint_pl_bit_exact_golden",
                joint_golden_passed,
                "关节 PL 已有至少两次交替事务的位精确黄金对拍证据"
                if joint_golden_passed
                else "关节 PL 黄金证据缺失、格式错误或有效事务不足",
            )
        )
        golden_boot_id = normalize_linux_boot_id(golden.get("linux_boot_id"))
        same_boot_session = (
            base.linux_boot_id is not None
            and golden_boot_id is not None
            and golden_boot_id == base.linux_boot_id
        )
        checks.append(
            AcceleratedEvidenceCheck(
                "joint_golden_same_boot_session",
                same_boot_session,
                f"核心闭环与关节 PL 黄金对拍来自同一 boot ID {base.linux_boot_id}"
                if same_boot_session
                else "核心闭环与关节 PL 黄金对拍缺少 boot ID 或来自不同启动会话",
            )
        )
    if deployment_readiness_path is not None:
        readiness_document = _read_json_object(
            deployment_readiness_path,
            label="核心部署预检证据",
        )
        readiness = _optional_mapping(readiness_document.get("readiness"))
        readiness_checks = (
            readiness.get("checks") if readiness is not None else None
        )
        check_records = (
            readiness_checks
            if isinstance(readiness_checks, (list, tuple))
            else ()
        )
        ready_names = {
            item.get("name")
            for item in check_records
            if isinstance(item, Mapping) and item.get("ready") is True
        }
        required_names = {
            "dual_camera_devices",
            "framebuffer_output",
            "joint_pl_dma_channels",
            "dpu_model_file",
        }
        if pose_backend == "native_vart_b3136_jsonl":
            required_names.add("native_vart_bridge")
        else:
            required_names.update(("vart_python_runtime", "movenet_xmodel_contract"))
        readiness_passed = (
            readiness is not None
            and readiness.get("profile") == "core-joint"
            and readiness.get("highest_stage")
            == "dpu_joint_pl_integration_candidate"
            and required_names.issubset(ready_names)
        )
        missing = sorted(required_names - ready_names)
        checks.append(
            AcceleratedEvidenceCheck(
                "core_joint_deployment_readiness_confirmed",
                readiness_passed,
                "核心镜像预检确认双摄、DPU、Joint PL 与 framebuffer 均可用"
                if readiness_passed
                else "核心镜像预检未完成："
                + ("、".join(missing) or "profile/阶段不匹配"),
            )
        )
        readiness_boot_id = normalize_linux_boot_id(
            readiness.get("linux_boot_id") if readiness is not None else None
        )
        readiness_same_boot_session = (
            base.linux_boot_id is not None
            and readiness_boot_id is not None
            and readiness_boot_id == base.linux_boot_id
        )
        checks.append(
            AcceleratedEvidenceCheck(
                "core_joint_deployment_readiness_same_boot_session",
                readiness_same_boot_session,
                f"核心闭环与部署预检来自同一 boot ID {base.linux_boot_id}"
                if readiness_same_boot_session
                else "核心闭环与部署预检缺少 boot ID 或来自不同启动会话",
            )
        )
    checks_tuple = tuple(checks)
    passed = all(check.passed for check in checks_tuple)
    failed = [check.detail_zh for check in checks_tuple if not check.passed]
    next_action = (
        "单 PL 核心闭环证据完整；reference 数字人不计作 PL，下一步才扩展其余通道。"
        if passed
        else "先补齐：" + "；".join(failed)
    )
    return AcceleratedSessionEvidence(
        passed,
        checks_tuple,
        next_action,
        linux_boot_id=base.linux_boot_id,
        assessment="core_joint_accelerated_session_evidence",
    )


def _avatar_dispatch_is_healthy(
    dispatch: Mapping[str, Any] | None,
    *,
    pairs: int,
    avatar_frames: int,
) -> bool:
    """验证异步数字人统计，同时允许明确记录的旧帧丢弃。"""

    if dispatch is None:
        return avatar_frames == pairs
    submitted = _count(dispatch, "submitted_pairs")
    processed = _count(dispatch, "processed_pairs")
    failed = dispatch.get("failed")
    return (
        pairs > 0
        and failed is False
        and submitted == pairs
        and 0 < processed <= submitted
        and avatar_frames == processed
    )


def _display_dispatch_is_healthy(
    dispatch: Mapping[str, Any] | None,
    *,
    pairs: int,
    display_frames: int,
) -> bool:
    """验证异步 HDMI 统计，允许显示端落后时丢弃旧画布。"""

    if dispatch is None:
        return display_frames == pairs
    submitted = _count(dispatch, "submitted_frames")
    rendered = _count(dispatch, "rendered_frames")
    failed = dispatch.get("failed")
    return (
        pairs > 0
        and failed is False
        and submitted == pairs
        and 0 < rendered <= submitted
        and display_frames == rendered
    )


def _display_freshness_is_complete(
    dispatch: Mapping[str, Any] | None,
    *,
    display_frames: int,
) -> bool:
    """异步 HDMI 必须为每个成功写屏帧提供同口径内容年龄。"""

    if dispatch is None or display_frames <= 0:
        return False
    content_age_frames = _count(dispatch, "content_age_frames")
    sample_count = _count(dispatch, "content_age_sample_count")
    window_size = _count(dispatch, "latency_window_size")
    mean_age = dispatch.get("mean_content_age_ms")
    p95_age = dispatch.get("p95_content_age_ms")
    max_age = dispatch.get("max_content_age_ms")
    return (
        dispatch.get("content_timestamp_semantics")
        == "newer_sync_adjusted_pose_timestamp_to_render_complete"
        and _positive_finite(dispatch.get("measurement_window_ms"))
        and _positive_finite(dispatch.get("observed_render_fps"))
        and content_age_frames == display_frames
        and window_size > 0
        and sample_count == min(content_age_frames, window_size)
        and _non_negative_finite(mean_age)
        and _non_negative_finite(p95_age)
        and _non_negative_finite(max_age)
        and float(mean_age) <= float(max_age)
        and float(p95_age) <= float(max_age)
        and _exact_zero_count(dispatch, "invalid_content_age_samples")
        and _exact_zero_count(dispatch, "missing_content_timestamp_frames")
    )


def _avatar_freshness_is_complete(
    dispatch: Mapping[str, Any] | None,
    *,
    avatar_frames: int,
) -> bool:
    """异步数字人必须为每个已处理同步对提供同口径内容年龄。"""

    if dispatch is None or avatar_frames <= 0:
        return False
    content_age_pairs = _count(dispatch, "content_age_pairs")
    sample_count = _count(dispatch, "content_age_sample_count")
    window_size = _count(dispatch, "latency_window_size")
    mean_age = dispatch.get("mean_content_age_ms")
    p95_age = dispatch.get("p95_content_age_ms")
    max_age = dispatch.get("max_content_age_ms")
    return (
        dispatch.get("content_timestamp_semantics")
        == "newer_sync_adjusted_pose_timestamp_to_avatar_process_complete"
        and _positive_finite(dispatch.get("measurement_window_ms"))
        and _positive_finite(dispatch.get("observed_process_fps"))
        and content_age_pairs == avatar_frames
        and window_size > 0
        and sample_count == min(content_age_pairs, window_size)
        and _non_negative_finite(mean_age)
        and _non_negative_finite(p95_age)
        and _non_negative_finite(max_age)
        and float(mean_age) <= float(max_age)
        and float(p95_age) <= float(max_age)
        and _exact_zero_count(dispatch, "invalid_content_age_samples")
        and _exact_zero_count(dispatch, "missing_content_timestamp_pairs")
    )


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"报告第 {line_number} 行不是有效 JSON") from exc
        if not isinstance(item, dict):
            raise ValueError(f"报告第 {line_number} 行必须是 JSON 对象")
        records.append(item)
    if not records or records[0].get("record_type") != "coach_session_header":
        raise ValueError("报告缺少 coach_session_header")
    return records


def _read_json_object(
    path: str | Path, *, label: str = "双 PL 黄金证据"
) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取{label}：{path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是 JSON 对象")
    return value


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"报告缺少 {name} 对象")
    return value


def _optional_mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _count(mapping: Mapping[str, Any] | None, key: str) -> int:
    if mapping is None:
        return 0
    value = mapping.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _exact_zero_count(mapping: Mapping[str, Any], key: str) -> bool:
    value = mapping.get(key)
    return isinstance(value, int) and not isinstance(value, bool) and value == 0


def _positive_bytes(mapping: Mapping[str, Any] | None, key: str) -> bool:
    return _count(mapping, key) > 0


def _non_negative_finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0.0
    )


def _positive_finite(value: object) -> bool:
    return _non_negative_finite(value) and float(value) > 0.0


def _native_vart_runtime_stats_ready(
    native_vart: Mapping[str, Any] | None,
) -> bool:
    if native_vart is None or _count(native_vart, "frames") < 1:
        return False
    return all(
        _positive_finite(native_vart.get(key))
        for key in (
            "front_capture_fps",
            "side_capture_fps",
            "pair_fps",
            "e2e_pair_fps",
        )
    )


def _native_vart_fourcc_check(
    native_vart: Mapping[str, Any], configuration: Mapping[str, Any]
) -> tuple[bool, str]:
    """Check actual UVC formats when the native probe recorded them.

    Empty/missing fields are an old-probe compatibility case rather than a
    failure.  A recorded non-empty format must match the requested FourCC so
    an accidental YUYV fallback is visible in the evidence report.
    """

    requested = configuration.get("capture_fourcc") or configuration.get(
        "requested_fourcc"
    )
    front = native_vart.get("front_fourcc")
    side = native_vart.get("side_fourcc")
    recorded = (front, side)
    if all(value is None or value == "" for value in recorded):
        return True, "原生探针未记录实际 FourCC；按旧版日志兼容，不据此宣称格式一致"
    if not isinstance(requested, str) or len(requested) != 4:
        return True, "原生探针已记录实际 FourCC，但报告头缺少可比较的请求格式"
    if not all(isinstance(value, str) and len(value) == 4 for value in recorded):
        return False, "原生探针实际 FourCC 字段不完整或格式非法"
    if front != requested or side != requested:
        return (
            False,
            f"请求 {requested}，实际正面/侧面为 {front}/{side}；请先检查 UVC 协商和 USB 带宽",
        )
    return True, f"请求与实际协商 FourCC 均为 {requested}"


def _native_camera_mode(
    native_vart: Mapping[str, Any] | None,
    configuration: Mapping[str, Any],
    view: str,
) -> Mapping[str, Any] | None:
    if native_vart is None:
        return None
    width = native_vart.get(f"{view}_width")
    height = native_vart.get(f"{view}_height")
    fps = native_vart.get(f"{view}_camera_fps")
    fourcc = configuration.get("capture_fourcc") or configuration.get("requested_fourcc")
    backend = configuration.get("capture_backend", "V4L2")
    mode = {
        "width": width,
        "height": height,
        "fps": fps,
        "fourcc": fourcc,
        "backend_name": backend,
        "source": "native_vart_stats",
    }
    return mode if _valid_camera_mode(mode) else None


def _percentage(value: object) -> bool:
    return _non_negative_finite(value) and float(value) <= 100.0


def _camera_mode_provenance_check(
    selection: Mapping[str, Any] | None,
    front_mode: Mapping[str, Any] | None,
    side_mode: Mapping[str, Any] | None,
    plan_source: object,
) -> tuple[bool, str]:
    """Verify that a selected probe mode is reflected by both negotiated streams.

    The probe chooses a requested mode, while V4L2 is allowed to report a
    slightly different delivered FPS.  Therefore this consistency check binds
    the selected width/height to the actual modes but deliberately does not
    turn FPS into a hidden performance threshold.
    """

    if selection is None:
        return False, "摄像头模式计划存在，但缺少合法的 selected_mode 记录"
    width = selection.get("width")
    height = selection.get("height")
    fps = selection.get("fps")
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or width <= 0
        or isinstance(height, bool)
        or not isinstance(height, int)
        or height <= 0
        or not _positive_finite(fps)
    ):
        return False, "selected_mode 的 width/height/fps 字段不合法"
    declared_spec = selection.get("spec")
    expected_spec = f"{width}x{height}@{float(fps):g}"
    if declared_spec is not None and declared_spec != expected_spec:
        return False, "selected_mode.spec 与 width/height/fps 不一致"
    if not _valid_camera_mode(front_mode) or not _valid_camera_mode(side_mode):
        return False, "模式计划存在，但缺少两路摄像头的实际协商模式"
    mismatches = []
    for view, mode in (("正面", front_mode), ("侧面", side_mode)):
        if mode is not None and (
            mode.get("width") != width or mode.get("height") != height
        ):
            mismatches.append(
                f"{view}实际 {mode.get('width')}x{mode.get('height')}"
            )
    if mismatches:
        return (
            False,
            f"模式计划 {expected_spec} 与协商模式不一致：" + "，".join(mismatches),
        )
    source = str(plan_source).strip() if plan_source is not None else "未记录路径"
    return (
        True,
        f"模式计划 {expected_spec} 已与正面/侧面实际分辨率一致（来源 {source}）；实际 FPS 仅作观测",
    )


def _valid_camera_mode(mode: Mapping[str, Any] | None) -> bool:
    if mode is None:
        return False
    width = mode.get("width")
    height = mode.get("height")
    fps = mode.get("fps")
    fourcc = mode.get("fourcc")
    backend = mode.get("backend_name")
    return (
        isinstance(width, int)
        and not isinstance(width, bool)
        and width > 0
        and isinstance(height, int)
        and not isinstance(height, bool)
        and height > 0
        and isinstance(fps, (int, float))
        and not isinstance(fps, bool)
        and fps > 0.0
        and isinstance(fourcc, str)
        and len(fourcc) == 4
        and isinstance(backend, str)
        and bool(backend.strip())
    )


def _camera_mode_text(mode: Mapping[str, Any] | None) -> str:
    if mode is None:
        return "缺失"
    return (
        f"{mode.get('width')}x{mode.get('height')} "
        f"{mode.get('fps')} FPS {mode.get('fourcc')} / {mode.get('backend_name')}"
    )
