"""四个主机入口共用的校正双摄三角测量命令行参数。"""

from __future__ import annotations

from argparse import ArgumentParser, Namespace

from .avatar_pipeline import TRIANGULATION_BACKENDS
from .avatar_setup import RECTIFIED_ACCELERATOR_BACKENDS


def add_triangulation_arguments(parser: ArgumentParser) -> None:
    """Register optional rectified-reference settings without changing defaults."""

    parser.add_argument(
        "--avatar-triangulation-backend",
        choices=TRIANGULATION_BACKENDS,
        default="general",
        help="3D 三角测量后端；rectified-reference 需要已校正双摄参数",
    )
    parser.add_argument(
        "--avatar-rectified-baseline",
        type=float,
        help="校正双摄基线（世界坐标单位）；仅 rectified-reference 使用",
    )
    parser.add_argument(
        "--avatar-rectified-focal",
        type=float,
        help="校正坐标的焦距尺度；相机归一化坐标通常填写 1.0",
    )
    parser.add_argument(
        "--avatar-rectified-min-disparity",
        type=float,
        default=64.0 / (1 << 14),
        help="校正坐标最小水平视差；默认对应 Q1.14 原始值 64",
    )
    parser.add_argument(
        "--avatar-rectified-max-vertical-disparity",
        type=float,
        default=0.02,
        help="校正坐标允许的最大垂直视差，默认 0.02",
    )
    parser.add_argument(
        "--avatar-triangulation-accelerator",
        choices=RECTIFIED_ACCELERATOR_BACKENDS,
        default="reference",
        help="校正三角测量的执行后端；verilator 用于离线 RTL 对拍，Linux 后端通过字符设备传输 AXI 批次",
    )
    parser.add_argument(
        "--avatar-triangulation-verilator-executable",
        help="校正三角测量 Verilator bridge 可执行文件；仅 verilator 后端使用",
    )
    parser.add_argument(
        "--avatar-triangulation-verilator-work-dir",
        help="校正三角测量 Verilator bridge 临时目录；默认当前目录下 tmp",
    )
    parser.add_argument(
        "--avatar-triangulation-dma-device",
        help="校正三角测量单设备 DMA 字符设备路径",
    )
    parser.add_argument(
        "--avatar-triangulation-dma-proxy-tx",
        help="校正三角测量 dma-proxy TX 字符设备路径",
    )
    parser.add_argument(
        "--avatar-triangulation-dma-proxy-rx",
        help="校正三角测量 dma-proxy RX 字符设备路径",
    )


def triangulation_argument_error(args: Namespace) -> str | None:
    """Return a concise parser error, or ``None`` when settings are valid."""

    accelerator = getattr(args, "avatar_triangulation_accelerator", "reference")
    if accelerator not in RECTIFIED_ACCELERATOR_BACKENDS:
        return f"不支持的校正三角测量后端：{accelerator}"
    if accelerator != "reference" and args.avatar_triangulation_backend != "rectified-reference":
        return "Linux 校正三角测量后端需要 --avatar-triangulation-backend rectified-reference"
    if args.avatar_triangulation_backend == "rectified-reference":
        if args.avatar_rectified_baseline is None:
            return "rectified-reference 需要 --avatar-rectified-baseline"
        if args.avatar_rectified_focal is None:
            return "rectified-reference 需要 --avatar-rectified-focal"
    if args.avatar_rectified_min_disparity <= 0.0:
        return "--avatar-rectified-min-disparity 必须为正数"
    if args.avatar_rectified_max_vertical_disparity < 0.0:
        return "--avatar-rectified-max-vertical-disparity 不能为负数"
    dma_device = getattr(args, "avatar_triangulation_dma_device", None)
    proxy_tx = getattr(args, "avatar_triangulation_dma_proxy_tx", None)
    proxy_rx = getattr(args, "avatar_triangulation_dma_proxy_rx", None)
    if accelerator == "linux-dma" and not dma_device:
        return "linux-dma 需要 --avatar-triangulation-dma-device"
    if accelerator == "linux-dma-proxy" and (not proxy_tx or not proxy_rx):
        return "linux-dma-proxy 需要 TX/RX 字符设备路径"
    if accelerator == "verilator" and not getattr(
        args, "avatar_triangulation_verilator_executable", None
    ):
        return "verilator 校正三角测量后端需要 --avatar-triangulation-verilator-executable"
    return None


def triangulation_setup_kwargs(args: Namespace) -> dict[str, object]:
    """Map parsed options to ``load_avatar_runtime_setup`` keyword names."""

    return {
        "triangulation_backend": args.avatar_triangulation_backend,
        "rectified_baseline": args.avatar_rectified_baseline,
        "rectified_focal": args.avatar_rectified_focal,
        "rectified_minimum_disparity": args.avatar_rectified_min_disparity,
        "rectified_maximum_vertical_disparity": args.avatar_rectified_max_vertical_disparity,
        "rectified_accelerator_backend": getattr(
            args, "avatar_triangulation_accelerator", "reference"
        ),
        "rectified_verilator_executable": getattr(
            args, "avatar_triangulation_verilator_executable", None
        ),
        "rectified_verilator_work_dir": getattr(
            args, "avatar_triangulation_verilator_work_dir", None
        ),
        "rectified_dma_device": getattr(args, "avatar_triangulation_dma_device", None),
        "rectified_dma_proxy_tx_device": getattr(
            args, "avatar_triangulation_dma_proxy_tx", None
        ),
        "rectified_dma_proxy_rx_device": getattr(
            args, "avatar_triangulation_dma_proxy_rx", None
        ),
    }


def triangulation_report_configuration(args: Namespace) -> dict[str, object]:
    """Return explicit report fields without exposing pixel/keypoint data."""

    return {
        "triangulation_backend": args.avatar_triangulation_backend,
        "rectified_baseline": args.avatar_rectified_baseline,
        "rectified_focal": args.avatar_rectified_focal,
        "rectified_minimum_disparity": args.avatar_rectified_min_disparity,
        "rectified_maximum_vertical_disparity": args.avatar_rectified_max_vertical_disparity,
        "rectified_accelerator_backend": getattr(
            args, "avatar_triangulation_accelerator", "reference"
        ),
        "rectified_verilator_executable": getattr(
            args, "avatar_triangulation_verilator_executable", None
        ),
        "rectified_verilator_work_dir": getattr(
            args, "avatar_triangulation_verilator_work_dir", None
        ),
        "rectified_dma_device": getattr(args, "avatar_triangulation_dma_device", None),
        "rectified_dma_proxy_tx_device": getattr(
            args, "avatar_triangulation_dma_proxy_tx", None
        ),
        "rectified_dma_proxy_rx_device": getattr(
            args, "avatar_triangulation_dma_proxy_rx", None
        ),
    }


__all__ = [
    "add_triangulation_arguments",
    "triangulation_argument_error",
    "triangulation_report_configuration",
    "triangulation_setup_kwargs",
]
