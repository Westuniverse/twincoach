"""生成一次实时会话的异构运行拓扑证据，不探测或宣称硬件通过。"""

from __future__ import annotations

from typing import Any


_PL_BACKENDS = frozenset(("linux-dma", "linux-dma-proxy"))
_DPU_POSE_BACKENDS = frozenset(
    ("vart_dpu", "vart_dpu_movenet_ntd", "native_vart_b3136_jsonl", "vart_native_four_head_worker")
)


def build_runtime_topology_record(
    *,
    pose_backend: str,
    joint_backend: str,
    avatar_backend: str | None,
    display_backend: str,
    motion_backend: str = "reference",
    motion_stats_backend: str | None = None,
    motion_summary_backend: str | None = None,
    triangulation_backend: str | None = None,
    triangulation_accelerator_backend: str | None = None,
) -> dict[str, Any]:
    """记录已选择的执行位置，并列出联合加速候选仍缺少的阶段。

    ``unified_accelerated_candidate`` 只表示命令行选择覆盖四个目标阶段；它不证明
    设备节点、DMA 数据、DPU 推理、HDMI 帧率或端到端延迟已经通过。
    """

    values = (pose_backend, joint_backend, display_backend, motion_backend)
    if any(not value for value in values):
        raise ValueError("runtime topology backend names must not be empty")
    if motion_stats_backend == "":
        raise ValueError("motion stats backend name must not be empty")
    if motion_summary_backend == "":
        raise ValueError("motion summary backend name must not be empty")
    if (
        motion_summary_backend not in (None, "none")
        and motion_stats_backend not in (None, "none")
    ):
        raise ValueError("motion summary and raw motion stats are mutually exclusive")
    if (
        triangulation_backend is None
    ) != (triangulation_accelerator_backend is None):
        raise ValueError(
            "triangulation geometry and accelerator backends must be provided together"
        )
    if triangulation_backend == "":
        raise ValueError("triangulation geometry backend must not be empty")
    if triangulation_accelerator_backend == "":
        raise ValueError("triangulation accelerator backend must not be empty")
    dpu_selected = pose_backend in _DPU_POSE_BACKENDS
    joint_pl_selected = joint_backend in _PL_BACKENDS or joint_backend == "linux-shared-dma"
    summary_pl_selected = motion_summary_backend in _PL_BACKENDS or motion_summary_backend == "linux-shared-dma"
    triangulation_pl_selected = triangulation_accelerator_backend in _PL_BACKENDS or triangulation_accelerator_backend == "linux-shared-dma"
    avatar_pl_selected = avatar_backend in _PL_BACKENDS
    hdmi_selected = display_backend == "framebuffer"
    selected = {
        "pose": pose_backend,
        "joint_metrics": joint_backend,
        "avatar_skinning": avatar_backend or "disabled",
        "display": display_backend,
        "motion_features": motion_backend,
        "motion_stats": motion_stats_backend or "disabled",
        "motion_summary": motion_summary_backend or "disabled",
        "triangulation_geometry": triangulation_backend or "disabled",
        "triangulation_accelerator": triangulation_accelerator_backend or "disabled",
    }
    missing = tuple(
        name
        for name, present in (
            ("dpu_pose", dpu_selected),
            ("joint_metrics_pl", joint_pl_selected),
            ("avatar_skinning_pl", avatar_pl_selected),
            ("hdmi_framebuffer", hdmi_selected),
        )
        if not present
    )
    complete_missing = list(missing)
    if motion_backend not in _PL_BACKENDS:
        complete_missing.append("motion_features_pl")
    if motion_stats_backend is not None and motion_stats_backend not in _PL_BACKENDS:
        complete_missing.append("motion_stats_pl")
    if (
        motion_summary_backend not in (None, "none")
        and not summary_pl_selected
    ):
        complete_missing.append("motion_summary_pl")
    if triangulation_backend == "rectified-reference" and not (
        triangulation_pl_selected
    ):
        complete_missing.append("rectified_triangulation_pl")
    elif triangulation_backend == "general" and avatar_backend is not None:
        complete_missing.append("general_triangulation_cpu")
    return {
        "selected": selected,
        "dpu_selected": dpu_selected,
        "joint_pl_selected": joint_pl_selected,
        "avatar_pl_selected": avatar_pl_selected,
        "hdmi_selected": hdmi_selected,
        "motion_pl_selected": motion_backend in _PL_BACKENDS,
        "motion_stats_pl_selected": motion_stats_backend in _PL_BACKENDS,
        "motion_summary_pl_selected": summary_pl_selected,
        "triangulation_pl_selected": triangulation_pl_selected,
        "full_accelerated_candidate": not complete_missing,
        "missing_full_accelerated_stages": complete_missing,
        "unified_accelerated_candidate": not missing,
        "missing_accelerated_stages": list(missing),
        "evidence_scope": "configuration_only_until_session_summary",
    }
