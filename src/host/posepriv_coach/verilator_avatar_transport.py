"""File-driven transport for the fixed avatar skinning packet RTL bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
from typing import Callable
from uuid import uuid4

from .avatar_skinning import MATRIX_COUNT, MATRIX_PACKET_BEATS
from .pl_metrics import AXIS_BEAT_BYTES
from .verilator_backend import command_prefix
from .verilator_transport import VerilatorBridgeError, _wsl_path


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
MATRIX_PREAMBLE_BEATS = MATRIX_COUNT * MATRIX_PACKET_BEATS


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )


@dataclass(frozen=True, slots=True)
class VerilatorAvatarSkinningTransport:
    """Send one complete matrix-preamble packet to the Verilator bridge."""

    executable: Path
    work_dir: Path
    command_runner: CommandRunner = _run
    path_mapper: Callable[[Path], str] = _wsl_path
    wsl_distribution: str = field(
        default_factory=lambda: os.environ.get("POSEPRIV_WSL_DISTRO", "Ubuntu")
    )

    def transfer(self, payload: bytes) -> bytes:
        if not payload or len(payload) % AXIS_BEAT_BYTES != 0:
            raise ValueError("avatar RTL bridge payload must be a non-empty 16-byte multiple")
        beat_count = len(payload) // AXIS_BEAT_BYTES
        if beat_count <= MATRIX_PREAMBLE_BEATS:
            raise ValueError("avatar RTL bridge packet must contain matrix preamble and vertices")
        executable = self.executable.resolve()
        if not executable.is_file():
            raise VerilatorBridgeError(f"找不到头像 Verilator 桥：{executable}")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        token = uuid4().hex
        input_path = self.work_dir / f"avatar_rtl_bridge_{token}.in"
        output_path = self.work_dir / f"avatar_rtl_bridge_{token}.out"
        lines = []
        for index in range(beat_count):
            beat = payload[index * AXIS_BEAT_BYTES : (index + 1) * AXIS_BEAT_BYTES]
            lines.append(f"{int.from_bytes(beat, 'little'):032x} {int(index == beat_count - 1)}")
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
                raise VerilatorBridgeError("头像 Verilator 桥执行失败：\n" + result.stdout.rstrip())
            if not output_path.is_file():
                raise VerilatorBridgeError("头像 Verilator 桥没有生成输出文件")
            return self._read_output(output_path, beat_count - MATRIX_PREAMBLE_BEATS)
        finally:
            input_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)

    @staticmethod
    def _read_output(path: Path, expected_beats: int) -> bytes:
        lines = [line for line in path.read_text(encoding="ascii").splitlines() if line]
        if len(lines) != expected_beats:
            raise VerilatorBridgeError(
                f"头像 RTL 桥输出拍数不匹配：{len(lines)} != {expected_beats}"
            )
        output = bytearray()
        for index, line in enumerate(lines):
            fields = line.split()
            if len(fields) != 2 or len(fields[0]) != 32 or fields[1] not in ("0", "1"):
                raise VerilatorBridgeError(f"头像 RTL 桥第 {index} 拍格式错误")
            try:
                word = int(fields[0], 16)
            except ValueError as exc:
                raise VerilatorBridgeError(f"头像 RTL 桥第 {index} 拍不是十六进制") from exc
            expected_last = index == expected_beats - 1
            if bool(int(fields[1])) != expected_last:
                raise VerilatorBridgeError(f"头像 RTL 桥第 {index} 拍 TLAST 错误")
            output.extend(word.to_bytes(AXIS_BEAT_BYTES, "little"))
        return bytes(output)
