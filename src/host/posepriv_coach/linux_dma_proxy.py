"""AMD/Xilinx dma-proxy 双通道的零拷贝用户态事务封装。"""

from __future__ import annotations

import mmap
import os
from pathlib import Path
import struct
from threading import Lock
from typing import Callable, Protocol


DMA_PROXY_BUFFER_BYTES = 128 * 1024
DMA_PROXY_BUFFER_COUNT = 32
DMA_PROXY_STATUS_OFFSET = DMA_PROXY_BUFFER_BYTES
DMA_PROXY_LENGTH_OFFSET = DMA_PROXY_STATUS_OFFSET + 4
DMA_PROXY_BUFFER_STRIDE = DMA_PROXY_BUFFER_BYTES + 16
DMA_PROXY_MAP_BYTES = DMA_PROXY_BUFFER_STRIDE * DMA_PROXY_BUFFER_COUNT

PROXY_NO_ERROR = 0
PROXY_BUSY = 1
PROXY_TIMEOUT = 2
PROXY_ERROR = 3


class LinuxDmaProxyError(RuntimeError):
    pass


class ProxyMapping(Protocol):
    def __getitem__(self, key: slice) -> bytes: ...
    def __setitem__(self, key: slice, value: bytes) -> None: ...
    def close(self) -> None: ...


ProxyOpener = Callable[[Path], int]
ProxyMapper = Callable[[int, int], ProxyMapping]
ProxyIoctl = Callable[[int, int, bytes], object]
ProxyCloser = Callable[[int], None]


