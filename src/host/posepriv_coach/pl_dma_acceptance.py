"""真实 Linux PL 传输与位精确软件黄金结果的分层验收。"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from time import monotonic_ns
from typing import Callable

from .avatar_skinning import (
    AffineMatrixQ1_14,
    MATRIX_COUNT,
    MatrixWrite,
    ReferencePacketAvatarTransport,
    pack_skinning_packet,
)
from .mesh_asset import AvatarMeshAsset
from .pl_metrics import (
    AxisBeat,
    JointMetricRequest,
    ReferenceJointMetricBackend,
    pack_joint_request,
)
from .pl_motion import (
    MotionMetricRequest,
    pack_motion_request,
    reference_motion_output,
    reference_motion_stats_output,
)
from .rectified_stereo import (
    RectifiedStereoRequest,
    pack_rectified_stereo_request,
    pack_rectified_stereo_result,
    reference_rectified_stereo_output,
)


ByteTransfer = Callable[[bytes], bytes]


class PlDmaAcceptanceError(RuntimeError):
    """报告真实 DMA 输出与软件黄金结果的首处不一致。"""


@dataclass(frozen=True, slots=True)
class PlDmaStageEvidence:
    transactions: int
    tx_bytes: int
    rx_bytes: int
    mean_ms: float
    p95_ms: float
    max_ms: float


@dataclass(frozen=True, slots=True)
class JointPlDmaAcceptance:
    """单独验证关节指标 PL 通道所得的位精确证据。"""

    joint_metrics: PlDmaStageEvidence
    joint_beats_per_transaction: int
    bit_exact: bool = True


@dataclass(frozen=True, slots=True)
class RectifiedPlDmaAcceptance:
    """独立校正三角测量通道，交替批次检查陈旧数据和长度。"""

    rectified_triangulation: PlDmaStageEvidence
    rectified_beats_per_fixture: tuple[int, ...]
    bit_exact: bool = True


@dataclass(frozen=True, slots=True)
class DualPlDmaAcceptance:
    joint_metrics: PlDmaStageEvidence
    avatar_skinning: PlDmaStageEvidence
    joint_beats_per_transaction: int
    avatar_vertices_per_frame: int
    bit_exact: bool = True


@dataclass(frozen=True, slots=True)
class TriplePlDmaAcceptance:
    """连续验证 Joint、Avatar 和校正三角测量三条 DMA 通路的证据。"""

    dual: DualPlDmaAcceptance
    rectified_triangulation: PlDmaStageEvidence
    rectified_beats_per_transaction: int
    bit_exact: bool = True

    @property
    def joint_metrics(self) -> PlDmaStageEvidence:
        return self.dual.joint_metrics

    @property
    def avatar_skinning(self) -> PlDmaStageEvidence:
        return self.dual.avatar_skinning

    @property
    def avatar_vertices_per_frame(self) -> int:
        return self.dual.avatar_vertices_per_frame


@dataclass(frozen=True, slots=True)
class MotionStatsPlDmaAcceptance:
    """Evidence for the fourth, batch-aggregation PL endpoint."""

    motion_stats: PlDmaStageEvidence
    motion_stats_input_beats: int
    bit_exact: bool = True


def verify_joint_pl_dma(
    joint_transfer: ByteTransfer,
    *,
    repetitions: int = 8,
    clock_ns: Callable[[], int] = monotonic_ns,
) -> JointPlDmaAcceptance:
    """先隔离关节指标通道，连续核对多个事务并检查跨事务陈旧状态。"""

    if repetitions < 2:
        raise ValueError("PL DMA 验收至少需要 2 次事务以检查跨帧状态")

    fixtures = (_joint_fixture(0), _joint_fixture(1))
    timings: list[int] = []
    for index in range(repetitions):
        payload, expected = fixtures[index & 1]
        _verify_transfer(
            "joint_metrics",
            index,
            joint_transfer,
            payload,
            expected,
            timings,
            clock_ns,
        )
    return JointPlDmaAcceptance(
        joint_metrics=_stage_evidence(
            timings,
            tx_bytes=sum(len(fixtures[index & 1][0]) for index in range(repetitions)),
            rx_bytes=sum(len(fixtures[index & 1][1]) for index in range(repetitions)),
        ),
        joint_beats_per_transaction=len(fixtures[0][0]) // 16,
    )


def verify_rectified_pl_dma(
    rectified_transfer: ByteTransfer,
    *,
    repetitions: int = 8,
    clock_ns: Callable[[], int] = monotonic_ns,
) -> RectifiedPlDmaAcceptance:
    """不依赖关节/数字人通道，核对两组不同长度的全部输出字节。"""

    if repetitions < 2:
        raise ValueError("PL DMA 验收至少需要 2 次事务以检查跨帧状态")
    fixtures = (_rectified_fixture(0), _rectified_fixture(1))
    timings: list[int] = []
    for index in range(repetitions):
        payload, expected = fixtures[index & 1]
        _verify_transfer("rectified_triangulation", index, rectified_transfer,
                         payload, expected, timings, clock_ns)
    return RectifiedPlDmaAcceptance(
        rectified_triangulation=_stage_evidence(
            timings,
            tx_bytes=sum(len(fixtures[index & 1][0]) for index in range(repetitions)),
            rx_bytes=sum(len(fixtures[index & 1][1]) for index in range(repetitions)),
        ),
        rectified_beats_per_fixture=tuple(len(payload) // 16 for payload, _ in fixtures),
    )


def verify_dual_pl_dma(
    joint_transfer: ByteTransfer,
    avatar_transfer: ByteTransfer,
    mesh_asset: AvatarMeshAsset,
    *,
    repetitions: int = 8,
    clock_ns: Callable[[], int] = monotonic_ns,
) -> DualPlDmaAcceptance:
    """连续交替输入两组数据，分别核对关节核和数字人核的全部输出字节。"""

    if repetitions < 2:
        raise ValueError("PL DMA 验收至少需要 2 次事务以检查跨帧状态")

    joint_fixtures = (_joint_fixture(0), _joint_fixture(1))
    avatar_requests = mesh_asset.skinning_requests()
    if not avatar_requests:
        raise ValueError("数字人网格必须至少包含一个顶点")
    avatar_payloads = tuple(
        pack_skinning_packet(_matrix_fixture(phase), avatar_requests)
        for phase in (0, 1)
    )
    reference_avatar = ReferencePacketAvatarTransport()
    avatar_expected = tuple(
        reference_avatar.transfer(payload) for payload in avatar_payloads
    )

    joint_timings: list[int] = []
    avatar_timings: list[int] = []
    for index in range(repetitions):
        joint_payload, joint_expected = joint_fixtures[index & 1]
        _verify_transfer(
            "joint_metrics",
            index,
            joint_transfer,
            joint_payload,
            joint_expected,
            joint_timings,
            clock_ns,
        )
        phase = index & 1
        _verify_transfer(
            "avatar_skinning",
            index,
            avatar_transfer,
            avatar_payloads[phase],
            avatar_expected[phase],
            avatar_timings,
            clock_ns,
        )

    return DualPlDmaAcceptance(
        joint_metrics=_stage_evidence(
            joint_timings,
            tx_bytes=sum(
                len(joint_fixtures[index & 1][0]) for index in range(repetitions)
            ),
            rx_bytes=sum(
                len(joint_fixtures[index & 1][1]) for index in range(repetitions)
            ),
        ),
        avatar_skinning=_stage_evidence(
            avatar_timings,
            tx_bytes=sum(len(avatar_payloads[index & 1]) for index in range(repetitions)),
            rx_bytes=sum(len(avatar_expected[index & 1]) for index in range(repetitions)),
        ),
        joint_beats_per_transaction=len(joint_fixtures[0][0]) // 16,
        avatar_vertices_per_frame=len(avatar_requests),
    )


def verify_triple_pl_dma(
    joint_transfer: ByteTransfer,
    avatar_transfer: ByteTransfer,
    rectified_transfer: ByteTransfer,
    mesh_asset: AvatarMeshAsset,
    *,
    repetitions: int = 8,
    clock_ns: Callable[[], int] = monotonic_ns,
) -> TriplePlDmaAcceptance:
    """在双路黄金对拍基础上连续核对校正三角测量 DMA。"""

    dual = verify_dual_pl_dma(
        joint_transfer,
        avatar_transfer,
        mesh_asset,
        repetitions=repetitions,
        clock_ns=clock_ns,
    )
    rectified_payload, rectified_expected = _rectified_fixture()
    rectified_timings: list[int] = []
    for index in range(repetitions):
        _verify_transfer(
            "rectified_triangulation",
            index,
            rectified_transfer,
            rectified_payload,
            rectified_expected,
            rectified_timings,
            clock_ns,
        )
    return TriplePlDmaAcceptance(
        dual=dual,
        rectified_triangulation=_stage_evidence(
            rectified_timings,
            tx_bytes=len(rectified_payload) * repetitions,
            rx_bytes=len(rectified_expected) * repetitions,
        ),
        rectified_beats_per_transaction=len(rectified_payload) // 16,
    )


def verify_motion_stats_dma(
    motion_stats_transfer: ByteTransfer,
    *,
    repetitions: int = 8,
    clock_ns: Callable[[], int] = monotonic_ns,
) -> MotionStatsPlDmaAcceptance:
    """Check the temporal-motion batch aggregator against its bit-accurate model."""

    if repetitions < 2:
        raise ValueError("motion stats PL DMA acceptance requires at least 2 transactions")

    payload, expected = _motion_stats_fixture()
    timings: list[int] = []
    for index in range(repetitions):
        _verify_transfer(
            "motion_stats",
            index,
            motion_stats_transfer,
            payload,
            expected,
            timings,
            clock_ns,
        )
    return MotionStatsPlDmaAcceptance(
        motion_stats=_stage_evidence(
            timings,
            tx_bytes=len(payload) * repetitions,
            rx_bytes=len(expected) * repetitions,
        ),
        motion_stats_input_beats=len(payload) // 16,
    )


def _joint_fixture(phase: int = 0) -> tuple[bytes, bytes]:
    if phase == 0:
        requests = (
            JointMetricRequest(-8192, 4096, 0, 0, 8192, 4096, 0, 0),
            JointMetricRequest(16384, 0, 8192, -4096, 0, 8192, 3, 1),
            JointMetricRequest(-12000, -6000, -1000, 2000, 14000, -3000, 7, 0),
            JointMetricRequest(32767, -32768, 0, 0, -32768, 32767, 15, 1),
        )
    elif phase == 1:
        requests = (
            JointMetricRequest(8192, -4096, 0, 0, -4096, 12288, 0, 0),
            JointMetricRequest(-16384, 4096, -8192, 0, 4096, -8192, 3, 1),
            JointMetricRequest(1000, 12000, -2000, -1000, -14000, 3000, 7, 0),
            JointMetricRequest(-32768, 32767, 0, 0, 32767, -32768, 15, 1),
        )
    else:
        raise ValueError(f"unsupported joint fixture phase: {phase}")
    beats = tuple(
        pack_joint_request(request, last=index == len(requests) - 1)
        for index, request in enumerate(requests)
    )
    expected = ReferenceJointMetricBackend().exchange(beats)
    return (
        b"".join(beat.data for beat in beats),
        b"".join(beat.data for beat in expected),
    )


def _rectified_fixture(phase: int = 0) -> tuple[bytes, bytes]:
    if phase == 0:
        base_requests = (
            RectifiedStereoRequest(819, 410, -819, 410, 3277, 16384, 7, 240, 230),
            RectifiedStereoRequest(100, 200, 100, 200, 3277, 16384, 8, 255, 255),
            RectifiedStereoRequest(-1000, 500, 1000, -500, -3277, 16384, 9, 64, 200),
            RectifiedStereoRequest(32767, -32768, -32768, 32767, 32767, -32768, 10, 255, 64),
        )
    elif phase == 1:
        base_requests = (
            RectifiedStereoRequest(-4096, -2048, 4096, -2048, 4096, 16384, 21, 200, 180),
            RectifiedStereoRequest(128, -100, 64, -100, 1024, 8192, 22, 64, 64),
            RectifiedStereoRequest(127, 200, 64, 200, 3277, 16384, 23, 255, 255),
            RectifiedStereoRequest(819, 410, -819, 410, 3277, 16384, 24, 63, 255),
            RectifiedStereoRequest(-32768, 32767, 32767, -32768, -32768, 32767, 25, 255, 255),
        )
    else:
        raise ValueError(f"unsupported rectified fixture phase: {phase}")
    requests = tuple(
        replace(request, last=index == len(base_requests) - 1)
        for index, request in enumerate(base_requests)
    )
    payload = b"".join(
        pack_rectified_stereo_request(request) for request in requests
    )
    expected = b"".join(
        pack_rectified_stereo_result(
            reference_rectified_stereo_output(request)
        )
        for request in requests
    )
    return payload, expected


def _motion_stats_fixture() -> tuple[bytes, bytes]:
    requests = (
        MotionMetricRequest(-1200, 800, -900, 760, -500, 700, 4, 0),
        MotionMetricRequest(200, -400, 320, -360, 580, -300, 4, 0),
        MotionMetricRequest(1800, 1200, 1700, 1160, 1500, 1080, 4, 0),
        MotionMetricRequest(-2400, -800, -2100, -760, -1700, -680, 4, 0),
    )
    input_beats = tuple(
        pack_motion_request(request, last=index == len(requests) - 1)
        for index, request in enumerate(requests)
    )
    output_beats = tuple(reference_motion_output(beat) for beat in input_beats)
    return (
        b"".join(beat.data for beat in output_beats),
        reference_motion_stats_output(output_beats).data,
    )


def _matrix_fixture(phase: int) -> tuple[MatrixWrite, ...]:
    identity = AffineMatrixQ1_14.identity()
    translation = 2048 if phase == 0 else -3072
    translated = AffineMatrixQ1_14(
        (16384, 0, 0, translation, 0, 16384, 0, 0, 0, 0, 16384, 1024)
    )
    return tuple(
        MatrixWrite(index, translated if index == 0 else identity)
        for index in range(MATRIX_COUNT)
    )


def _verify_transfer(
    stage: str,
    transaction: int,
    transfer: ByteTransfer,
    payload: bytes,
    expected: bytes,
    timings: list[int],
    clock_ns: Callable[[], int],
) -> None:
    start = clock_ns()
    actual = transfer(payload)
    elapsed = clock_ns() - start
    if elapsed < 0:
        raise PlDmaAcceptanceError(f"{stage} 计时器发生倒退")
    timings.append(elapsed)
    if len(actual) != len(expected):
        raise PlDmaAcceptanceError(
            f"{stage} 第 {transaction + 1} 次返回长度错误："
            f"期望 {len(expected)} B，实际 {len(actual)} B"
        )
    mismatch = next(
        (index for index, pair in enumerate(zip(actual, expected, strict=True)) if pair[0] != pair[1]),
        None,
    )
    if mismatch is not None:
        raise PlDmaAcceptanceError(
            f"{stage} 第 {transaction + 1} 次在字节 {mismatch} 不一致："
            f"期望 0x{expected[mismatch]:02x}，实际 0x{actual[mismatch]:02x}"
        )


def _stage_evidence(
    timings: list[int],
    *,
    tx_bytes: int,
    rx_bytes: int,
) -> PlDmaStageEvidence:
    ordered = sorted(timings)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return PlDmaStageEvidence(
        transactions=len(timings),
        tx_bytes=tx_bytes,
        rx_bytes=rx_bytes,
        mean_ms=sum(timings) / len(timings) / 1_000_000,
        p95_ms=ordered[p95_index] / 1_000_000,
        max_ms=ordered[-1] / 1_000_000,
    )


__all__ = [
    "DualPlDmaAcceptance",
    "JointPlDmaAcceptance",
    "RectifiedPlDmaAcceptance",
    "PlDmaAcceptanceError",
    "PlDmaStageEvidence",
    "TriplePlDmaAcceptance",
    "MotionStatsPlDmaAcceptance",
    "verify_joint_pl_dma",
    "verify_rectified_pl_dma",
    "verify_dual_pl_dma",
    "verify_triple_pl_dma",
    "verify_motion_stats_dma",
]
