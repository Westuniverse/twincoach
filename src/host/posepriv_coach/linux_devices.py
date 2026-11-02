"""Linux 设备发现接口，不假定具体的 /dev 编号。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DrmConnector:
    name: str
    status: str
    modes: tuple[str, ...]

    @property
    def connected(self) -> bool:
        return self.status == "connected"


@dataclass(frozen=True, slots=True)
class FramebufferDevice:
    path: str
    name: str
    width: int
    height: int
    virtual_height: int
    bits_per_pixel: int
    stride_bytes: int
    writable: bool

    @property
    def rgb565_compatible(self) -> bool:
        return self.bits_per_pixel == 16 and self.stride_bytes >= self.width * 2


@dataclass(frozen=True, slots=True)
class LinuxPlatformInventory:
    video_devices: tuple[str, ...]
    stable_video_paths: tuple[str, ...]
    drm_nodes: tuple[str, ...]
    drm_connectors: tuple[DrmConnector, ...]
    framebuffers: tuple[FramebufferDevice, ...]
    dma_candidates: tuple[str, ...]

    @property
    def stable_video_devices(self) -> tuple[str, ...]:
        """按符号链接最终目标去重，避免 by-id/by-path 重复计算同一摄像头。"""

        unique: dict[str, str] = {}
        for path_text in self.stable_video_paths:
            path = Path(path_text)
            try:
                identity = str(path.resolve(strict=False))
            except OSError:
                identity = str(path)
            unique.setdefault(identity, path_text)
        return tuple(unique.values())

    @property
    def dma_proxy_channels(self) -> tuple[str, ...]:
        return tuple(
            path
            for path in self.dma_candidates
            if Path(path).name.startswith(("posepriv_", "dma_proxy_"))
        )

    @property
    def joint_accel_dma_proxy_ready(self) -> bool:
        """Whether the isolated joint-metric accelerator exposes both channels."""

        names = {Path(path).name for path in self.dma_proxy_channels}
        return {
            "posepriv_joint_tx",
            "posepriv_joint_rx",
        }.issubset(names)

    @property
    def dual_accel_dma_proxy_ready(self) -> bool:
        names = {Path(path).name for path in self.dma_proxy_channels}
        return {
            "posepriv_avatar_tx",
            "posepriv_avatar_rx",
            "posepriv_joint_tx",
            "posepriv_joint_rx",
        }.issubset(names)

    @property
    def triple_accel_dma_proxy_ready(self) -> bool:
        """Whether the integrated Avatar/Joint/Rectified overlay has six channels."""

        names = {Path(path).name for path in self.dma_proxy_channels}
        return {
            "posepriv_avatar_tx",
            "posepriv_avatar_rx",
            "posepriv_joint_tx",
            "posepriv_joint_rx",
            "posepriv_rectified_tx",
            "posepriv_rectified_rx",
        }.issubset(names)

    @property
    def quad_accel_dma_proxy_ready(self) -> bool:
        """Whether the four-operator overlay exposes all eight proxy channels."""

        names = {Path(path).name for path in self.dma_proxy_channels}
        return {
            "posepriv_avatar_tx",
            "posepriv_avatar_rx",
            "posepriv_joint_tx",
            "posepriv_joint_rx",
            "posepriv_rectified_tx",
            "posepriv_rectified_rx",
            "posepriv_motion_stats_tx",
            "posepriv_motion_stats_rx",
        }.issubset(names)

    @property
    def connected_connectors(self) -> tuple[DrmConnector, ...]:
        return tuple(connector for connector in self.drm_connectors if connector.connected)

    @property
    def preferred_framebuffer(self) -> FramebufferDevice | None:
        return next(
            (device for device in self.framebuffers if device.rgb565_compatible),
            self.framebuffers[0] if self.framebuffers else None,
        )

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "video_devices": self.video_devices,
            "stable_video_paths": self.stable_video_paths,
            "drm_nodes": self.drm_nodes,
            "drm_connectors": tuple(asdict(item) for item in self.drm_connectors),
            "framebuffers": tuple(asdict(item) for item in self.framebuffers),
            "dma_candidates": self.dma_candidates,
            "derived": {
                "stable_video_device_count": len(self.stable_video_devices),
                "connected_display_count": len(self.connected_connectors),
                "framebuffer_output_ready": bool(
                    self.preferred_framebuffer
                    and self.preferred_framebuffer.rgb565_compatible
                    and self.preferred_framebuffer.writable
                ),
                "dma_proxy_channel_count": len(self.dma_proxy_channels),
                "joint_accel_dma_proxy_ready": self.joint_accel_dma_proxy_ready,
                "dual_accel_dma_proxy_ready": self.dual_accel_dma_proxy_ready,
                "triple_accel_dma_proxy_ready": self.triple_accel_dma_proxy_ready,
                "quad_accel_dma_proxy_ready": self.quad_accel_dma_proxy_ready,
            },
        }


def _read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return default


def _read_int(path: Path, default: int = 0) -> int:
    try:
        return int(_read_text(path))
    except ValueError:
        return default


def _device_paths(directory: Path, pattern: str) -> tuple[str, ...]:
    if not directory.is_dir():
        return ()
    return tuple(str(path) for path in sorted(directory.glob(pattern)))


def discover_linux_platform(
    *,
    dev_root: Path = Path("/dev"),
    sys_root: Path = Path("/sys"),
) -> LinuxPlatformInventory:
    """Discover video, display and DMA endpoints through stable Linux interfaces."""

    video_devices = _device_paths(dev_root, "video*")
    stable_video_paths = tuple(
        path
        for directory in (dev_root / "v4l" / "by-id", dev_root / "v4l" / "by-path")
        for path in _device_paths(directory, "*video-index0")
    )
    drm_nodes = _device_paths(dev_root / "dri", "card*") + _device_paths(
        dev_root / "dri", "renderD*"
    )

    drm_connectors = []
    drm_root = sys_root / "class" / "drm"
    if drm_root.is_dir():
        for status_path in sorted(drm_root.glob("card*-*/status")):
            connector_root = status_path.parent
            modes_text = _read_text(connector_root / "modes")
            drm_connectors.append(
                DrmConnector(
                    name=connector_root.name,
                    status=_read_text(status_path, "unknown"),
                    modes=tuple(line for line in modes_text.splitlines() if line),
                )
            )

    framebuffers = []
    graphics_root = sys_root / "class" / "graphics"
    if graphics_root.is_dir():
        for sys_device in sorted(graphics_root.glob("fb[0-9]*")):
            device_path = dev_root / sys_device.name
            virtual_size = _read_text(sys_device / "virtual_size")
            try:
                width_text, virtual_height_text = virtual_size.split(",", 1)
                width = int(width_text)
                virtual_height = int(virtual_height_text)
            except ValueError:
                width = 0
                virtual_height = 0
            height = 0
            for connector in drm_connectors:
                if connector.connected and connector.modes:
                    try:
                        mode_width, mode_height = connector.modes[0].split("x", 1)
                        if int(mode_width) == width:
                            height = int(mode_height)
                            break
                    except ValueError:
                        continue
            if height == 0:
                height = virtual_height
            framebuffers.append(
                FramebufferDevice(
                    path=str(device_path),
                    name=_read_text(sys_device / "name", sys_device.name),
                    width=width,
                    height=height,
                    virtual_height=virtual_height,
                    bits_per_pixel=_read_int(sys_device / "bits_per_pixel"),
                    stride_bytes=_read_int(sys_device / "stride"),
                    writable=device_path.exists() and _is_writable(device_path),
                )
            )

    dma_candidates = (
        _device_paths(dev_root, "uio*")
        + _device_paths(dev_root, "udmabuf*")
        + _device_paths(dev_root, "posepriv*")
        + _device_paths(dev_root, "dma_proxy*")
        + ((str(dev_root / "dma_heap"),) if (dev_root / "dma_heap").exists() else ())
    )
    return LinuxPlatformInventory(
        video_devices=video_devices,
        stable_video_paths=stable_video_paths,
        drm_nodes=drm_nodes,
        drm_connectors=tuple(drm_connectors),
        framebuffers=tuple(framebuffers),
        dma_candidates=dma_candidates,
    )


def _is_writable(path: Path) -> bool:
    try:
        with path.open("r+b"):
            return True
    except OSError:
        return False
