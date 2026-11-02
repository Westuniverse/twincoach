"""Select the joint-metric implementation used by a host application."""

from __future__ import annotations

from pathlib import Path
from functools import partial

from .linux_shared_pose_dma import LinuxSharedPoseDma

from .linux_dma_device import DeviceOpener
from .linux_joint_dma import (
    LinuxJointMetricDmaProxyTransport,
    LinuxJointMetricDmaTransport,
)
from .pl_metrics import (
    JointMetricBackend,
    PackedDmaJointMetricBackend,
    ReferenceJointMetricBackend,
)
from .verilator_transport import VerilatorAxisTransport, VerilatorBridgeError


JOINT_BACKEND_NAMES = ("reference", "verilator", "linux-dma", "linux-dma-proxy", "linux-shared-dma")


def load_joint_metric_backend(
    name: str,
    *,
    verilator_executable: Path | None = None,
    work_dir: Path | None = None,
    dma_device: str | Path | None = None,
    dma_device_opener: DeviceOpener | None = None,
    dma_proxy_tx_device: str | Path | None = None,
    dma_proxy_rx_device: str | Path | None = None,
    shared_dma: LinuxSharedPoseDma | None = None,
) -> JointMetricBackend:
    """Create a software, Verilated or Linux-device joint-metric backend."""

    if name == "reference":
        return ReferenceJointMetricBackend()
    if name == "linux-shared-dma":
        if shared_dma is None:
            raise ValueError("linux-shared-dma requires an application-owned shared_dma transport")
        return PackedDmaJointMetricBackend(partial(shared_dma.exchange_bytes, 0))
    if name == "linux-dma":
        if dma_device is None:
            raise ValueError("linux-dma 关节指标后端需要字符设备路径")
        transport_options = {}
        if dma_device_opener is not None:
            transport_options["opener"] = dma_device_opener
        transport = LinuxJointMetricDmaTransport(dma_device, **transport_options)
        return PackedDmaJointMetricBackend(
            transport.transfer,
            close_callback=transport.close,
        )
    if name == "linux-dma-proxy":
        if dma_proxy_tx_device is None or dma_proxy_rx_device is None:
            raise ValueError("linux-dma-proxy 关节后端需要 TX 与 RX 设备路径")
        transport = LinuxJointMetricDmaProxyTransport(
            dma_proxy_tx_device,
            dma_proxy_rx_device,
        )
        return PackedDmaJointMetricBackend(
            transport.transfer,
            close_callback=transport.close,
        )
    if name != "verilator":
        raise ValueError(f"未知关节指标后端：{name}")
    if verilator_executable is None or work_dir is None:
        raise ValueError("Verilator 后端需要可执行文件和临时目录")
    executable = verilator_executable.resolve()
    if not executable.is_file():
        raise VerilatorBridgeError(
            f"找不到 Verilator 桥：{executable}；"
            "请先运行 python scripts/run_rtl_checks.py --target "
            "joint_metrics_axis_bridge"
        )
    transport = VerilatorAxisTransport(executable, work_dir)
    return PackedDmaJointMetricBackend(transport.transfer)
