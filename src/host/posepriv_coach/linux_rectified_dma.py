"""Linux 字符设备到校正三角测量 AXI-DMA 批次的适配器。"""

from __future__ import annotations

from pathlib import Path

from .linux_dma_device import (
    DeviceOpener,
    LinuxBlockingDmaDevice,
    LinuxDmaDeviceError,
    open_unbuffered,
)
from .linux_dma_proxy import LinuxDmaProxyError, LinuxDmaProxyPair
from .rectified_stereo import AXIS_BEAT_BYTES


class LinuxRectifiedStereoDmaError(RuntimeError):
    """报告校正三角测量字符设备的传输错误。"""


class LinuxRectifiedStereoDmaTransport:
    """通过单个阻塞字符设备交换完整的校正关键点批次。"""

    def __init__(
        self,
        device: str | Path,
        *,
        opener: DeviceOpener = open_unbuffered,
    ) -> None:
        self.device = Path(device)
        self._device = LinuxBlockingDmaDevice(self.device, opener=opener)

    def transfer(self, payload: bytes) -> bytes:
        if not payload or len(payload) % AXIS_BEAT_BYTES != 0:
            raise ValueError("Linux rectified DMA payload must contain complete AXI beats")
        try:
            return self._device.exchange(payload, expected_bytes=len(payload))
        except LinuxDmaDeviceError as exc:
            raise LinuxRectifiedStereoDmaError(str(exc)) from exc

    def close(self) -> None:
        self._device.close()


class LinuxRectifiedStereoDmaProxyTransport:
    """通过独立 TX/RX dma-proxy 设备交换校正关键点批次。"""

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
        if not payload or len(payload) % AXIS_BEAT_BYTES != 0:
            raise ValueError("Linux rectified DMA payload must contain complete AXI beats")
        try:
            return self._proxy.exchange(payload, expected_bytes=len(payload))
        except LinuxDmaProxyError as exc:
            raise LinuxRectifiedStereoDmaError(str(exc)) from exc

    def close(self) -> None:
        self._proxy.close()


__all__ = [
    "LinuxRectifiedStereoDmaError",
    "LinuxRectifiedStereoDmaProxyTransport",
    "LinuxRectifiedStereoDmaTransport",
]
