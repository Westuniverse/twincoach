# SPDX-License-Identifier: MIT
"""Single frozen frame, manual plane correspondences, assumed relative 3D."""

from __future__ import annotations

from dataclasses import replace

from .capture import CapturedFrame
from .relative_object import (
    AssumedObjectCamera, RectangleObjectPrior, ColoredMarkerFrame, MARKER_BGR,
    RelativeObjectFrame, estimate_relative_object, render_relative_object,
    _validate_bgr_frame,
)

CORNER_ORDER = ("TL", "TR", "BR", "BL")


class FrozenRectangleSelection:
    """Bounded operator selection; TL/TR/BR/BL are object-local semantic IDs.

    All display and solve calls use the owned immutable snapshot. Reset never
    captures or substitutes a new frame. There is no camera or GUI access here.
    """

    def __init__(self, frame: CapturedFrame, *, focal_width_ratio: float,
                 marker_height_ratio: float, model_depth_ratio: float):
        if any(isinstance(value, bool) for value in
               (focal_width_ratio, marker_height_ratio, model_depth_ratio)):
            raise ValueError("manual geometric assumptions must be numeric, not bool")
        import numpy as np
        self.camera = AssumedObjectCamera(frame.width, frame.height, focal_width_ratio)
        self.prior = RectangleObjectPrior(marker_height_ratio, model_depth_ratio)
        ColoredMarkerFrame(frame.view, frame.frame_id, frame.timestamp_ns,
                           frame.width, frame.height, (None,) * 4)
        image = _validate_bgr_frame(frame)
        self._pixel_bytes = image.tobytes(order="C")
        frozen = np.frombuffer(self._pixel_bytes, dtype=np.uint8).reshape(image.shape)
        self._frame = replace(frame, payload=frozen)
        scale = min(376 / frame.width, 472 / frame.height)
        width, height = max(1, round(frame.width * scale)), max(1, round(frame.height * scale))
        self._image_rect = (16 + (376 - width) // 2, 64 + (472 - height) // 2, width, height)
        self._points: list[tuple[float, float]] = []
        self._clicks: list[tuple[int, int]] = []
        self.state = "select_TL"

    @property
    def image_rect(self) -> tuple[int, int, int, int]:
        return self._image_rect

    @property
    def reference_frame(self) -> CapturedFrame:
        """Readonly pixels with isolated metadata, never the capture buffer.

        Rebuild the ndarray root from immutable bytes without a pixel copy.
        Exposing the internal ndarray (even a view) would let a caller alter
        its shape/strides through a base chain and corrupt selection rendering.
        """
        import numpy as np
        payload = np.frombuffer(self._pixel_bytes, dtype=np.uint8).reshape(
            self._frame.height, self._frame.width, 3)
        return replace(self._frame, payload=payload)

    def source_identity(self):
        frame = self._frame
        return (frame.view, frame.frame_id, frame.timestamp_ns, frame.width, frame.height)

    def normalized_points(self) -> tuple[tuple[float, float], ...]:
        return tuple(self._points)

    def source_pixel_from_display(self, x: int, y: int) -> tuple[float, float] | None:
        if type(x) is not int or type(y) is not int:
            raise ValueError("click positions must be integer display pixels")
        left, top, width, height = self.image_rect
        if not left <= x < left + width or not top <= y < top + height:
            return None
        sx = (x - left + .5) * self._frame.width / width - .5
        sy = (y - top + .5) * self._frame.height / height - .5
        if not 0 <= sx < self._frame.width or not 0 <= sy < self._frame.height:
            return None
        return sx, sy

    def click(self, x: int, y: int) -> str:
        if self.state in ("cancelled", "ready", "rejected_corner_order"):
            return "selection_not_active"
        pixel = self.source_pixel_from_display(x, y)
        if pixel is None:
            return "outside_source_image"
        point = (pixel[0] / self._frame.width, pixel[1] / self._frame.height)
        if point in self._points:
            return "duplicate_corner"
        self._points.append(point)
        self._clicks.append((x, y))
        if len(self._points) == 4:
            edges = [(self._points[(i + 1) % 4][0] - self._points[i][0],
                      self._points[(i + 1) % 4][1] - self._points[i][1]) for i in range(4)]
            if any(edges[i][0] * edges[(i + 1) % 4][1]
                   - edges[i][1] * edges[(i + 1) % 4][0] <= 0 for i in range(4)):
                self.state = "rejected_corner_order"
                return self.state
            self.state = "ready"
            return "four_corners_ready"
        self.state = "select_" + CORNER_ORDER[len(self._points)]
        return "corner_recorded"

    def reset(self) -> None:
        self._points.clear()
        self._clicks.clear()
        self.state = "select_TL"

    def cancel(self) -> None:
        self.reset()
        self.state = "cancelled"

    def observation(self) -> ColoredMarkerFrame:
        if self.state != "ready":
            raise ValueError("four valid ordered corners on the frozen frame are required")
        frame = self._frame
        return ColoredMarkerFrame(frame.view, frame.frame_id, frame.timestamp_ns,
                                  frame.width, frame.height, tuple(self._points))

    def solve(self) -> RelativeObjectFrame:
        return estimate_relative_object(self.observation(), self.camera, self.prior)

    def selection_canvas(self):
        import cv2
        import numpy as np
        canvas = np.full((600, 800, 3), (18, 26, 40), dtype=np.uint8)
        left, top, width, height = self.image_rect
        canvas[top:top + height, left:left + width] = cv2.resize(self._frame.payload, (width, height))
        roi = canvas[top:top + height, left:left + width]
        for (x, y), color in zip(self._clicks, MARKER_BGR):
            cv2.circle(roi, (x - left, y - top), 4, color, 1, cv2.LINE_8)
        cv2.putText(canvas, "FROZEN SOURCE / CLICK TL TR BR BL", (16, 32),
                    cv2.FONT_HERSHEY_SIMPLEX, .5, (230, 230, 230), 1)
        cv2.putText(canvas, self.state, (16, 566), cv2.FONT_HERSHEY_SIMPLEX, .55, (0, 210, 255), 1)
        cv2.putText(canvas, "MANUAL STATIC / R RESELECT / Q FINISH / ESC CANCEL", (16, 588),
                    cv2.FONT_HERSHEY_SIMPLEX, .4, (0, 210, 255), 1)
        return canvas

    def model_canvas(self, result: RelativeObjectFrame | None = None):
        import cv2
        if self.state != "ready":
            raise ValueError("no complete current manual selection")
        if result is None:
            result = self.solve()
        if (result.observation.points != tuple(self._points)
                or result.camera != self.camera or result.prior != self.prior):
            raise ValueError("cached result does not match the current manual selection")
        canvas = render_relative_object(self._frame, result)
        canvas[:48] = (18, 26, 40)
        canvas[576:] = (18, 26, 40)
        cv2.putText(canvas, "FROZEN SOURCE / MANUAL CORNERS", (16, 32),
                    cv2.FONT_HERSHEY_SIMPLEX, .5, (230, 230, 230), 1)
        cv2.putText(canvas, "RELATIVE MODEL / ASSUMED CAMERA", (408, 32),
                    cv2.FONT_HERSHEY_SIMPLEX, .45, (230, 230, 230), 1)
        cv2.putText(canvas, "STATIC MANUAL / R RESELECT / Q FINISH / ESC CANCEL", (16, 588),
                    cv2.FONT_HERSHEY_SIMPLEX, .4, (0, 210, 255), 1)
        return canvas
