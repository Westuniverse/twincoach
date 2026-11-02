"""Opt-in whole-model current rotation filtering around the display pelvis."""
from __future__ import annotations

from dataclasses import dataclass, replace

from .preview_head_orientation import _follow, _rotation
from .preview_rigid_avatar import _matmul
from .avatar_image_source import avatar_image_source


@dataclass(frozen=True, slots=True)
class CurrentBodyRotationPreview:
    preview: object
    reason: str
    target_error_rad: float | None


class PreviewCurrentBodyRotation:
    """Display affine only: no geometry, inferred labels or scoring writes."""

    def __init__(self, spine_slot=0):
        if type(spine_slot) is not int or not 0 <= spine_slot < 16:
            raise ValueError('explicit spine slot required')
        self.spine_slot = spine_slot
        self.reset()

    def reset(self):
        self._timestamp = None
        self._rotation = None

    def apply(self, preview):
        import numpy as np
        front = avatar_image_source(preview.render_plan)
        previous = self._timestamp
        if previous is not None and front.timestamp_ns < previous:
            raise ValueError('non-monotonic current body timestamp')
        if previous is not None and front.timestamp_ns - previous > 250_000_000:
            self.reset()
            previous = None
        if preview.reason not in ('tracked', 'partial'):
            self.reset()
            return CurrentBodyRotationPreview(preview, 'reference_body', None)
        matrix = np.array(preview.render_plan.matrix_writes[self.spine_slot].matrix.values,
                          dtype=float).reshape(3, 4)
        display = np.array(preview.display_transform).reshape(4, 4)
        target = _rotation(_matmul(_rotation(display[:3, :3]), _rotation(matrix[:, :3])))
        if self._rotation is None:
            self._rotation = target
            self._timestamp = front.timestamp_ns
            return CurrentBodyRotationPreview(preview, 'initial_current', 0.)
        if np.array_equal(self._rotation, target):
            self._timestamp = front.timestamp_ns
            return CurrentBodyRotationPreview(preview, 'unchanged_current', 0.)
        elapsed = (front.timestamp_ns - previous) / 1e9
        next_rotation, error = _follow(self._rotation, target, elapsed,
            recovering=False, cutoff_hz=6., maximum_rate=4.)
        canonical = tuple(round(float(v) * 16777216) / 16777216 for v in next_rotation.flat)
        next_rotation = np.array(canonical).reshape(3, 3)
        correction = _matmul(next_rotation, target.T)
        pelvis = np.array(preview.render_plan.joints['pelvis'].position)
        pivot = _matmul(display[:3, :3], pelvis) + display[:3, 3]
        result = display.copy()
        result[:3, :3] = _matmul(correction, display[:3, :3])
        result[:3, 3] = pivot - _matmul(correction, pivot) + _matmul(correction, display[:3, 3])
        rendered = replace(preview, display_transform=tuple(result.flat))
        self._timestamp = front.timestamp_ns
        self._rotation = next_rotation
        return CurrentBodyRotationPreview(rendered, 'filtered_current', error)
