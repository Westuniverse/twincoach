"""从 Vivado XSA/HWH 提取 Linux 驱动需要的双 DMA 硬件事实。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile


@dataclass(frozen=True, slots=True)
class DmaHardwareContract:
    instance: str
    base_address: int
    high_address: int
    clock_hz: int
    memory_map_width_bits: int
    stream_width_bits: int
    mm2s_irq_vector_index: int
    s2mm_irq_vector_index: int
    stream_target: str


@dataclass(frozen=True, slots=True)
class DualAccelHardwareContract:
    source_hwh: str
    interrupt_sink: str
    avatar_dma: DmaHardwareContract
    joint_dma: DmaHardwareContract

    def to_record(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TripleAccelHardwareContract:
    """同一份 XSA 中三套 PL 算子 DMA 的硬件契约。"""

    source_hwh: str
    interrupt_sink: str
    avatar_dma: DmaHardwareContract
    joint_dma: DmaHardwareContract
    rectified_dma: DmaHardwareContract

    def to_record(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class QuadAccelHardwareContract:
    """同一份 XSA 中四路 PL 算子 DMA 的硬件契约。

    前三路地址和中断顺序与 ``TripleAccelHardwareContract`` 保持兼容，
    第四路用于时间运动统计。这个契约只描述可由 HWH 直接证明的接口，
    不把摄像头、DPU 或 Linux 驱动状态混入其中。
    """

    source_hwh: str
    interrupt_sink: str
    avatar_dma: DmaHardwareContract
    joint_dma: DmaHardwareContract
    rectified_dma: DmaHardwareContract
    motion_stats_dma: DmaHardwareContract

    def to_record(self) -> dict[str, object]:
        return asdict(self)


def extract_dual_accel_contract(
    source: str | Path,
    *,
    avatar_dma_instance: str = "axi_dma_avatar",
    joint_dma_instance: str = "axi_dma_joint",
) -> DualAccelHardwareContract:
    """Read the top-level HWH from a HWH file or XSA zip archive."""

    path = Path(source)
    if path.suffix.lower() == ".xsa":
        with ZipFile(path) as archive:
            hwh_names = tuple(
                name
                for name in archive.namelist()
                if name.lower().endswith(".hwh") and "/" not in name.strip("/")
            )
            if not hwh_names:
                raise ValueError("XSA 中没有顶层 HWH")
            preferred = f"{path.stem}.hwh"
            hwh_name = preferred if preferred in hwh_names else hwh_names[0]
            root = ElementTree.fromstring(archive.read(hwh_name))
    else:
        hwh_name = path.name
        root = ElementTree.parse(path).getroot()

    modules = {
        module.attrib["INSTANCE"]: module
        for module in root.findall("./MODULES/MODULE")
        if "INSTANCE" in module.attrib
    }
    concat = next(
        (
            module
            for module in modules.values()
            if module.attrib.get("MODTYPE") == "xlconcat"
            and _parameter(module, "NUM_PORTS") == "4"
        ),
        None,
    )
    if concat is None:
        raise ValueError("HWH 中没有四路 DMA 中断拼接器")

    return DualAccelHardwareContract(
        source_hwh=hwh_name,
        interrupt_sink=_connected_endpoint(concat, "dout"),
        avatar_dma=_dma_contract(modules, concat, avatar_dma_instance),
        joint_dma=_dma_contract(modules, concat, joint_dma_instance),
    )


def extract_triple_accel_contract(
    source: str | Path,
    *,
    avatar_dma_instance: str = "axi_dma_avatar",
    joint_dma_instance: str = "axi_dma_joint",
    rectified_dma_instance: str = "axi_dma_rectified",
) -> TripleAccelHardwareContract:
    """Read a triple-accelerator HWH/XSA and resolve all six IRQ inputs."""

    hwh_name, root = _load_hwh(source)
    modules = {
        module.attrib["INSTANCE"]: module
        for module in root.findall("./MODULES/MODULE")
        if "INSTANCE" in module.attrib
    }
    concat = next(
        (
            module
            for module in modules.values()
            if module.attrib.get("MODTYPE") == "xlconcat"
            and _parameter(module, "NUM_PORTS") == "6"
        ),
        None,
    )
    if concat is None:
        raise ValueError("HWH 中没有六路 DMA 中断拼接器")

    return TripleAccelHardwareContract(
        source_hwh=hwh_name,
        interrupt_sink=_connected_endpoint(concat, "dout"),
        avatar_dma=_dma_contract(modules, concat, avatar_dma_instance),
        joint_dma=_dma_contract(modules, concat, joint_dma_instance),
        rectified_dma=_dma_contract(modules, concat, rectified_dma_instance),
    )


def extract_quad_accel_contract(
    source: str | Path,
    *,
    avatar_dma_instance: str = "axi_dma_avatar",
    joint_dma_instance: str = "axi_dma_joint",
    rectified_dma_instance: str = "axi_dma_rectified",
    motion_stats_dma_instance: str = "axi_dma_motion_stats",
) -> QuadAccelHardwareContract:
    """Read a four-accelerator HWH/XSA and resolve all eight IRQ inputs."""

    hwh_name, root = _load_hwh(source)
    modules = {
        module.attrib["INSTANCE"]: module
        for module in root.findall("./MODULES/MODULE")
        if "INSTANCE" in module.attrib
    }
    concat = next(
        (
            module
            for module in modules.values()
            if module.attrib.get("MODTYPE") == "xlconcat"
            and _parameter(module, "NUM_PORTS") == "8"
        ),
        None,
    )
    if concat is None:
        raise ValueError("HWH 中没有八路 DMA 中断拼接器")

    return QuadAccelHardwareContract(
        source_hwh=hwh_name,
        interrupt_sink=_connected_endpoint(concat, "dout"),
        avatar_dma=_dma_contract(modules, concat, avatar_dma_instance),
        joint_dma=_dma_contract(modules, concat, joint_dma_instance),
        rectified_dma=_dma_contract(modules, concat, rectified_dma_instance),
        motion_stats_dma=_dma_contract(modules, concat, motion_stats_dma_instance),
    )


def _load_hwh(source: str | Path) -> tuple[str, ElementTree.Element]:
    """Read the top-level HWH from a HWH file or XSA zip archive."""

    path = Path(source)
    if path.suffix.lower() == ".xsa":
        with ZipFile(path) as archive:
            hwh_names = tuple(
                name
                for name in archive.namelist()
                if name.lower().endswith(".hwh") and "/" not in name.strip("/")
            )
            if not hwh_names:
                raise ValueError("XSA 中没有顶层 HWH")
            preferred = f"{path.stem}.hwh"
            hwh_name = preferred if preferred in hwh_names else hwh_names[0]
            root = ElementTree.fromstring(archive.read(hwh_name))
    else:
        hwh_name = path.name
        root = ElementTree.parse(path).getroot()
    return hwh_name, root


def _dma_contract(
    modules: dict[str, ElementTree.Element],
    concat: ElementTree.Element,
    instance: str,
) -> DmaHardwareContract:
    try:
        dma = modules[instance]
    except KeyError as exc:
        raise ValueError(f"HWH 中没有 DMA 实例：{instance}") from exc
    if dma.attrib.get("MODTYPE") != "axi_dma":
        raise ValueError(f"{instance} 不是 AXI DMA")

    return DmaHardwareContract(
        instance=instance,
        base_address=int(_required_parameter(dma, "C_BASEADDR"), 0),
        high_address=int(_required_parameter(dma, "C_HIGHADDR"), 0),
        clock_hz=int(_required_port_attribute(dma, "s_axi_lite_aclk", "CLKFREQUENCY")),
        memory_map_width_bits=int(_required_parameter(dma, "C_M_AXI_MM2S_DATA_WIDTH")),
        stream_width_bits=int(_required_parameter(dma, "C_M_AXIS_MM2S_TDATA_WIDTH")),
        mm2s_irq_vector_index=_irq_vector_index(concat, instance, "mm2s_introut"),
        s2mm_irq_vector_index=_irq_vector_index(concat, instance, "s2mm_introut"),
        stream_target=_connected_bus_endpoint(modules, dma, "M_AXIS_MM2S"),
    )


def _parameter(module: ElementTree.Element, name: str) -> str | None:
    item = module.find(f"./PARAMETERS/PARAMETER[@NAME='{name}']")
    return None if item is None else item.attrib.get("VALUE")


def _required_parameter(module: ElementTree.Element, name: str) -> str:
    value = _parameter(module, name)
    if value is None:
        raise ValueError(f"{module.attrib.get('INSTANCE')} 缺少参数 {name}")
    return value


def _required_port_attribute(
    module: ElementTree.Element,
    port_name: str,
    attribute: str,
) -> str:
    port = module.find(f"./PORTS/PORT[@NAME='{port_name}']")
    if port is None or attribute not in port.attrib:
        raise ValueError(f"{module.attrib.get('INSTANCE')}/{port_name} 缺少 {attribute}")
    return port.attrib[attribute]


def _connected_endpoint(module: ElementTree.Element, port_name: str) -> str:
    port = module.find(f"./PORTS/PORT[@NAME='{port_name}']")
    connection = None if port is None else port.find("./CONNECTIONS/CONNECTION")
    if connection is None:
        raise ValueError(f"{module.attrib.get('INSTANCE')}/{port_name} 没有连接")
    return f"{connection.attrib['INSTANCE']}/{connection.attrib['PORT']}"


def _connected_bus_endpoint(
    modules: dict[str, ElementTree.Element],
    module: ElementTree.Element,
    interface_name: str,
) -> str:
    """Resolve an HWH bus connection by its shared BUSNAME."""

    interface = module.find(
        f"./BUSINTERFACES/BUSINTERFACE[@NAME='{interface_name}']"
    )
    if interface is None or "BUSNAME" not in interface.attrib:
        raise ValueError(
            f"{module.attrib.get('INSTANCE')}/{interface_name} 缺少 BUSNAME"
        )
    bus_name = interface.attrib["BUSNAME"]
    instance = module.attrib.get("INSTANCE")
    for peer_instance, peer_module in modules.items():
        if peer_instance == instance:
            continue
        for peer in peer_module.findall("./BUSINTERFACES/BUSINTERFACE"):
            if peer.attrib.get("BUSNAME") == bus_name:
                return f"{peer_instance}/{peer.attrib['NAME']}"
    raise ValueError(f"总线 {bus_name} 没有对端接口")


def _irq_vector_index(
    concat: ElementTree.Element,
    dma_instance: str,
    dma_port: str,
) -> int:
    for port in concat.findall("./PORTS/PORT"):
        connection = port.find("./CONNECTIONS/CONNECTION")
        if (
            connection is not None
            and connection.attrib.get("INSTANCE") == dma_instance
            and connection.attrib.get("PORT") == dma_port
        ):
            name = port.attrib.get("NAME", "")
            if name.startswith("In"):
                return int(name[2:])
    raise ValueError(f"找不到 {dma_instance}/{dma_port} 的中断向量位置")


__all__ = [
    "DmaHardwareContract",
    "DualAccelHardwareContract",
    "TripleAccelHardwareContract",
    "QuadAccelHardwareContract",
    "extract_dual_accel_contract",
    "extract_triple_accel_contract",
    "extract_quad_accel_contract",
]
