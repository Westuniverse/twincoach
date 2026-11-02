#!/usr/bin/env python3
"""显式临时设置双摄曝光动态帧率，运行命令，退出恢复；不保证30FPS。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import signal
import subprocess
import sys


CONTROL = "exposure_dynamic_framerate"


class CameraControlError(RuntimeError):
    pass


class CameraSessionInterrupted(OSError):
    def __init__(self, signum):
        super().__init__(f"相机会话收到信号{signum}")
        self.signum = signum


class DynamicFrameRateSession:
    """先读全体原值，再设置；部分写入失败也恢复，不重试或提权。"""

    def __init__(self, devices, value, *, tool="/usr/bin/v4l2-ctl", runner=subprocess.run):
        self.devices = tuple(devices)
        if len(self.devices) != 2 or any(not isinstance(p, str) or not p for p in self.devices):
            raise ValueError("需要两个非空摄像头路径")
        if self.devices[0] == self.devices[1]:
            raise ValueError("两台摄像头路径不能相同")
        if type(value) is not int or value not in (0, 1):
            raise ValueError("动态帧率必须显式为0或1")
        self.value, self.tool, self.runner = value, tool, runner
        self.original = {}
        self.touched = []
        self.entered = False
        self.session_active = False
        self.restore_attempted = False

    def _call(self, device, operation):
        result = self.runner([self.tool, "--device", device, operation],
                             capture_output=True, text=True, encoding="utf-8", timeout=5, check=False)
        if result.returncode:
            raise CameraControlError(f"摄像头控制失败({result.returncode}): {device}: {result.stderr.strip()}")
        return result.stdout

    def _read(self, device):
        output = self._call(device, f"--get-ctrl={CONTROL}")
        matched = re.fullmatch(rf"\s*{CONTROL}\s*:\s*([01])\s*", output)
        if matched is None:
            raise CameraControlError(f"无法严格读取动态帧率0/1: {device}")
        return int(matched[1])

    def _write(self, device, value):
        self._call(device, f"--set-ctrl={CONTROL}={value}")

    def __enter__(self):
        if self.entered:
            raise CameraControlError("会话对象不能重复进入")
        self.entered = True
        # No mutation if either camera lacks the existing control/tool/permissions.
        self.original = {device: self._read(device) for device in self.devices}
        try:
            for device in self.devices:
                if self.original[device] != self.value:
                    # Mark before calling: failed ioctl/process may have changed the control.
                    self.touched.append(device)
                    self._write(device, self.value)
                    if self._read(device) != self.value:
                        raise CameraControlError(f"控制写入后读回不一致: {device}")
        except BaseException as error:
            try:
                self.restore()
            except CameraControlError as restore_error:
                raise CameraControlError(f"{error}; 恢复也失败: {restore_error}") from error
            raise
        self.session_active = True
        return self

    def restore(self):
        if self.restore_attempted:
            return
        self.restore_attempted = True
        errors = []
        candidates = self.devices if self.session_active else self.touched
        for device in reversed(candidates):
            try:
                # A child can reopen the cameras and change even an initially
                # matching control. Restore original values, not just our writes.
                if self.session_active and self._read(device) == self.original[device]:
                    continue
                self._write(device, self.original[device])
                if self._read(device) != self.original[device]:
                    raise CameraControlError(f"恢复后读回不一致: {device}")
            except (CameraControlError, OSError, subprocess.SubprocessError) as error:
                errors.append(f"{device}: {error}")
        if errors:
            raise CameraControlError("相机原控制未确认恢复；请人工检查，不自动重试: " + "; ".join(errors))

    def __exit__(self, *_):
        self.restore()


def run_command(command):
    """只转发到自建命令进程组；子进程结束后才交还摄像头控制。"""
    child = None
    pending = []
    previous = {}

    def forward(signum, _):
        if child is None:
            pending.append(signum)
        elif child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, forward)
        child = subprocess.Popen(command, start_new_session=True)
        for sig in pending:
            forward(sig, None)
        returncode = child.wait()
        return returncode if returncode >= 0 else 128 - returncode
    finally:
        try:
            if child is not None and child.poll() is None:
                forward(signal.SIGTERM, None)
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    forward(signal.SIGKILL, None)
                    child.wait()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--front", required=True)
    parser.add_argument("--side", required=True)
    parser.add_argument("--dynamic-framerate", type=int, choices=(0, 1), required=True)
    parser.add_argument("--v4l2-ctl", default="/usr/bin/v4l2-ctl")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- 后的现有采集/推理命令")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("必须提供 -- 后的命令；不单独留下永久相机设置")
    if sys.platform != "linux":
        parser.error("此可选入口仅用于已有 v4l2-ctl 的 Linux 板端")
    try:
        devices = [str(Path(path).resolve(strict=True)) for path in (args.front, args.side)]
    except OSError as error:
        parser.error(f"摄像头路径不可用: {error}")
    if not all(Path(path).is_char_device() for path in devices):
        parser.error("相机必须是当前存在的字符设备")
    if os.stat(devices[0]).st_rdev == os.stat(devices[1]).st_rdev:
        parser.error("两条路径不能指向同一设备")
    previous = {}

    def interrupt(signum, _):
        raise CameraSessionInterrupted(signum)

    try:
        # Also protect the control-application interval before a child exists.
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, interrupt)
        with DynamicFrameRateSession(devices, args.dynamic_framerate, tool=args.v4l2_ctl) as session:
            print(f"仅本会话动态帧率={args.dynamic_framerate}；原值={session.original}；退出恢复，非30FPS保证。",
                  flush=True)
            status = run_command(command)
        print("相机原动态帧率已恢复并读回确认。", flush=True)
        return status
    except CameraSessionInterrupted as error:
        print(str(error), file=sys.stderr)
        return 128 + error.signum
    except (CameraControlError, OSError, subprocess.SubprocessError) as error:
        print(f"相机会话失败: {error}", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
