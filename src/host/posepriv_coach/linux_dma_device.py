"""阻塞 Linux 字符设备的一次完整写入、定长读回事务。"""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import BinaryIO, Callable


class LinuxDmaDeviceError(RuntimeError):
    pass


DeviceOpener = Callable[[Path], BinaryIO]


def open_unbuffered(path: Path) -> BinaryIO:
    return open(path, "r+b", buffering=0)


class LinuxBlockingDmaDevice:
    """复用一个设备句柄，并保证不同帧的写读事务不会交叉。"""

    def __init__(
        self,
        device: str | Path,
        *,
        opener: DeviceOpener = open_unbuffered,
    ) -> None:
        self.device = Path(device)
        self._opener = opener
        self._stream: BinaryIO | None = None
        self._closed = False
        self._faulted = False
        self._lock = Lock()

    def exchange(self, payload: bytes, *, expected_bytes: int) -> bytes:
        if not payload or expected_bytes <= 0:
            raise ValueError("Linux DMA transaction sizes must be positive")
        with self._lock:
            if self._closed:
                raise LinuxDmaDeviceError("Linux DMA transport is closed")
            if self._faulted:
                raise LinuxDmaDeviceError(
                    "Linux DMA transport is faulted and must be reconstructed"
                )
            stream = self._ensure_open()
            try:
                self._write_all(stream, payload)
                return self._read_exact(stream, expected_bytes)
            except LinuxDmaDeviceError as exc:
                self._invalidate_after_transaction_failure()
                raise LinuxDmaDeviceError(
                    f"{exc}; DMA transport invalidated after partial transaction"
                ) from exc

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._stream is not None:
                self._stream.close()
                self._stream = None

    def _invalidate_after_transaction_failure(self) -> None:
        self._faulted = True
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _ensure_open(self) -> BinaryIO:
        if self._stream is None:
            try:
                self._stream = self._opener(self.device)
            except OSError as exc:
                raise LinuxDmaDeviceError(f"无法打开 DMA 设备：{self.device}") from exc
        return self._stream

    @staticmethod
    def _write_all(stream: BinaryIO, payload: bytes) -> None:
        view = memoryview(payload)
        written = 0
        while written < len(payload):
            try:
                count = stream.write(view[written:])
            except OSError as exc:
                raise LinuxDmaDeviceError("DMA 写入失败") from exc
            if count is None or count <= 0:
                raise LinuxDmaDeviceError("DMA 设备发生短写")
            written += count

    @staticmethod
    def _read_exact(stream: BinaryIO, expected: int) -> bytes:
        chunks: list[bytes] = []
        received = 0
        while received < expected:
            try:
                chunk = stream.read(expected - received)
            except OSError as exc:
                raise LinuxDmaDeviceError("DMA 读取失败") from exc
            if not chunk:
                raise LinuxDmaDeviceError(
                    f"DMA 输出提前结束：期望 {expected} B，实际 {received} B"
                )
            chunks.append(bytes(chunk))
            received += len(chunk)
        return b"".join(chunks)


__all__ = ["DeviceOpener", "LinuxBlockingDmaDevice", "LinuxDmaDeviceError"]
