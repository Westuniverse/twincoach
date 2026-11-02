#!/usr/bin/env python3
"""混合算子批次的 Python→真实 Verilated RTL 字节对拍，不是板端 DMA。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from posepriv_coach.pl_metrics import AxisBeat, JointMetricRequest, pack_joint_request
from posepriv_coach.pl_shared_pose_accel import reference_shared_pose_output
from posepriv_coach.rectified_stereo import pack_rectified_stereo_request
from posepriv_coach.verilator_backend import command_prefix
from posepriv_coach.verilator_transport import VerilatorAxisTransport
from verify_motion_summary_bridge import verification_batches as summary_batches
from verify_rectified_rtl_bridge import verification_requests as rectified_requests
from make_avatar_triangle_lighting_fixture import cases as lighting_cases
from posepriv_coach.avatar_triangle_lighting_packet import LightingRequest, pack_input_batch


def verification_batches(*, include_lighting: bool = False) -> tuple[tuple[int, tuple[AxisBeat, ...]], ...]:
    """交替模式、小包/极值/最大包，沿用已验证算术的确定性输入空间。"""
    joints = []
    for size in (1, 17, 257, 8192):
        beats = []
        for index in range(size):
            coordinates = tuple(((index * 7919 + lane * 10973) & 0xFFFF) - 32768
                                for lane in range(6))
            request = JointMetricRequest(*coordinates, index & 255, index & 1)
            beats.append(pack_joint_request(request, last=index == size-1))
        joints.append(tuple(beats))
    requests = rectified_requests()
    rectified = tuple(AxisBeat(pack_rectified_stereo_request(request), index == len(requests)-1)
                      for index, request in enumerate(requests))
    summaries = summary_batches()
    lighting = []
    if include_lighting:
        fixtures = list(lighting_cases())
        for size in (1, 17, len(fixtures), 4096):
            payload = pack_input_batch(tuple(LightingRequest(*fixtures[index % len(fixtures)], index)
                for index in range(size)))
            lighting.append(tuple(AxisBeat(payload[offset:offset+16], offset+16 == len(payload))
                for offset in range(0, len(payload), 16)))
    batches = []
    for index, summary in enumerate(summaries):
        batches.append((1, summary))
        if index < len(joints):
            batches.append((0, joints[index]))
        if index == 0:
            batches.append((2, (AxisBeat(rectified[0].data, True),)))
        elif index == 1:
            batches.append((2, rectified))
        if index < len(lighting):
            batches.append((3, lighting[index]))
    return tuple(batches)


def verify_output(path: Path, expected: tuple[tuple[int, AxisBeat], ...]) -> None:
    lines = path.read_text(encoding="ascii").splitlines()
    if len(lines) != len(expected):
        raise RuntimeError(f"shared bridge output count {len(lines)} != {len(expected)}")
    for index, (line, (operator, beat)) in enumerate(zip(lines, expected)):
        fields = line.split()
        if len(fields) != 3 or fields[0] != str(operator) or fields[2] != str(int(beat.last)):
            raise RuntimeError(f"shared bridge mode/TLAST mismatch at output {index}")
        try:
            word = int(fields[1], 16)
        except ValueError as error:
            raise RuntimeError(f"shared bridge invalid hex at output {index}") from error
        if len(fields[1]) != 32 or not 0 <= word < (1 << 128) or word.to_bytes(16, "little") != beat.data:
            raise RuntimeError(f"shared bridge byte mismatch at output {index}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, default=(
        ROOT / "build/verilator/shared_pose_accel_axis/Vtb_shared_pose_accel_axis"))
    parser.add_argument("--work-dir", type=Path, default=ROOT / "build/shared_pose_bridge")
    parser.add_argument("--wsl-distro")
    args = parser.parse_args()
    if not args.executable.is_file():
        parser.error("run scripts/run_rtl_checks.py --target shared_pose_accel_axis first")
    manifest = json.loads((ROOT / "config/rtl_targets.json").read_text(encoding="utf-8"))
    distribution = args.wsl_distro or manifest["wsl_distribution"]
    mapper = VerilatorAxisTransport(args.executable, args.work_dir).path_mapper
    args.work_dir.mkdir(parents=True, exist_ok=True)
    input_path = args.work_dir / "shared_pose.in"
    output_path = args.work_dir / "shared_pose.out"
    # 离线新 RTL 必测四模式；复用者默认仍给现役 PPS1 提供三模式。
    batches = verification_batches(include_lighting=True)
    expected = tuple((operator, beat) for operator, inputs in batches
                     for beat in reference_shared_pose_output(operator, inputs))
    input_path.write_text("".join(f"{operator} {int.from_bytes(beat.data, 'little'):032x} {int(beat.last)}\n"
                                 for operator, inputs in batches for beat in inputs), encoding="ascii")
    # A failed run cannot accidentally reuse an earlier output file.
    output_path.unlink(missing_ok=True)
    result = subprocess.run([
        *command_prefix(distribution),
        mapper(args.executable), f"+INPUT={mapper(input_path)}", f"+OUTPUT={mapper(output_path)}",
    ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace", timeout=60)
    print(result.stdout, end="")
    if result.returncode != 0 or "SHARED_POSE_BRIDGE PASS" not in result.stdout:
        raise RuntimeError("shared RTL simulation failed or lacks self-check completion")
    verify_output(output_path, expected)
    report = {
        "evidence": "python_to_verilated_rtl_not_board_dma",
        "packets": len(batches), "input_beats": sum(len(inputs) for _, inputs in batches),
        "output_beats": len(expected), "byte_equal": True,
        "batches": [{"operator": op, "input_beats": len(inputs),
                     "output_beats": len(reference_shared_pose_output(op, inputs))}
                    for op, inputs in batches],
    }
    (args.work_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n",
                                              encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
