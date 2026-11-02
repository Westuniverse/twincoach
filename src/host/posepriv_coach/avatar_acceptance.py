"""数字人资产从固定点网格到显示投影的离线闭环验收。"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic_ns

from .avatar_display import AvatarDisplayModel
from .avatar_pipeline import AvatarFrameResult
from .avatar_skinning import PackedDmaAvatarSkinningBackend, ReferencePacketAvatarTransport
from .display_probe import build_synthetic_avatar_plan, build_synthetic_display_sample
from .mesh_asset import AvatarMeshAsset


@dataclass(frozen=True, slots=True)
class AvatarPacketAcceptance:
    """一次完整主机参考闭环的可记录结果。"""

    asset_name: str
    vertex_count: int
    triangle_count: int
    packet_tx_bytes: int
    packet_rx_bytes: int
    repetitions: int
    elapsed_ns: int


def verify_avatar_packet_pipeline(
    asset: AvatarMeshAsset,
    *,
    repetitions: int = 1,
) -> AvatarPacketAcceptance:
    """贯通资产、矩阵、包格式、蒙皮、投影与相机交互。

    该函数只产生主机 packet-reference 正确性证据，不代表板端 DMA 性能。
    """

    if repetitions <= 0:
        raise ValueError("repetitions 必须为正数")
    sample = build_synthetic_display_sample(0, 1_000_000_000)
    plan = build_synthetic_avatar_plan(sample, asset)
    transport = ReferencePacketAvatarTransport()
    backend = PackedDmaAvatarSkinningBackend(transport.transfer)
    display = AvatarDisplayModel()
    start_ns = monotonic_ns()
    result = None
    for _ in range(repetitions):
        result = AvatarFrameResult(
            plan=plan,
            output_beats=plan.execute(
                backend,
                program_matrices=backend.program_matrices,
            ),
        )
    assert result is not None
    display.update(result, asset)
    state = display.snapshot(width=960, height=540)
    baseline_segments = state.projected_segments
    display.rotate(37.0, -12.0)
    display.zoom_by(1.2)
    display.pan(9.0, -5.0)
    moved_state = display.snapshot(width=960, height=540)
    if moved_state.projected_segments == baseline_segments:
        raise RuntimeError("相机交互没有改变数字人投影")
    # Back-face culling is view dependent: rotating a single-sided asset may
    # legitimately expose a different subset of its source triangles.  The
    # interaction contract is that the mesh remains non-empty and never grows
    # beyond the source topology, not that the culled count stays constant.
    if not moved_state.projected_triangles:
        raise RuntimeError("相机交互后网格投影不应为空")
    if len(moved_state.projected_triangles) > asset.triangle_count:
        raise RuntimeError("相机交互后投影三角形数量超过资产拓扑")
    if moved_state.skin_vertex_count != state.skin_vertex_count:
        raise RuntimeError("相机交互不应改变蒙皮顶点数量")
    display.reset_camera()
    if display.snapshot(width=960, height=540).projected_segments != baseline_segments:
        raise RuntimeError("相机复位没有恢复原始投影")
    elapsed_ns = monotonic_ns() - start_ns
    if len(result.output_beats) != asset.vertex_count:
        raise RuntimeError("蒙皮结果顶点数与资产不一致")
    # Opaque assets may record single-sided materials, so back-face culling
    # makes the projected set a non-empty subset of the source triangles.
    if not state.projected_triangles:
        raise RuntimeError("projected mesh must not be empty")
    if len(state.projected_triangles) > asset.triangle_count:
        raise RuntimeError("投影三角形数与资产不一致")
    return AvatarPacketAcceptance(
        asset_name=asset.name,
        vertex_count=asset.vertex_count,
        triangle_count=asset.triangle_count,
        packet_tx_bytes=plan.packet_tx_bytes,
        packet_rx_bytes=plan.packet_rx_bytes,
        repetitions=repetitions,
        elapsed_ns=elapsed_ns,
    )


__all__ = ["AvatarPacketAcceptance", "verify_avatar_packet_pipeline"]
