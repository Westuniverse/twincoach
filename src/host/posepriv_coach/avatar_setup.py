"""实时、录像和关键点回放共用的数字人运行时组装入口。"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
import math
from pathlib import Path

from .avatar_calibration import AvatarCalibration, load_avatar_calibration
from .avatar_pipeline import AvatarFramePlanner
from .avatar_pipeline import TRIANGULATION_BACKENDS
from .avatar_runtime import AvatarRuntime
from .pose_avatar_preview import PoseAvatarPreviewPlanner
from .avatar_appearance import load_avatar_appearance
from .avatar_skinning import (
    PackedDmaAvatarSkinningBackend,
    ProgrammableReferenceVertexSkinningBackend,
    ReferencePacketAvatarTransport,
)
from .joint_filter import AdaptiveJointFilter
from .linux_shared_pose_dma import LinuxSharedPoseDma
from .linux_avatar_dma import (
    DeviceOpener,
    LinuxAvatarDmaProxyTransport,
    LinuxAvatarDmaTransport,
)
from .linux_rectified_dma import (
    LinuxRectifiedStereoDmaProxyTransport,
    LinuxRectifiedStereoDmaTransport,
)
from .mesh_asset import (
    AvatarMeshAsset,
    build_demo_detailed_humanoid_mesh_asset,
    build_demo_high_detail_humanoid_mesh_asset,
    build_coach_humanoid_mesh_asset,
    build_demo_humanoid_mesh_asset,
    load_avatar_mesh_asset,
)
from .verilator_avatar_transport import VerilatorAvatarSkinningTransport
from .verilator_transport import VerilatorAxisTransport
from .rectified_backend import (
    PackedDmaRectifiedStereoBackend,
    ReferenceRectifiedStereoBackend,
)


AVATAR_SKINNING_BACKENDS = (
    "reference",
    "packet-reference",
    "verilator",
    "linux-dma",
    "linux-dma-proxy",
)
AVATAR_DETAIL_LEVELS = ("low", "detailed", "high")
RECTIFIED_ACCELERATOR_BACKENDS = (
    "reference",
    "verilator",
    "linux-dma",
    "linux-dma-proxy",
    "linux-shared-dma",
)


@dataclass(frozen=True, slots=True)
class AvatarRuntimeSetup:
    """组装完成的运行时及其固定网格，供显示和报告共同引用。"""

    runtime: AvatarRuntime
    mesh_asset: AvatarMeshAsset
    calibration: AvatarCalibration | None
    skinning_backend: str
    smoothing_enabled: bool
    transport: object | None = None
    triangulation_transport: object | None = None

    def close(self) -> None:
        closed: set[int] = set()
        for resource in (self.transport, self.triangulation_transport):
            if resource is None or id(resource) in closed:
                continue
            close = getattr(resource, "close", None)
            if callable(close):
                close()
                closed.add(id(resource))

    @property
    def calibration_quality_report(self) -> dict[str, object] | None:
        quality = self.calibration.quality if self.calibration is not None else None
        return None if quality is None else quality.as_report_mapping()


def load_avatar_preview_setup(
    *, mesh_path: str | Path | None = None, detail: str = "high", appearance_path: str | Path | None = None,
    shared_dma: object | None = None, skinning_validator=None, native_upper64_scan: bool = False,
) -> AvatarRuntimeSetup:
    """Pose preview; optional shared DMA is borrowed, with no CPU fallback."""
    if type(native_upper64_scan) is not bool:
        raise ValueError('native_upper64_scan must be boolean')
    upper64_check=None
    if native_upper64_scan:
        if shared_dma is None or skinning_validator is None:
            raise ValueError('native upper64 scan requires explicit shared DMA and native validator')
        skinning_validator.require_upper64_scan()
        upper64_check=skinning_validator.validate_upper64
    transfer=None
    if shared_dma is not None:
        from .shared_skinning_batches import BoundedSkinningPacketTransfer
        transfer=BoundedSkinningPacketTransfer(
            getattr(shared_dma,'exchange_skinning_packet',None),validator=skinning_validator)
    elif skinning_validator is not None:
        raise ValueError('skinning validator requires explicit shared DMA')
    if detail not in AVATAR_DETAIL_LEVELS:
        raise ValueError(f"unsupported avatar detail level: {detail}")
    mesh_asset = (load_avatar_mesh_asset(mesh_path) if mesh_path is not None else {
        "low": build_demo_humanoid_mesh_asset,
        "detailed": build_demo_detailed_humanoid_mesh_asset,
        "high": build_coach_humanoid_mesh_asset,
    }[detail]())
    if appearance_path is not None:
        if mesh_path is not None:
            raise ValueError("choose an appearance profile or an external mesh")
        mesh_asset = build_coach_humanoid_mesh_asset(load_avatar_appearance(appearance_path))
    transport = None
    backend_name='linux-shared-dma'
    if transfer is None:
        transport=ReferencePacketAvatarTransport(numpy_warmup_vertex_count=mesh_asset.vertex_count)
        transfer=transport.transfer
        backend_name='packet-reference'
    runtime = AvatarRuntime(
        PoseAvatarPreviewPlanner(hand_bones=any(n in mesh_asset.bone_names for n in ('left_hand','right_hand'))),
        PackedDmaAvatarSkinningBackend(transfer,upper64_check=upper64_check),
        mesh_asset=mesh_asset, matrix_hold_frames=0,
    )
    runtime.display.camera.yaw_degrees = 30.0
    return AvatarRuntimeSetup(runtime, mesh_asset, None, backend_name, True, transport)


def load_avatar_runtime_setup(
    calibration_path: str | Path,
    *,
    mesh_path: str | Path | None = None,
    detail: str = "detailed",
    skinning_backend: str = "packet-reference",
    smoothing: bool = True,
    world_to_model_scale: float = 1.0,
    minimum_ray_angle_degrees: float = 1.0,
    triangulation_backend: str = "general",
    rectified_baseline: float | None = None,
    rectified_focal: float | None = None,
    rectified_minimum_disparity: float = 64.0 / (1 << 14),
    rectified_maximum_vertical_disparity: float = 0.02,
    verilator_executable: str | Path | None = None,
    verilator_work_dir: str | Path | None = None,
    matrix_hold_frames: int = 6,
    dma_device: str | Path | None = None,
    dma_device_opener: DeviceOpener | None = None,
    dma_proxy_tx_device: str | Path | None = None,
    dma_proxy_rx_device: str | Path | None = None,
    rectified_accelerator_backend: str = "reference",
    rectified_verilator_executable: str | Path | None = None,
    rectified_verilator_work_dir: str | Path | None = None,
    rectified_dma_device: str | Path | None = None,
    rectified_dma_device_opener: DeviceOpener | None = None,
    rectified_dma_proxy_tx_device: str | Path | None = None,
    rectified_dma_proxy_rx_device: str | Path | None = None,
    shared_dma: LinuxSharedPoseDma | None = None,
) -> AvatarRuntimeSetup:
    """从文件配置构建所有应用入口语义一致的 ``AvatarRuntime``。"""

    if detail not in AVATAR_DETAIL_LEVELS:
        raise ValueError(f"unsupported avatar detail level: {detail}")
    if skinning_backend not in AVATAR_SKINNING_BACKENDS:
        raise ValueError(f"unsupported avatar skinning backend: {skinning_backend}")
    if triangulation_backend not in TRIANGULATION_BACKENDS:
        raise ValueError(
            f"unsupported avatar triangulation backend: {triangulation_backend}"
        )
    if rectified_accelerator_backend not in RECTIFIED_ACCELERATOR_BACKENDS:
        raise ValueError(
            "unsupported rectified accelerator backend: "
            f"{rectified_accelerator_backend}"
        )
    if (
        rectified_accelerator_backend != "reference"
        and triangulation_backend != "rectified-reference"
    ):
        raise ValueError(
            "a Linux rectified accelerator requires triangulation_backend="
            "rectified-reference"
        )
    if rectified_accelerator_backend == "linux-shared-dma" and shared_dma is None:
        raise ValueError("linux-shared-dma requires an application-owned shared_dma transport")
    if world_to_model_scale <= 0.0:
        raise ValueError("avatar world_to_model_scale must be positive")
    calibration = load_avatar_calibration(calibration_path)
    mesh_asset = (
        load_avatar_mesh_asset(mesh_path)
        if mesh_path is not None
        else (
            build_demo_humanoid_mesh_asset()
            if detail == "low"
            else build_demo_detailed_humanoid_mesh_asset()
            if detail == "detailed"
            else build_demo_high_detail_humanoid_mesh_asset()
        )
    )
    transport: object | None = None
    triangulation_transport: object | None = None
    if skinning_backend == "verilator":
        if verilator_executable is None or verilator_work_dir is None:
            raise ValueError(
                "verilator avatar backend requires executable and work directory"
            )
        transport = VerilatorAvatarSkinningTransport(
            Path(verilator_executable),
            Path(verilator_work_dir),
        )
        backend = PackedDmaAvatarSkinningBackend(transport.transfer)
    elif skinning_backend == "linux-dma":
        if dma_device is None:
            raise ValueError("linux-dma avatar backend requires dma_device")
        transport = LinuxAvatarDmaTransport(
            dma_device,
            **({"opener": dma_device_opener} if dma_device_opener is not None else {}),
        )
        backend = PackedDmaAvatarSkinningBackend(transport.transfer)
    elif skinning_backend == "linux-dma-proxy":
        if dma_proxy_tx_device is None or dma_proxy_rx_device is None:
            raise ValueError(
                "linux-dma-proxy avatar backend requires TX and RX devices"
            )
        transport = LinuxAvatarDmaProxyTransport(
            dma_proxy_tx_device,
            dma_proxy_rx_device,
        )
        backend = PackedDmaAvatarSkinningBackend(transport.transfer)
    elif skinning_backend == "packet-reference":
        transport = ReferencePacketAvatarTransport(
            numpy_warmup_vertex_count=mesh_asset.vertex_count,
        )
        backend = PackedDmaAvatarSkinningBackend(transport.transfer)
    else:
        backend = ProgrammableReferenceVertexSkinningBackend()
    triangulation_backend_obj = None
    if triangulation_backend == "rectified-reference":
        minimum_disparity_raw = max(
            1, math.ceil(rectified_minimum_disparity * (1 << 14))
        )
        minimum_confidence_raw = max(0, math.ceil(0.35 * 255.0))
        if rectified_accelerator_backend == "reference":
            triangulation_backend_obj = ReferenceRectifiedStereoBackend(
                minimum_disparity=minimum_disparity_raw,
                minimum_confidence=minimum_confidence_raw,
            )
        elif rectified_accelerator_backend == "verilator":
            if rectified_verilator_executable is None:
                raise ValueError(
                    "verilator rectified backend requires rectified_verilator_executable"
                )
            verilator_work_dir_path = (
                Path(rectified_verilator_work_dir)
                if rectified_verilator_work_dir is not None
                else Path.cwd() / "tmp"
            )
            triangulation_transport = VerilatorAxisTransport(
                Path(rectified_verilator_executable),
                verilator_work_dir_path,
            )
            triangulation_backend_obj = PackedDmaRectifiedStereoBackend(
                triangulation_transport.transfer,
                close_callback=None,
                backend_name="verilator",
            )
        elif rectified_accelerator_backend == "linux-shared-dma":
            # Borrow the application's single transport. AvatarRuntimeSetup
            # must not close it while joint/summary clients can still use it.
            triangulation_backend_obj = PackedDmaRectifiedStereoBackend(
                partial(shared_dma.exchange_bytes, 2), backend_name="linux-shared-dma",
            )
        elif rectified_accelerator_backend == "linux-dma":
            if rectified_dma_device is None:
                raise ValueError(
                    "linux-dma rectified backend requires rectified_dma_device"
                )
            triangulation_transport = LinuxRectifiedStereoDmaTransport(
                rectified_dma_device,
                **(
                    {"opener": rectified_dma_device_opener}
                    if rectified_dma_device_opener is not None
                    else {}
                ),
            )
            triangulation_backend_obj = PackedDmaRectifiedStereoBackend(
                triangulation_transport.transfer,
                close_callback=triangulation_transport.close,
                backend_name="linux-dma",
            )
        else:
            if (
                rectified_dma_proxy_tx_device is None
                or rectified_dma_proxy_rx_device is None
            ):
                raise ValueError(
                    "linux-dma-proxy rectified backend requires TX and RX devices"
                )
            triangulation_transport = LinuxRectifiedStereoDmaProxyTransport(
                rectified_dma_proxy_tx_device,
                rectified_dma_proxy_rx_device,
            )
            triangulation_backend_obj = PackedDmaRectifiedStereoBackend(
                triangulation_transport.transfer,
                close_callback=triangulation_transport.close,
                backend_name="linux-dma-proxy",
            )
    runtime = AvatarRuntime(
        AvatarFramePlanner(
            calibration.to_pipeline_config(
                world_to_model_scale=world_to_model_scale,
                minimum_ray_angle_degrees=minimum_ray_angle_degrees,
                triangulation_backend=triangulation_backend,
                rectified_baseline=rectified_baseline,
                rectified_focal=rectified_focal,
                rectified_minimum_disparity=rectified_minimum_disparity,
                rectified_maximum_vertical_disparity=rectified_maximum_vertical_disparity,
                rectified_backend=triangulation_backend_obj,
            ),
            joint_filter=(AdaptiveJointFilter() if smoothing else None),
        ),
        backend,
        mesh_asset=mesh_asset,
        matrix_hold_frames=matrix_hold_frames,
    )
    return AvatarRuntimeSetup(
        runtime=runtime,
        mesh_asset=mesh_asset,
        calibration=calibration,
        skinning_backend=skinning_backend,
        smoothing_enabled=smoothing,
        transport=transport,
        triangulation_transport=triangulation_transport,
    )


__all__ = [
    "AVATAR_DETAIL_LEVELS",
    "AVATAR_SKINNING_BACKENDS",
    "RECTIFIED_ACCELERATOR_BACKENDS",
    "AvatarRuntimeSetup",
    "load_avatar_runtime_setup",
    "load_avatar_preview_setup",
]
