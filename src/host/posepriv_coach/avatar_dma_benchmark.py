"""解析头像蒙皮包 DMA 裸机自检的最小串口摘要。"""

from __future__ import annotations

from dataclasses import dataclass
import re


_HEADER = "PosePriv avatar-skinning packet DMA self-test"
_FREQUENCY = re.compile(r"Timer frequency:\s*(\d+)\s*Hz")
_RESULT = re.compile(
    r"PASS:\s*(\d+)\s*TX bytes,\s*(\d+)\s*RX bytes,\s*DMA ticks=(\d+)"
)
_PASS_MARKER = "POSEPRIV_AVATAR_DMA_TEST: PASS"
_FAIL_MARKER = "POSEPRIV_AVATAR_DMA_TEST: FAIL"
_CONTINUOUS_FRAMES = re.compile(r"Continuous frames:\s*(\d+)\s*PASS")


@dataclass(frozen=True, slots=True)
class AvatarDmaBenchmark:
    """一次头像数据包的 DMA 延迟和有效负载摘要。"""

    timer_frequency_hz: int
    tx_bytes: int
    rx_bytes: int
    ticks: int
    latency_us: float
    effective_tx_mbps: float
    continuous_frames: int = 1
    mean_frame_latency_us: float = 0.0


def parse_avatar_dma_selftest_log(text: str) -> AvatarDmaBenchmark:
    """解析最后一次启动的头像 DMA 自检块。

    解析边界对应裸机程序的最终 PASS 标记，不把日志中的启动噪声或更早
    的失败尝试混入测量结果。
    """

    if _HEADER not in text:
        raise ValueError("avatar DMA self-test header not found")
    block = text.rsplit(_HEADER, 1)[1]
    pass_index = block.rfind(_PASS_MARKER)
    fail_index = block.rfind(_FAIL_MARKER)
    if pass_index < 0 or fail_index > pass_index:
        raise ValueError("final avatar DMA self-test did not pass")

    frequency_match = _FREQUENCY.search(block[:pass_index])
    if frequency_match is None:
        raise ValueError("avatar DMA timer frequency not found")
    frequency_hz = int(frequency_match.group(1))
    if frequency_hz <= 0:
        raise ValueError("avatar DMA timer frequency must be positive")

    results = _RESULT.findall(block[:pass_index])
    if not results:
        raise ValueError("avatar DMA result line not found")
    tx_bytes, rx_bytes, ticks = (int(value) for value in results[-1])
    if tx_bytes <= 0 or rx_bytes <= 0 or ticks <= 0:
        raise ValueError("avatar DMA result values must be positive")
    frame_matches = _CONTINUOUS_FRAMES.findall(block[:pass_index])
    continuous_frames = int(frame_matches[-1]) if frame_matches else 1
    if continuous_frames <= 0:
        raise ValueError("avatar DMA continuous frame count must be positive")
    return AvatarDmaBenchmark(
        timer_frequency_hz=frequency_hz,
        tx_bytes=tx_bytes,
        rx_bytes=rx_bytes,
        ticks=ticks,
        latency_us=ticks * 1_000_000 / frequency_hz,
        effective_tx_mbps=(
            tx_bytes * continuous_frames * 8 * frequency_hz / ticks / 1_000_000
        ),
        continuous_frames=continuous_frames,
        mean_frame_latency_us=(
            ticks * 1_000_000 / frequency_hz / continuous_frames
        ),
    )


__all__ = ["AvatarDmaBenchmark", "parse_avatar_dma_selftest_log"]
