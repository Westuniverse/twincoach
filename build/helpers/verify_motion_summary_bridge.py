#!/usr/bin/env python3
"""对拍 MOTION-SUMMARY v1 坐标字节与实际 Verilated 复合核。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from posepriv_coach.pl_metrics import AxisBeat  # noqa: E402
from posepriv_coach.pl_motion import (  # noqa: E402
    MotionMetricRequest,
    pack_motion_request,
    unpack_motion_stats_result,
)
from posepriv_coach.pl_motion_summary import reference_motion_summary_output  # noqa: E402
from posepriv_coach.verilator_transport import VerilatorAxisTransport  # noqa: E402


def verification_batches() -> tuple[tuple[AxisBeat, ...], ...]:
    batches = []
    for batch_index, size in enumerate((1, 2, 17, 257, 8192)):
        beats = []
        for index in range(size):
            if size == 8192 or index % 5 == 1:
                coordinates = (-32768, -32768, 32767, 32767, -32768, -32768)
            elif index % 5 == 0:
                coordinates = (0,) * 6
            else:
                coordinates = tuple(
                    ((index * multiplier + offset) & 0xFFFF) - 32768
                    for multiplier, offset in (
                        (7919, 13), (4051, 173), (9013, 217),
                        (1607, 791), (6553, 8191), (3253, 32761),
                    )
                )
            request = MotionMetricRequest(
                *coordinates, joint_id=(255 + batch_index * 47 + index) & 255,
                view_id=batch_index & 1,
            )
            beats.append(pack_motion_request(request, last=index == size - 1))
        batches.append(tuple(beats))
    return tuple(batches)


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    # Do not depend on a global WSL installation/PATH selection.
    if command[0] == "wsl.exe":
        command = ["C:/Windows/System32/wsl.exe", *command[1:]]
    return subprocess.run(
        command, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
    )


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, default=(
        ROOT / "build/verilator/temporal_motion_summary_axis_ip"
        / "Vtb_temporal_motion_summary_axis_ip"
    ))
    parser.add_argument("--work-dir", type=Path, default=ROOT / "build/motion_summary_bridge")
    parser.add_argument("--wsl-distro")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "config/rtl_targets.json").read_text(encoding="utf-8"))
    transport = VerilatorAxisTransport(
        args.executable, args.work_dir, command_runner=run_command,
        wsl_distribution=args.wsl_distro or manifest["wsl_distribution"],
    )
    records = []
    for beats in verification_batches():
        golden = reference_motion_summary_output(beats)
        payload = b"".join(beat.data for beat in beats)
        actual = transport.transfer(payload, expected_output_beats=1)
        if actual != golden.data:
            raise RuntimeError(f"summary byte mismatch for {len(beats)} input beats")
        summary = unpack_motion_stats_result(AxisBeat(actual, last=True))
        if summary.sample_count != len(beats) or summary.first_joint_id != beats[0].data[12]:
            raise RuntimeError("summary count/metadata mismatch")
        records.append({
            "input_beats": len(beats), "tx_bytes": len(payload), "rx_bytes": len(actual),
            "sum_acceleration_sq": summary.sum_acceleration_sq,
            "max_acceleration_sq": summary.max_acceleration_sq,
            "first_joint_id": summary.first_joint_id, "view_id": summary.view_id,
            "byte_equal": True,
        })
    print(json.dumps({
        "evidence": "host_to_verilated_rtl_not_board_dma",
        "contract": "MOTION-SUMMARY v1", "batches": records,
        "total_input_beats": sum(row["input_beats"] for row in records),
        "total_output_beats": len(records),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
