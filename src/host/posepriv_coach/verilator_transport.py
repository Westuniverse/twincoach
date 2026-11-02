"""File-driven transport from Python bytes to the Verilated AXI metric core."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
from typing import Callable
from uuid import uuid4

from .pl_metrics import AXIS_BEAT_BYTES
from .verilator_backend import command_prefix, execution_path


class VerilatorBridgeError(RuntimeError):
    pass


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
PathMapper = Callable[[Path], str]


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )


_wsl_path = execution_path  # Backward-compatible import for existing callers.


@dataclass(frozen=True, slots=True)
class VerilatorAxisTransport:
    executable: Path
    work_dir: Path
    command_runner: CommandRunner = _run
    path_mapper: PathMapper = _wsl_path
    wsl_distribution: str = field(
        default_factory=lambda: os.environ.get("POSEPRIV_WSL_DISTRO", "Ubuntu")
    )

    def transfer(self, payload: bytes, *, expected_output_beats: int | None = None) -> bytes:
        """Run one ordered batch; memory bytes are little-endian AXI TDATA."""

        if not payload or len(payload) % AXIS_BEAT_BYTES != 0:
            raise ValueError("RTL bridge payload must be a non-empty multiple of 16 bytes")
        if expected_output_beats is not None and (
            type(expected_output_beats) is not int or expected_output_beats <= 0
        ):
            raise ValueError("expected_output_beats must be a positive integer")
        executable = self.executable.resolve()
        if not executable.is_file():
            raise VerilatorBridgeError(f"找不到 Verilator 桥：{executable}")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        token = uuid4().hex
        input_path = self.work_dir / f"rtl_bridge_{token}.in"
        output_path = self.work_dir / f"rtl_bridge_{token}.out"
        beat_count = len(payload) // AXIS_BEAT_BYTES
        lines = []
        for index in range(beat_count):
            beat = payload[index * AXIS_BEAT_BYTES : (index + 1) * AXIS_BEAT_BYTES]
            word = int.from_bytes(beat, "little")
            lines.append(f"{word:032x} {int(index == beat_count - 1)}")
        input_path.write_text("\n".join(lines) + "\n", encoding="ascii")

        try:
            distribution = self.wsl_distribution.strip()
            command = [
                *command_prefix(distribution),
                self.path_mapper(executable),
                f"+INPUT={self.path_mapper(input_path)}",
                f"+OUTPUT={self.path_mapper(output_path)}",
            ]
            result = self.command_runner(command)
            if result.returncode != 0:
                raise VerilatorBridgeError(
                    "Verilator 桥执行失败：\n" + result.stdout.rstrip()
                )
            if not output_path.is_file():
                raise VerilatorBridgeError("Verilator 桥没有生成输出文件")
            return self._read_output(
                output_path, beat_count if expected_output_beats is None else expected_output_beats
            )
        finally:
            input_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)

    @staticmethod
    def _read_output(path: Path, expected_beats: int) -> bytes:
        lines = [line for line in path.read_text(encoding="ascii").splitlines() if line]
        if len(lines) != expected_beats:
            raise VerilatorBridgeError(
                f"RTL 桥输出拍数不匹配：{len(lines)} != {expected_beats}"
            )
        output = bytearray()
        for index, line in enumerate(lines):
            fields = line.split()
            if len(fields) != 2 or len(fields[0]) != 32 or fields[1] not in ("0", "1"):
                raise VerilatorBridgeError(f"RTL 桥第 {index} 拍格式错误")
            try:
                word = int(fields[0], 16)
            except ValueError as exc:
                raise VerilatorBridgeError(f"RTL 桥第 {index} 拍不是十六进制") from exc
            expected_last = index == expected_beats - 1
            if bool(int(fields[1])) != expected_last:
                raise VerilatorBridgeError(f"RTL 桥第 {index} 拍 TLAST 错误")
            output.extend(word.to_bytes(AXIS_BEAT_BYTES, "little"))
        return bytes(output)


# Compatibility alias for existing joint-metric callers.  The transport is
# width/transaction generic and is also used by the temporal-motion bridge.
VerilatorJointMetricTransport = VerilatorAxisTransport


__all__ = [
    "CommandRunner",
    "PathMapper",
    "VerilatorAxisTransport",
    "VerilatorBridgeError",
    "VerilatorJointMetricTransport",
]
