"""Parse the small, useful part of ``lsusb -t`` for UVC camera diagnosis."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True, slots=True)
class UsbCameraNode:
    """One Video/UVC node and the root bus it consumes bandwidth from."""

    bus: int
    port: str
    speed_mbps: int | None
    driver: str | None
    root_bus: int
    root_speed_mbps: int | None
    raw_line: str

    def to_record(self) -> dict[str, Any]:
        return {
            "bus": self.bus,
            "port": self.port,
            "speed_mbps": self.speed_mbps,
            "driver": self.driver,
            "root_bus": self.root_bus,
            "root_speed_mbps": self.root_speed_mbps,
            "raw_line": self.raw_line,
        }


@dataclass(frozen=True, slots=True)
class UsbCameraTopology:
    """A compact diagnosis of the UVC nodes found in ``lsusb -t``."""

    cameras: tuple[UsbCameraNode, ...]
    root_bus_counts: tuple[tuple[int, int], ...]
    shared_root_bus: bool
    recommendation: str

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "camera_count": len(self.cameras),
            "cameras": [camera.to_record() for camera in self.cameras],
            "root_bus_counts": {
                str(bus): count for bus, count in self.root_bus_counts
            },
            "shared_root_bus": self.shared_root_bus,
            "recommendation": self.recommendation,
        }


_ROOT_RE = re.compile(
    r"^\s*/:\s+Bus\s+(?P<bus>\d+)\.Port\s+\d+:.*?(?P<speed>\d+)M\s*$"
)
_PORT_RE = re.compile(
    r"^\s*(?:\|__\s*)?Port\s+(?P<port>[0-9.]+):.*?"
    r"Class=(?P<class>[^,]+).*?(?:Driver=(?P<driver>[^,]+),)?\s*"
    r"(?P<speed>\d+)M\s*$"
)


def parse_lsusb_camera_topology(text: str) -> UsbCameraTopology:
    """Extract Video/UVC children while tolerating hub and spacing variants."""

    current_bus: int | None = None
    current_root_speed: int | None = None
    cameras: list[UsbCameraNode] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        root_match = _ROOT_RE.match(line)
        if root_match is not None:
            current_bus = int(root_match.group("bus"))
            current_root_speed = int(root_match.group("speed"))
            continue
        port_match = _PORT_RE.match(line)
        if port_match is None or current_bus is None:
            continue
        class_name = port_match.group("class").strip().lower()
        driver = port_match.group("driver")
        if "video" not in class_name and driver != "uvcvideo":
            continue
        cameras.append(
            UsbCameraNode(
                bus=current_bus,
                port=port_match.group("port"),
                speed_mbps=int(port_match.group("speed")),
                driver=driver.strip() if driver else None,
                root_bus=current_bus,
                root_speed_mbps=current_root_speed,
                raw_line=line,
            )
        )

    counts: dict[int, int] = {}
    for camera in cameras:
        counts[camera.root_bus] = counts.get(camera.root_bus, 0) + 1
    root_bus_counts = tuple(sorted(counts.items()))
    shared = len(cameras) >= 2 and len(counts) == 1
    if not cameras:
        recommendation = "未发现 Video/UVC 节点；先确认摄像头已连接并加载 uvcvideo。"
    elif shared:
        recommendation = (
            "两路摄像头共享同一个 USB 根总线；物理插槽分开不等于带宽分开，"
            "先降低模式或改用独立根控制器/CSI-2。"
        )
    elif len(cameras) >= 2:
        recommendation = "两路摄像头至少分属不同根总线，可继续比较实际协商模式和端到端帧率。"
    else:
        recommendation = "当前只发现一路 Video/UVC 节点，不能进行双摄带宽结论。"
    return UsbCameraTopology(
        cameras=tuple(cameras),
        root_bus_counts=root_bus_counts,
        shared_root_bus=shared,
        recommendation=recommendation,
    )


__all__ = ["UsbCameraNode", "UsbCameraTopology", "parse_lsusb_camera_topology"]

