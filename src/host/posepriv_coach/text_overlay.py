"""可选的 UTF-8/CJK 画布文字层。

OpenCV 自带 Hershey 字体没有中文字形，因此主渲染路径继续使用 ASCII 回退。提供字体
路径时才加载 Pillow，并且只在画布完成后一次性叠加少量中文提示，不触碰采集、DPU、
蒙皮或 framebuffer 接口。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
import importlib.util


def default_cjk_font() -> Path | None:
    """Use an installed Chinese font without adding a display dependency."""
    if importlib.util.find_spec("PIL") is None:
        return None
    for path in (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "C:/Windows/Fonts/msyh.ttc",
    ):
        if Path(path).is_file():
            return Path(path)
    return None


class TextOverlayError(RuntimeError):
    """CJK 字体或可选 Pillow 后端不可用。"""


@dataclass(frozen=True, slots=True)
class OverlayText:
    text: str
    x: int
    y: int
    font_size: int
    color_bgr: tuple[int, int, int]
    stroke_width: int = 0
    stroke_color_bgr: tuple[int, int, int] | None = None

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("overlay text must not be empty")
        if self.x < 0 or self.y < 0 or self.font_size <= 0:
            raise ValueError("overlay text position and font size must be positive")
        if self.stroke_width < 0:
            raise ValueError("overlay text stroke width must be non-negative")
        _validate_bgr(self.color_bgr, "color_bgr")
        if self.stroke_color_bgr is not None:
            _validate_bgr(self.stroke_color_bgr, "stroke_color_bgr")


class OptionalCjkTextOverlay:
    """在给定字体和 Pillow 存在时把少量 UTF-8 文字叠加到 BGR NumPy 画布。"""

    def __init__(self, font_path: str | Path | None, *, numpy_module: Any) -> None:
        self.font_path = None if font_path is None else Path(font_path)
        self._numpy = numpy_module
        self._fonts: dict[int, Any] = {}
        self._image_module: Any | None = None
        self._draw_module: Any | None = None
        if self.font_path is None:
            return
        if not self.font_path.is_file():
            raise TextOverlayError(f"中文字体文件不存在：{self.font_path}")
        try:
            from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-not-found]
        except ImportError as exc:
            raise TextOverlayError(
                "指定中文字体需要 Pillow；请安装 posepriv-coach-tools[display]"
            ) from exc
        self._image_module = (Image, ImageFont)
        self._draw_module = ImageDraw

    @property
    def enabled(self) -> bool:
        return self.font_path is not None

    def draw_many(self, canvas: Any, texts: Iterable[OverlayText]) -> bool:
        """一次转换画布并绘制多条文字，返回是否实际使用了 CJK 后端。"""

        items = tuple(texts)
        if not items:
            return False
        if not self.enabled:
            return False
        if self._image_module is None or self._draw_module is None:
            raise TextOverlayError("中文字体后端没有正确初始化")
        image_type, font_type = self._image_module
        image = image_type.fromarray(canvas[:, :, ::-1], mode="RGB")
        draw = self._draw_module.Draw(image)
        for item in items:
            font = self._fonts.get(item.font_size)
            if font is None:
                font = font_type.truetype(str(self.font_path), item.font_size)
                self._fonts[item.font_size] = font
            fill = tuple(reversed(item.color_bgr))
            stroke_fill = (
                tuple(reversed(item.stroke_color_bgr))
                if item.stroke_color_bgr is not None
                else None
            )
            draw.text(
                (item.x, item.y),
                item.text,
                font=font,
                fill=fill,
                stroke_width=item.stroke_width,
                stroke_fill=stroke_fill,
            )
        canvas[...] = self._numpy.asarray(image)[:, :, ::-1]
        return True


def _validate_bgr(value: tuple[int, int, int], name: str) -> None:
    if len(value) != 3 or any(
        not isinstance(channel, int) or not 0 <= channel <= 255
        for channel in value
    ):
        raise ValueError(f"{name} must be an RGB8/BGR8 triple")


__all__ = ["OptionalCjkTextOverlay", "OverlayText", "TextOverlayError"]
