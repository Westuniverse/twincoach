"""把板端设备清点结果转换为可解释的联合部署阶段。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .linux_devices import LinuxPlatformInventory


DEPLOYMENT_READINESS_PROFILES = ("unified", "core-joint")


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: str
    ready: bool
    detail: str


@dataclass(frozen=True, slots=True)
class DeploymentReadiness:
    checks: tuple[ReadinessCheck, ...]
    highest_stage: str
    next_action: str
    linux_boot_id: str | None = None
    profile: str = "unified"

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "profile": self.profile,
            "linux_boot_id": self.linux_boot_id,
            "highest_stage": self.highest_stage,
            "checks": tuple(asdict(item) for item in self.checks),
            "next_action": self.next_action,
        }


def evaluate_deployment_readiness(
    inventory: LinuxPlatformInventory,
    *,
    xmodel: Path | None = None,
    vart_importable: bool = False,
    xmodel_contract_valid: bool | None = None,
    xmodel_contract_detail: str | None = None,
    model_package_valid: bool | None = None,
    model_package_detail: str | None = None,
    native_vart_probe: Path | None = None,
    linux_boot_id: str | None = None,
    profile: str = "unified",
) -> DeploymentReadiness:
    """给出能力分层，不把“设备存在”误写成端到端性能通过。"""

    if profile not in DEPLOYMENT_READINESS_PROFILES:
        raise ValueError(f"unknown deployment readiness profile: {profile}")
    stable_camera_count = len(inventory.stable_video_devices)
    dual_camera = stable_camera_count >= 2
    display = bool(
        inventory.preferred_framebuffer
        and inventory.preferred_framebuffer.rgb565_compatible
        and inventory.preferred_framebuffer.writable
    )
    joint_pl_channels = inventory.joint_accel_dma_proxy_ready
    joint_channel_count = sum(
        Path(path).name in {"posepriv_joint_tx", "posepriv_joint_rx"}
        for path in inventory.dma_proxy_channels
    )
    pl_channels = inventory.dual_accel_dma_proxy_ready
    triple_pl_channels = inventory.triple_accel_dma_proxy_ready
    quad_pl_channels = inventory.quad_accel_dma_proxy_ready
    model_exists = xmodel is not None and xmodel.is_file()
    contract_valid = xmodel_contract_valid is True
    native_probe_ready = native_vart_probe is not None and native_vart_probe.is_file()
    package_ready = model_package_valid is not False
    dpu_runtime = model_exists and package_ready and (
        (vart_importable and contract_valid) or native_probe_ready
    )

    checks = [
        ReadinessCheck(
            "dual_camera_devices",
            dual_camera,
            f"发现 {stable_camera_count} 个唯一 video-index0 采集设备，需要至少 2 个",
        ),
        ReadinessCheck(
            "framebuffer_output",
            display,
            "RGB565 framebuffer 可写" if display else "未发现可写 RGB565 framebuffer",
        ),
        ReadinessCheck(
            "joint_pl_dma_channels",
            joint_pl_channels,
            f"发现 {joint_channel_count}/2 条 Joint dma-proxy 通道",
        ),
        ReadinessCheck(
            "dual_pl_dma_channels",
            pl_channels,
            f"发现 {len(inventory.dma_proxy_channels)}/4 条 PosePriv dma-proxy 通道",
        ),
        ReadinessCheck(
            "triple_pl_dma_channels",
            triple_pl_channels,
            f"检测到 {len(inventory.dma_proxy_channels)}/6 条 Avatar/Joint/Rectified dma-proxy 通道",
        ),
        ReadinessCheck(
            "quad_pl_dma_channels",
            quad_pl_channels,
            f"detected {len(inventory.dma_proxy_channels)}/8 Avatar/Joint/Rectified/Motion Stats dma-proxy channels",
        ),
        ReadinessCheck(
            "dpu_model_file",
            model_exists,
            str(xmodel) if model_exists else "未提供可读的 .xmodel 文件",
        ),
        ReadinessCheck(
            "vart_python_runtime",
            vart_importable,
            "VART Python 模块可导入" if vart_importable else "VART Python 模块不可导入",
        ),
        ReadinessCheck(
            "movenet_xmodel_contract",
            contract_valid,
            xmodel_contract_detail
            or (
                "尚未检查 XModel Runner 张量契约"
                if model_exists and vart_importable
                else "需要先提供模型并加载 VART/XIR"
            ),
        ),
    ]
    if model_package_valid is not None:
        checks.append(
            ReadinessCheck(
                "movenet_model_package",
                model_package_valid,
                model_package_detail or "模型包静态契约通过",
            )
        )
    if native_vart_probe is not None:
        checks.append(
            ReadinessCheck(
                "native_vart_bridge",
                native_probe_ready,
                (
                    f"原生 VART 双摄桥可执行文件存在：{native_vart_probe}"
                    if native_probe_ready
                    else f"原生 VART 双摄桥不存在：{native_vart_probe}"
                ),
            )
        )

    required_pl_channels = joint_pl_channels if profile == "core-joint" else pl_channels
    if dual_camera and display and required_pl_channels and dpu_runtime:
        if profile == "core-joint":
            stage = "dpu_joint_pl_integration_candidate"
            next_action = "运行单路 Joint DMA 黄金对拍和 DPU+Joint PL+reference 数字人 HDMI 闭环"
        else:
            stage = "dpu_pl_integration_candidate"
            next_action = "运行双摄真实读帧、模型契约检查、两套 DMA 黄金对拍和持续 HDMI 验收"
    elif dual_camera and display and required_pl_channels:
        if profile == "core-joint":
            stage = "cpu_joint_pl_integration_candidate"
            next_action = "先保留 Joint PL 通道，同时补齐与实际 DPU 架构匹配的 .xmodel"
        else:
            stage = "cpu_pl_integration_candidate"
            next_action = "先完成 CPU+PL 联合闭环，同时准备与实际 DPU 架构匹配的 .xmodel"
    elif dual_camera and display:
        stage = "camera_display_candidate"
        next_action = (
            "先完成双摄标定与 HDMI 实时显示，再加载两条 Joint dma-proxy 通道"
            if profile == "core-joint"
            else "先完成双摄标定与 HDMI 实时显示，再加载四条 dma-proxy 通道"
        )
    else:
        stage = "inventory_incomplete"
        next_action = "优先补齐双摄稳定路径和可写 HDMI framebuffer，再继续加速器联合验收"

    return DeploymentReadiness(
        checks=tuple(checks),
        highest_stage=stage,
        next_action=next_action,
        linux_boot_id=linux_boot_id,
        profile=profile,
    )
