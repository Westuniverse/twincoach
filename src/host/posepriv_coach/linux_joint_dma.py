"""Linux 字符设备到关节指标 AXI-DMA 批次的适配器。"""

from __future__ import annotations

from pathlib import Path

from .linux_dma_device import (
    DeviceOpener,
    LinuxBlockingDmaDevice,
    LinuxDmaDeviceError,
    open_unbuffered,
)
from .linux_dma_proxy import LinuxDmaProxyError, LinuxDmaProxyPair
from .pl_metrics import AXIS_BEAT_BYTES


class LinuxJointMetricDmaError(RuntimeError):
    pass


class LinuxJointMetricDmaTransport:
    """一次写入完整关节批次，并读回相同拍数的结果。"""

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
            raise ValueError("Linux joint DMA payload must contain complete AXI beats")
        try:
            return self._device.exchange(payload, expected_bytes=len(payload))
        except LinuxDmaDeviceError as exc:
            raise LinuxJointMetricDmaError(str(exc)) from exc

    def close(self) -> None:
        self._device.close()


class LinuxJointMetricDmaProxyTransport:
    """通过独立 dma-proxy TX/RX 设备执行关节指标批次。"""

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
            raise ValueError("Linux joint DMA payload must contain complete AXI beats")
        try:
            return self._proxy.exchange(payload, expected_bytes=len(payload))
        except LinuxDmaProxyError as exc:
            raise LinuxJointMetricDmaError(str(exc)) from exc

    def close(self) -> None:
        self._proxy.close()


__all__ = [
    "LinuxJointMetricDmaError",
    "LinuxJointMetricDmaProxyTransport",
    "LinuxJointMetricDmaTransport",
]
