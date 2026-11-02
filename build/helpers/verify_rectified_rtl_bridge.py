#!/usr/bin/env python3
"""对拍校正双目三角测量的 Python 黄金模型与真实 RTL bridge。"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from posepriv_coach import (  # noqa: E402
    PackedDmaRectifiedStereoBackend,
    RectifiedStereoRequest,
    VerilatorAxisTransport,
    pack_rectified_stereo_result,
    reference_rectified_stereo_output,
)


def verification_requests() -> tuple[RectifiedStereoRequest, ...]:
    """覆盖符号、无效视差、阈值边界和可复现输入空间。"""

    directed = (
        RectifiedStereoRequest(819, 410, -819, 410, 3277, 16384, 7, 240, 230),
        RectifiedStereoRequest(100, 200, 100, 200, 3277, 16384, 8, 255, 255),
        RectifiedStereoRequest(-1000, 500, 1000, -500, -3277, 16384, 9, 64, 200),
        RectifiedStereoRequest(32767, -32768, -32768, 32767, 32767, -32768, 10, 255, 64),
    )
    state = 0x504F5345

    def next_signed_16() -> int:
        nonlocal state
        state = (1_664_525 * state + 1_013_904_223) & 0xFFFFFFFF
        return ((state >> 16) & 0xFFFF) - 32768

    generated = tuple(
        RectifiedStereoRequest(
            *(next_signed_16() for _ in range(6)),
            joint_id=index & 0xFF,
            front_confidence=(index * 37) & 0xFF,
            side_confidence=(255 - index * 19) & 0xFF,
            last=False,
        )
        for index in range(256)
    )
    return directed + generated


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="校正三角测量 Python/RTL 字节对拍")
    parser.add_argument(
        "--executable",
        type=Path,
        help="Verilator bridge 可执行文件；默认使用 manifest 目标产物",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=ROOT / "tmp",
        help="bridge 输入/输出临时文件目录",
    )
    args = parser.parse_args()
    executable = args.executable or (
        ROOT
        / "build"
        / "verilator"
        / "rectified_triangulation_axis_bridge"
        / "Vtb_rectified_triangulation_axis_bridge"
    )
    executable = executable.resolve()
    if not executable.is_file():
        print(
            "缺少校正三角测量 Verilator bridge："
            f"{executable}\n"
            "请先运行 python scripts/run_rtl_checks.py "
            "--target rectified_triangulation_axis_bridge",
            file=sys.stderr,
        )
        return 2
    requests = verification_requests()
    requests = tuple(
        replace(request, last=index == len(requests) - 1)
        for index, request in enumerate(requests)
    )
    expected = b"".join(
        pack_rectified_stereo_result(reference_rectified_stereo_output(request))
        for request in requests
    )
    transport = VerilatorAxisTransport(executable, args.work_dir)
    backend = PackedDmaRectifiedStereoBackend(transport.transfer)
    actual_results = backend.exchange(requests)
    actual = b"".join(pack_rectified_stereo_result(result) for result in actual_results)
    if actual != expected:
        raise RuntimeError("校正三角测量 Verilated RTL 输出与 Python 黄金模型不一致")
    if tuple(result.last for result in actual_results) != (
        (False,) * (len(actual_results) - 1) + (True,)
    ):
        raise RuntimeError("校正三角测量 RTL bridge 的 TLAST 与批次边界不一致")
    stats = backend.stats()
    print(
        f"校正三角测量 RTL 对拍通过：{len(requests)} 拍，"
        f"TX/RX={stats.tx_bytes}/{stats.rx_bytes} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
