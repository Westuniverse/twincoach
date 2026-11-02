"""Linux DMA transports for the temporal-motion AXI stream."""

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


class LinuxMotionDmaError(RuntimeError):
    """A temporal-motion DMA transaction failed."""


class LinuxMotionDmaTransport:
    """Exchange one same-length batch with a blocking character device."""

    def __init__(
        self,
        device: str | Path,
        *,
        opener: DeviceOpener = open_unbuffered,
    ) -> None:
        self.device = Path(device)
        self._device = LinuxBlockingDmaDevice(self.device, opener=opener)

    def transfer(self, payload: bytes, *, expected_bytes: int | None = None) -> bytes:
        if not payload or len(payload) % AXIS_BEAT_BYTES != 0:
            raise ValueError("Linux motion DMA payload must contain complete AXI beats")
        expected = len(payload) if expected_bytes is None else expected_bytes
        if expected <= 0 or expected % AXIS_BEAT_BYTES != 0:
            raise ValueError("Linux motion DMA output must contain complete AXI beats")
        try:
            return self._device.exchange(payload, expected_bytes=expected)
        except LinuxDmaDeviceError as exc:
            raise LinuxMotionDmaError(str(exc)) from exc

    def close(self) -> None:
        self._device.close()


class LinuxMotionDmaProxyTransport:
    """Exchange one same-length batch through independent dma-proxy TX/RX nodes."""

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

    def transfer(self, payload: bytes, *, expected_bytes: int | None = None) -> bytes:
        if not payload or len(payload) % AXIS_BEAT_BYTES != 0:
            raise ValueError("Linux motion DMA payload must contain complete AXI beats")
        expected = len(payload) if expected_bytes is None else expected_bytes
        if expected <= 0 or expected % AXIS_BEAT_BYTES != 0:
            raise ValueError("Linux motion DMA output must contain complete AXI beats")
        try:
            return self._proxy.exchange(payload, expected_bytes=expected)
        except LinuxDmaProxyError as exc:
            raise LinuxMotionDmaError(str(exc)) from exc

    def close(self) -> None:
        self._proxy.close()


__all__ = [
    "LinuxMotionDmaError",
    "LinuxMotionDmaProxyTransport",
    "LinuxMotionDmaTransport",
]
