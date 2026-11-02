"""Preview-only side-depth continuity, independent from score observations."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class PreviewDepthValue:
    value: float
    source: str
    age_ns: int


class PreviewDepthTracker:
    """Use confidence hysteresis and at most 100 ms of actual depth history.

    The caller supplies only currently visible front joints. A retained Z value
    is a presentation aid, never a new keypoint or confidence observation.
    """

    def __init__(self, *, hold_ms=100.):
        if not math.isfinite(hold_ms) or not 0 <= hold_ms <= 100:
            raise ValueError('depth hold must be within [0,100] ms')
        self.hold_ns = round(hold_ms*1_000_000)
        self.reset()

    def reset(self):
        self._timestamp_ns = None
        self._geometry = None
        self._tracked = set()
        self._observed = {}
        self.last_values = {}
        self.source_counts = dict(observed=0, held=0, flat=0)

    @staticmethod
    def _visible(point, threshold):
        return (point is not None and point.confidence >= threshold
                and math.isfinite(point.x) and math.isfinite(point.y)
                and 0 <= point.x <= 1 and 0 <= point.y <= 1)

    def update(self, side, names, *, forward_sign, timestamp_ns):
        if type(timestamp_ns) is not int or timestamp_ns < 0:
            raise ValueError('nonnegative integer preview timestamp required')
        if self._timestamp_ns is not None and timestamp_ns < self._timestamp_ns:
            raise ValueError('non-monotonic preview depth timestamp')
        if forward_sign not in (None, -1., 1.):
            raise ValueError('preview forward sign must be known unit sign or None')
        geometry = (side.source_width, side.source_height, forward_sign)
        if (self._timestamp_ns is not None and timestamp_ns-self._timestamp_ns > 250_000_000
                or self._geometry is not None and self._geometry != geometry):
            self.reset()
        self._timestamp_ns, self._geometry = timestamp_ns, geometry
        names = tuple(names)
        eligible = set(names) | {'left_hip', 'right_hip'}
        tracked = {name for name in eligible if self._visible(side.keypoints.get(name),
                   .35 if name in self._tracked else .5)}
        self._tracked = tracked
        origin_ok = {'left_hip', 'right_hip'} <= tracked and forward_sign is not None
        origin = ((side.keypoints['left_hip'].x+side.keypoints['right_hip'].x)/2
                  if origin_ok else None)
        values = {}
        for name in names:
            if origin_ok and name in tracked:
                depth = ((origin-side.keypoints[name].x)*forward_sign*2*side.source_width
                         / max(side.source_width, side.source_height))
                self._observed[name] = (timestamp_ns, depth)
                value = PreviewDepthValue(depth, 'observed', 0)
            else:
                old = self._observed.get(name)
                age = timestamp_ns-old[0] if old is not None else None
                if forward_sign is not None and age is not None and age <= self.hold_ns:
                    value = PreviewDepthValue(old[1], 'held', age)
                else:
                    self._observed.pop(name, None)
                    value = PreviewDepthValue(0., 'flat', 0)
            values[name] = value
            self.source_counts[value.source] += 1
        # Missing front joints do not acquire positions; their old depth may be
        # reused only while its original source time remains within the limit.
        self._observed = {name: item for name, item in self._observed.items()
                          if timestamp_ns-item[0] <= self.hold_ns}
        self.last_values = values
        return values
