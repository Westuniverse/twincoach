# SPDX-License-Identifier: MIT
"""Opt-in crop-assisted manual corners on one immutable frozen source.

Left preview clicks focus the crop; right crop clicks commit semantic corners.
No capture, window, automatic correspondence or alternate PnP is introduced.
"""

from __future__ import annotations

from .capture import CapturedFrame
from .manual_rectangle import FrozenRectangleSelection
from .relative_object import MARKER_BGR

PRECISION_DISPLAY_RECT = (408, 64, 376, 472)
MAXIMUM_CROP_SIZE = (188, 236)
_INACTIVE_STATES = ("cancelled", "ready", "rejected_corner_order")


class PrecisionRectangleSelection(FrozenRectangleSelection):
    """Same ownership/pose contract, two-stage clicks with optional view stamp."""

    def __init__(self, frame: CapturedFrame, *, focal_width_ratio: float,
                 marker_height_ratio: float, model_depth_ratio: float):
        super().__init__(frame, focal_width_ratio=focal_width_ratio,
                         marker_height_ratio=marker_height_ratio,
                         model_depth_ratio=model_depth_ratio)
        self._precision_crop: tuple[int, int, int, int] | None = None

    @property
    def precision_crop(self) -> tuple[int, int, int, int] | None:
        """Immutable source-pixel rectangle, not a new frame or a GUI proof."""
        return self._precision_crop

    @staticmethod
    def _in_precision_panel(x: int, y: int) -> bool:
        left, top, width, height = PRECISION_DISPLAY_RECT
        return left <= x < left + width and top <= y < top + height

    def source_pixel_from_display(self, x: int, y: int) -> tuple[float, float] | None:
        # Parent validates strict integer clicks and the unchanged left mapping.
        pixel = super().source_pixel_from_display(x, y)
        if pixel is not None:
            return pixel
        crop = self.precision_crop
        if crop is None or not self._in_precision_panel(x, y):
            return None
        left, top, width, height = PRECISION_DISPLAY_RECT
        source_left, source_top, source_width, source_height = crop
        sx = source_left + (x - left + .5) * source_width / width - .5
        sy = source_top + (y - top + .5) * source_height / height - .5
        if not 0 <= sx < self._frame.width or not 0 <= sy < self._frame.height:
            return None  # Never clamp an operator correspondence.
        return sx, sy

    def click(self, x: int, y: int) -> str:
        """Immediate coordinates; GUI callers must use click_on_submitted_view."""
        if self.state in _INACTIVE_STATES:
            return "selection_not_active"
        pixel = super().source_pixel_from_display(x, y)
        if pixel is not None:
            width = min(MAXIMUM_CROP_SIZE[0], self._frame.width)
            height = min(MAXIMUM_CROP_SIZE[1], self._frame.height)
            left = max(0, min(self._frame.width - width, round(pixel[0]) - width // 2))
            top = max(0, min(self._frame.height - height, round(pixel[1]) - height // 2))
            self._precision_crop = (left, top, width, height)
            return "precision_focus_updated"
        if self._in_precision_panel(x, y) and self.precision_crop is None:
            return "precision_focus_required"
        status = super().click(x, y)
        if status in ("corner_recorded", "four_corners_ready", "rejected_corner_order"):
            self._precision_crop = None  # Every next semantic corner needs a new focus.
        return status

    def click_on_submitted_view(self, x: int, y: int,
                                submitted_crop: tuple[int, int, int, int] | None) -> str:
        """Reject right clicks whose last submitted mapping changed in the queue.

        Equal crops on the same frozen source have identical coordinate meaning.
        This stamp describes imshow submission, not physical presentation.
        """
        if self.state not in _INACTIVE_STATES and type(x) is int and type(y) is int:
            if (self._in_precision_panel(x, y) and self.precision_crop is not None
                    and submitted_crop != self.precision_crop):
                return "precision_view_not_current"
        return self.click(x, y)

    def reset(self) -> None:
        super().reset()
        self._precision_crop = None

    def _preview_point(self, sx: float, sy: float) -> tuple[int, int]:
        left, top, width, height = self.image_rect
        return (round((sx + .5) * width / self._frame.width - .5 + left),
                round((sy + .5) * height / self._frame.height - .5 + top))

    def _draw_precision_overlay(self, canvas, *, cv2_module):
        """Compose only the focus box/zoom into the caller's initialized canvas.

        No full-canvas allocation or source-preview resize. Both static and
        dual-live callers draw preview markers after this shared overlay.
        """
        crop = self.precision_crop
        if crop is None:
            return
        cv2 = cv2_module
        left, top, width, height = self.image_rect
        preview = canvas[top:top + height, left:left + width]
        source_left, source_top, source_width, source_height = crop
        x0, y0 = self._preview_point(source_left, source_top)
        x1, y1 = self._preview_point(source_left + source_width - 1,
                                   source_top + source_height - 1)
        cv2.rectangle(preview, (x0 - left, y0 - top), (x1 - left, y1 - top),
                      (0, 210, 255), 1, cv2.LINE_8)
        zoom_left, zoom_top, zoom_width, zoom_height = PRECISION_DISPLAY_RECT
        zoom = canvas[zoom_top:zoom_top + zoom_height, zoom_left:zoom_left + zoom_width]
        region = self._frame.payload[
            source_top:source_top + source_height, source_left:source_left + source_width]
        if source_width == 1 and source_height == 1:
            zoom[:] = region[0, 0]
        else:
            zoom[:] = cv2.resize(region, (zoom_width, zoom_height), interpolation=cv2.INTER_LINEAR)
        for point, color in zip(self.normalized_points(), MARKER_BGR, strict=False):
            sx, sy = point[0] * self._frame.width, point[1] * self._frame.height
            if source_left <= sx < source_left + source_width \
                    and source_top <= sy < source_top + source_height:
                zx = round((sx - source_left + .5) * zoom_width / source_width - .5)
                zy = round((sy - source_top + .5) * zoom_height / source_height - .5)
                cv2.circle(zoom, (zx, zy), 4, color, 1, cv2.LINE_8)

    def selection_canvas(self):
        """Original frozen preview plus actual bounded crop, wholly initialized."""
        import cv2
        import numpy as np
        canvas = np.full((600, 800, 3), (18, 26, 40), dtype=np.uint8)
        left, top, width, height = self.image_rect
        preview = canvas[top:top + height, left:left + width]
        if self._frame.width == 1 and self._frame.height == 1:
            # Target OpenCV 4.5.4 rejects this legal raster. Linear resize of a
            # single pixel is exactly a constant fill, not an error fallback.
            preview[:] = self._frame.payload[0, 0]
        else:
            preview[:] = cv2.resize(self._frame.payload, (width, height))
        self._draw_precision_overlay(canvas, cv2_module=cv2)
        for point, color in zip(self.normalized_points(), MARKER_BGR, strict=False):
            sx, sy = point[0] * self._frame.width, point[1] * self._frame.height
            x, y = self._preview_point(sx, sy)
            cv2.circle(preview, (x - left, y - top), 4, color, 1, cv2.LINE_8)
        for text, origin, scale, color in (
            ("FROZEN SOURCE / FOCUS CLICK", (16, 32), .5, (230, 230, 230)),
            ("ZOOM / CONFIRM CORNER", (408, 32), .5, (230, 230, 230)),
            (self.state, (16, 566), .55, (0, 210, 255)),
            ("LEFT FOCUS / RIGHT TL TR BR BL / R RESET / Q EXIT", (16, 588), .4, (0, 210, 255)),
        ):
            cv2.putText(canvas, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1)
        return canvas
