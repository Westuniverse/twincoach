"""Linux 字符设备到数字人 AXI-DMA 整包边界的最小适配器。"""

from __future__ import annotations

from pathlib import Path

from .avatar_skinning import AXIS_BEAT_BYTES, MATRIX_COUNT, MATRIX_PACKET_BYTES
from .linux_dma_device import (
    DeviceOpener,
    LinuxBlockingDmaDevice,
    LinuxDmaDeviceError,
    open_unbuffered,
)
from .linux_dma_proxy import LinuxDmaProxyError, LinuxDmaProxyPair


class LinuxAvatarDmaError(RuntimeError):
    """报告字符设备打开、短写、短读或关闭后的调用。"""


class LinuxAvatarDmaTransport:
    """通过一个阻塞字符设备完成一帧数字人 DMA 事务。

    一次 ``write`` 接收完整矩阵前导和顶点包；随后 ``read`` 返回每顶点 16 字节结果。
    设备句柄跨帧复用，锁保证帧不会交叉。缓存维护和中断等待属于驱动。
    """

    def __init__(
        self,
        device: str | Path,
        *,
        opener: DeviceOpener = open_unbuffered,
    ) -> None:
        self.device = Path(device)
        self._device = LinuxBlockingDmaDevice(self.device, opener=opener)

    def transfer(self, payload: bytes) -> bytes:
        matrix_bytes = MATRIX_COUNT * MATRIX_PACKET_BYTES
        if len(payload) <= matrix_bytes:
            raise ValueError("Linux avatar DMA packet must contain at least one vertex")
        vertex_bytes = len(payload) - matrix_bytes
        if vertex_bytes % AXIS_BEAT_BYTES != 0:
            raise ValueError("Linux avatar DMA packet must contain complete AXI beats")
        try:
            return self._device.exchange(payload, expected_bytes=vertex_bytes)
        except LinuxDmaDeviceError as exc:
            raise LinuxAvatarDmaError(str(exc)) from exc

    def close(self) -> None:
        self._device.close()


class LinuxAvatarDmaProxyTransport:
    """通过官方 dma-proxy 风格的独立 TX/RX 设备完成数字人整帧事务。"""

    def __init__(
        self,
        tx_device: str | Path,
        rx_device: str | Path,
        *,
        proxy: LinuxDmaProxyPair | None = None,
    ) -> None:
        self.tx_device = Path(tx_device)
        self.rx_device = Path(rx_device)
        self._proxy = proxy or LinuxDmaProxyPair(self.tx_device, self.rx_device)

    def transfer(self, payload: bytes) -> bytes:
        matrix_bytes = MATRIX_COUNT * MATRIX_PACKET_BYTES
        if len(payload) <= matrix_bytes:
            raise ValueError("Linux avatar DMA packet must contain at least one vertex")
        vertex_bytes = len(payload) - matrix_bytes
        if vertex_bytes % AXIS_BEAT_BYTES != 0:
            raise ValueError("Linux avatar DMA packet must contain complete AXI beats")
        try:
            return self._proxy.exchange(payload, expected_bytes=vertex_bytes)
        except LinuxDmaProxyError as exc:
            raise LinuxAvatarDmaError(str(exc)) from exc

    def close(self) -> None:
        self._proxy.close()


__all__ = [
    "LinuxAvatarDmaError",
    "LinuxAvatarDmaProxyTransport",
    "LinuxAvatarDmaTransport",
]