def _ioc(direction: int, type_value: int, number: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (type_value << 8) | number


_POINTER_BYTES = struct.calcsize("P")
DMA_PROXY_FINISH_XFER = _ioc(1, ord("a"), ord("a"), _POINTER_BYTES)
DMA_PROXY_START_XFER = _ioc(1, ord("a"), ord("b"), _POINTER_BYTES)


def open_proxy_device(path: Path) -> int:
    return os.open(path, os.O_RDWR | getattr(os, "O_SYNC", 0))


def map_proxy_device(fd: int, size: int) -> ProxyMapping:
    return mmap.mmap(
        fd,
        size,
        flags=mmap.MAP_SHARED,
        prot=mmap.PROT_READ | mmap.PROT_WRITE,
    )


def proxy_ioctl(fd: int, request: int, argument: bytes) -> object:
    import fcntl

    return fcntl.ioctl(fd, request, argument)


class LinuxDmaProxyPair:
    """把 dma-proxy 的 TX/RX 字符设备组合成一次阻塞交换。"""

    def __init__(
        self,
        tx_device: str | Path,
        rx_device: str | Path,
        *,
        opener: ProxyOpener = open_proxy_device,
        mapper: ProxyMapper = map_proxy_device,
        ioctl: ProxyIoctl = proxy_ioctl,
        closer: ProxyCloser = os.close,
        buffer_index: int = 0,
    ) -> None:
        if not 0 <= buffer_index < DMA_PROXY_BUFFER_COUNT:
            raise ValueError("dma-proxy buffer_index 超出范围")
        self.tx_device = Path(tx_device)
        self.rx_device = Path(rx_device)
        self.buffer_index = buffer_index
        self._opener = opener
        self._mapper = mapper
        self._ioctl = ioctl
        self._closer = closer
        self._tx_fd: int | None = None
        self._rx_fd: int | None = None
        self._tx_map: ProxyMapping | None = None
        self._rx_map: ProxyMapping | None = None
        self._closed = False
        self._faulted = False
        self._lock = Lock()

    def exchange(self, payload: bytes, *, expected_bytes: int) -> bytes:
        if not payload or expected_bytes <= 0:
            raise ValueError("dma-proxy 事务长度必须为正数")
        if max(len(payload), expected_bytes) > DMA_PROXY_BUFFER_BYTES:
            raise ValueError(
                f"dma-proxy 单次事务不能超过 {DMA_PROXY_BUFFER_BYTES} B"
            )
        with self._lock:
            if self._closed:
                raise LinuxDmaProxyError("dma-proxy 传输层已关闭")
            if self._faulted:
                raise LinuxDmaProxyError("dma-proxy 传输层已失效，必须重新创建")
            self._ensure_open()
            assert self._tx_fd is not None and self._rx_fd is not None
            assert self._tx_map is not None and self._rx_map is not None
            offset = self.buffer_index * DMA_PROXY_BUFFER_STRIDE
            self._tx_map[offset : offset + len(payload)] = payload
            self._set_u32(self._tx_map, offset + DMA_PROXY_LENGTH_OFFSET, len(payload))
            self._set_u32(
                self._rx_map, offset + DMA_PROXY_LENGTH_OFFSET, expected_bytes
            )
            index = struct.pack("@i", self.buffer_index)
            try:
                # 接收必须先排队，避免短流水核在 S2MM 尚未就绪时产生背压或丢失首拍。
                self._ioctl(self._rx_fd, DMA_PROXY_START_XFER, index)
                self._ioctl(self._tx_fd, DMA_PROXY_START_XFER, index)
                self._ioctl(self._tx_fd, DMA_PROXY_FINISH_XFER, index)
                self._ioctl(self._rx_fd, DMA_PROXY_FINISH_XFER, index)
            except OSError as exc:
                self._invalidate_after_transaction_failure()
                raise LinuxDmaProxyError(
                    "dma-proxy ioctl 事务失败；传输层已失效，必须重新创建"
                ) from exc
            try:
                self._check_status(self._tx_map, offset, "TX")
                self._check_status(self._rx_map, offset, "RX")
            except LinuxDmaProxyError as exc:
                self._invalidate_after_transaction_failure()
                raise LinuxDmaProxyError(
                    f"{exc}；传输层已失效，必须重新创建"
                ) from exc
            return bytes(self._rx_map[offset : offset + expected_bytes])

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for mapping in (self._tx_map, self._rx_map):
                if mapping is not None:
                    mapping.close()
            for fd in (self._tx_fd, self._rx_fd):
                if fd is not None:
                    self._closer(fd)
            self._tx_map = self._rx_map = None
            self._tx_fd = self._rx_fd = None

    def _invalidate_after_transaction_failure(self) -> None:
        self._faulted = True
        for mapping in (self._tx_map, self._rx_map):
            if mapping is not None:
                try:
                    mapping.close()
                except Exception:
                    pass
        for fd in (self._tx_fd, self._rx_fd):
            if fd is not None:
                try:
                    self._closer(fd)
                except Exception:
                    pass
        self._tx_map = self._rx_map = None
        self._tx_fd = self._rx_fd = None

    def _ensure_open(self) -> None:
        if self._tx_fd is not None:
            return
        try:
            tx_fd = self._opener(self.tx_device)
            try:
                rx_fd = self._opener(self.rx_device)
            except BaseException:
                self._closer(tx_fd)
                raise
            try:
                tx_map = self._mapper(tx_fd, DMA_PROXY_MAP_BYTES)
                try:
                    rx_map = self._mapper(rx_fd, DMA_PROXY_MAP_BYTES)
                except BaseException:
                    tx_map.close()
                    raise
            except BaseException:
                self._closer(rx_fd)
                self._closer(tx_fd)
                raise
        except OSError as exc:
            raise LinuxDmaProxyError(
                f"无法打开 dma-proxy 设备：{self.tx_device} / {self.rx_device}"
            ) from exc
        self._tx_fd, self._rx_fd = tx_fd, rx_fd
        self._tx_map, self._rx_map = tx_map, rx_map

    @staticmethod
    def _set_u32(mapping: ProxyMapping, offset: int, value: int) -> None:
        mapping[offset : offset + 4] = struct.pack("@I", value)

    @staticmethod
    def _get_u32(mapping: ProxyMapping, offset: int) -> int:
        return struct.unpack("@I", mapping[offset : offset + 4])[0]

    @classmethod
    def _check_status(cls, mapping: ProxyMapping, offset: int, channel: str) -> None:
        status = cls._get_u32(mapping, offset + DMA_PROXY_STATUS_OFFSET)
        if status == PROXY_NO_ERROR:
            return
        names = {
            PROXY_BUSY: "BUSY",
            PROXY_TIMEOUT: "TIMEOUT",
            PROXY_ERROR: "ERROR",
        }
        raise LinuxDmaProxyError(
            f"dma-proxy {channel} 状态异常：{names.get(status, status)}"
        )

    def __enter__(self) -> "LinuxDmaProxyPair":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = [
    "DMA_PROXY_BUFFER_BYTES",
    "DMA_PROXY_BUFFER_COUNT",
    "DMA_PROXY_BUFFER_STRIDE",
    "DMA_PROXY_FINISH_XFER",
    "DMA_PROXY_MAP_BYTES",
    "DMA_PROXY_START_XFER",
    "LinuxDmaProxyError",
    "LinuxDmaProxyPair",
]
