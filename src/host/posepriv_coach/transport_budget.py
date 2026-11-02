"""视频传输预算的无依赖计算，避免把采集前后的压缩混为一谈。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping


_BYTES_PER_PIXEL: Mapping[str, float] = {
    "YUYV": 2.0,
    "YUY2": 2.0,
    "NV12": 1.5,
    "I420": 1.5,
    "RGB": 3.0,
    "BGR": 3.0,
    "RGB24": 3.0,
    "BGR24": 3.0,
}


@dataclass(frozen=True, slots=True)
class VideoTransportEstimate:
    """一组相机模式的有效载荷估算。

    ``payload_mbps`` 对 MJPG 使用实测平均帧字节数，对原始格式使用像素格式的
    固定字节数。它只描述有效数据，不冒充 USB UVC 等时端点的预约容量；最终仍需
    通过 ``lsusb -t`` 和实际 ``STREAMON`` 验收。
    """

    width: int
    height: int
    fps: float
    cameras: int
    pixel_format: str
    raw_payload_mbps: float
    payload_mbps: float | None
    average_frame_bytes: float | None

    @property
    def per_camera_payload_mbps(self) -> float | None:
        if self.payload_mbps is None:
            return None
        return self.payload_mbps / self.cameras

    @property
    def compression_ratio(self) -> float | None:
        if self.payload_mbps is None or self.payload_mbps <= 0.0:
            return None
        return self.raw_payload_mbps / self.payload_mbps

    @property
    def decode_required_on_host(self) -> bool:
        return self.pixel_format == "MJPG"

    def to_record(self) -> dict[str, int | float | str | None | bool]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "cameras": self.cameras,
            "pixel_format": self.pixel_format,
            "raw_payload_mbps": self.raw_payload_mbps,
            "payload_mbps": self.payload_mbps,
            "per_camera_payload_mbps": self.per_camera_payload_mbps,
            "average_frame_bytes": self.average_frame_bytes,
            "compression_ratio": self.compression_ratio,
            "decode_required_on_host": self.decode_required_on_host,
            "upstream_compression": self.pixel_format == "MJPG",
        }


def estimate_video_transport(
    *,
    width: int,
    height: int,
    fps: float,
    cameras: int = 2,
    pixel_format: str,
    average_frame_bytes: float | None = None,
) -> VideoTransportEstimate:
    """估算双摄有效载荷，支持 YUYV/NV12/RGB 和带实测大小的 MJPG。"""

    if width < 1 or height < 1:
        raise ValueError("视频宽高必须为正数")
    if not math.isfinite(fps) or fps <= 0.0:
        raise ValueError("视频 FPS 必须为有限正数")
    if cameras < 1:
        raise ValueError("摄像头数量必须为正整数")
    normalized = pixel_format.strip().upper()
    if not normalized:
        raise ValueError("像素格式不能为空")
    raw_bytes_per_frame = width * height * fps * _raw_bytes_per_pixel(normalized)
    raw_payload_mbps = raw_bytes_per_frame * 8.0 * cameras / 1_000_000.0
    if normalized == "MJPG":
        if average_frame_bytes is None:
            payload_mbps = None
        else:
            if not math.isfinite(average_frame_bytes) or average_frame_bytes <= 0.0:
                raise ValueError("MJPG 平均帧字节数必须为有限正数")
            payload_mbps = average_frame_bytes * fps * 8.0 * cameras / 1_000_000.0
    else:
        if average_frame_bytes is not None:
            raise ValueError("只有 MJPG 可以提供平均帧字节数")
        payload_mbps = raw_payload_mbps
    return VideoTransportEstimate(
        width=width,
        height=height,
        fps=fps,
        cameras=cameras,
        pixel_format=normalized,
        raw_payload_mbps=raw_payload_mbps,
        payload_mbps=payload_mbps,
        average_frame_bytes=average_frame_bytes,
    )


def _raw_bytes_per_pixel(pixel_format: str) -> float:
    if pixel_format == "MJPG":
        # 仅用于展示“若相同分辨率走原始 YUYV”的对照，不代表 MJPG 实际大小。
        return 2.0
    try:
        return _BYTES_PER_PIXEL[pixel_format]
    except KeyError as exc:
        supported = ", ".join((*_BYTES_PER_PIXEL, "MJPG"))
        raise ValueError(f"不支持的像素格式 {pixel_format!r}，可选：{supported}") from exc


__all__ = ["VideoTransportEstimate", "estimate_video_transport"]
