"""把 Linux evdev 相对鼠标事件接到 framebuffer 数字人相机。"""

from __future__ import annotations

import os
from pathlib import Path
import select
import struct
from threading import Event, Thread
from typing import Protocol


EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
SYN_REPORT = 0
REL_X = 0
REL_Y = 1
REL_WHEEL = 8
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
_INPUT_EVENT = struct.Struct("llHHi")


class AvatarMouseTarget(Protocol):
    def rotate(self, delta_yaw_degrees: float, delta_pitch_degrees: float) -> None: ...
    def pan(self, delta_x: float, delta_y: float) -> None: ...
    def zoom_by(self, factor: float) -> None: ...


class LinuxAvatarMouseError(RuntimeError):
    pass


class RelativeMouseEventDecoder:
    """按 SYN_REPORT 聚合 evdev 相对位移，便于分片读取和确定性测试。"""

    def __init__(
        self,
        target: AvatarMouseTarget,
        *,
        rotate_degrees_per_count: float = 0.35,
        pan_pixels_per_count: float = 1.0,
        zoom_step: float = 1.1,
    ) -> None:
        if rotate_degrees_per_count <= 0 or pan_pixels_per_count <= 0:
            raise ValueError("鼠标旋转和平移灵敏度必须为正数")
        if zoom_step <= 1.0:
            raise ValueError("鼠标滚轮缩放倍率必须大于 1")
        self.target = target
        self.rotate_degrees_per_count = rotate_degrees_per_count
        self.pan_pixels_per_count = pan_pixels_per_count
        self.zoom_step = zoom_step
        self._buffer = bytearray()
        self._left = False
        self._right = False
        self._dx = 0
        self._dy = 0
        self._wheel = 0

    def feed(self, payload: bytes) -> None:
        self._buffer.extend(payload)
        while len(self._buffer) >= _INPUT_EVENT.size:
            raw = bytes(self._buffer[: _INPUT_EVENT.size])
            del self._buffer[: _INPUT_EVENT.size]
            _seconds, _microseconds, event_type, code, value = _INPUT_EVENT.unpack(raw)
            self._accept(event_type, code, value)

    def _accept(self, event_type: int, code: int, value: int) -> None:
        if event_type == EV_KEY:
            if code == BTN_LEFT:
                self._left = value != 0
            elif code == BTN_RIGHT:
                self._right = value != 0
        elif event_type == EV_REL:
            if code == REL_X:
                self._dx += value
            elif code == REL_Y:
                self._dy += value
            elif code == REL_WHEEL:
                self._wheel += value
        elif event_type == EV_SYN and code == SYN_REPORT:
            self._flush()

    def _flush(self) -> None:
        if self._left and (self._dx or self._dy):
            self.target.rotate(
                self._dx * self.rotate_degrees_per_count,
                self._dy * self.rotate_degrees_per_count,
            )
        elif self._right and (self._dx or self._dy):
            self.target.pan(
                self._dx * self.pan_pixels_per_count,
                self._dy * self.pan_pixels_per_count,
            )
        if self._wheel:
            factor = self.zoom_step ** abs(self._wheel)
            self.target.zoom_by(factor if self._wheel > 0 else 1.0 / factor)
        self._dx = self._dy = self._wheel = 0


class LinuxAvatarMouseController:
    """后台读取单个明确指定的 evdev 设备，不扫描或独占其他输入设备。"""

    def __init__(self, device: str | Path, target: AvatarMouseTarget) -> None:
        self.device = Path(device)
        self._decoder = RelativeMouseEventDecoder(target)
        self._stop = Event()
        self._thread: Thread | None = None
        self._fd: int | None = None
        self._failure: BaseException | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise LinuxAvatarMouseError("数字人鼠标控制器已经启动")
        try:
            self._fd = os.open(self.device, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            raise LinuxAvatarMouseError(f"无法打开数字人鼠标设备：{self.device}") from exc
        self._thread = Thread(target=self._run, name="posepriv-avatar-mouse", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        assert self._fd is not None
        try:
            while not self._stop.is_set():
                readable, _, _ = select.select((self._fd,), (), (), 0.1)
                if not readable:
                    continue
                try:
                    payload = os.read(self._fd, _INPUT_EVENT.size * 16)
                except BlockingIOError:
                    continue
                if not payload:
                    raise LinuxAvatarMouseError("数字人鼠标设备已断开")
                self._decoder.feed(payload)
        except BaseException as exc:
            self._failure = exc

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise LinuxAvatarMouseError("数字人鼠标输入线程失败") from self._failure

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._fd is not None:
            os.close(self._fd)
        self._thread = None
        self._fd = None


def pack_test_input_event(event_type: int, code: int, value: int) -> bytes:
    """仅供离线测试生成与本机 ABI 一致的 input_event。"""

    return _INPUT_EVENT.pack(0, 0, event_type, code, value)


__all__ = [
    "LinuxAvatarMouseController",
    "LinuxAvatarMouseError",
    "RelativeMouseEventDecoder",
]
