"""Bounded generated-UI templates; never cache camera or pose frames.

The synchronous renderer owner owns the two-entry cache. Only the existing
comparison panel style, supported extents and two fixed titles are admitted.
All other draws use the original renderer, including foreign-thread calls.
"""
from __future__ import annotations

from collections import OrderedDict
from threading import get_ident


def make_panel_shell_renderer(baseline):
    """Return an opt-in renderer type without mutating the baseline class."""
    class PanelShellTemplate(baseline):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._shell_owner = get_ident()
            self._shell_templates = OrderedDict()
            self._shell_calls = self._shell_hits = self._shell_builds = self._shell_fallbacks = 0
            self._shell_evictions = self._shell_peak_bytes = 0

        def _draw_panel_shell(self, canvas, rect, title):
            if get_ident() != self._shell_owner:
                return super()._draw_panel_shell(canvas, rect, title)
            self._shell_calls += 1
            numpy = self._numpy
            if (type(canvas) is not numpy.ndarray or canvas.dtype != numpy.uint8
                    or canvas.ndim != 3 or canvas.shape[2] != 3 or not canvas.flags.writeable
                    or canvas.strides[0] <= 0 or canvas.strides[1:] != (3, 1)
                    or type(rect) is not tuple or len(rect) != 4
                    or any(type(value) is not int for value in rect)
                    or type(title) is not str or title not in ("LIVE PERSON", "DIGITAL HUMAN", "CAMERA VIEWS", "3D AVATAR", "2D POSE AVATAR", "VIDEO-DRIVEN AVATAR")):
                self._shell_fallbacks += 1
                return super()._draw_panel_shell(canvas, rect, title)
            x, y, width, height = rect
            if (x < 0 or y < 0 or width < 300 or height < 44
                    or x + width >= canvas.shape[1] or y + height >= canvas.shape[0]
                    or (width + 1) * (height + 1) * 3 > 1024 * 1024):
                self._shell_fallbacks += 1
                return super()._draw_panel_shell(canvas, rect, title)
            key = (width, height, title)
            template = self._shell_templates.get(key)
            if template is None:
                template = numpy.zeros((height + 1, width + 1, 3), dtype=numpy.uint8)
                super()._draw_panel_shell(template, (0, 0, width, height), title)
                template.flags.writeable = False
                if len(self._shell_templates) == 2:
                    self._shell_templates.popitem(last=False)
                    self._shell_evictions += 1
                self._shell_templates[key] = template
                self._shell_builds += 1
                self._shell_peak_bytes = max(self._shell_peak_bytes,
                    sum(value.nbytes for value in self._shell_templates.values()))
            else:
                self._shell_templates.move_to_end(key)
                self._shell_hits += 1
            canvas[y:y + height + 1, x:x + width + 1] = template

        def shell_template_stats(self):
            return {"scope": "generated_UI_only_NOT_camera_pose_PL",
                "calls": self._shell_calls, "hits": self._shell_hits, "builds": self._shell_builds,
                "fallbacks": self._shell_fallbacks, "evictions": self._shell_evictions,
                "templates": len(self._shell_templates),
                "bytes": sum(value.nbytes for value in self._shell_templates.values()),
                "peak_bytes": self._shell_peak_bytes, "limit_bytes": 2 * 1024 * 1024,
                "camera_or_pose_frame_bytes": 0, "closed": self._closed}

        def close(self):
            self._shell_templates.clear()
            super().close()

    return PanelShellTemplate

