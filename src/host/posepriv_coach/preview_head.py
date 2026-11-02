"""Optional source-time direction limit for the displayed avatar head."""
from __future__ import annotations

from dataclasses import replace
import math


class PreviewHeadDirection:
    """Limit displayed tilt only; never fill an absent head observation."""

    def __init__(self):
        self._timestamp_ns = None
        self._direction = None
        self._direction_timestamp_ns = None
        self.last_target_error_rad = 0.

    def update(self, neck, nose, *, timestamp_ns):
        previous_time = self._timestamp_ns
        if previous_time is not None and timestamp_ns < previous_time:
            raise ValueError('non-monotonic head preview timestamp')
        self._timestamp_ns = timestamp_ns
        self.last_target_error_rad = 0.
        if neck is None or not neck.valid:
            self._direction = None
            self._direction_timestamp_ns = None
            return nose
        if nose is None or not nose.valid:
            # Keep orientation only. No missing joint is filled, and invalid
            # samples never refresh the last complete observation's timestamp.
            if (self._direction_timestamp_ns is not None and
                    timestamp_ns-self._direction_timestamp_ns>100_000_000):
                self._direction = None
                self._direction_timestamp_ns = None
            return nose
        offset = tuple(b-a for a,b in zip(neck.position,nose.position))
        length = math.sqrt(sum(v*v for v in offset))
        if length <= 1.e-6:
            self._direction = None
            self._direction_timestamp_ns = None
            return nose
        target = tuple(v/length for v in offset)
        previous = self._direction
        previous_time = self._direction_timestamp_ns
        if previous is not None and previous_time is not None and timestamp_ns-previous_time <= 100_000_000:
            dot = max(-1.,min(1.,sum(a*b for a,b in zip(previous,target))))
            delta = math.acos(dot)
            maximum = 4.*(timestamp_ns-previous_time)/1.e9
            if delta > maximum:
                tangent = tuple(b-dot*a for a,b in zip(previous,target))
                norm = math.sqrt(sum(v*v for v in tangent))
                if norm <= 1.e-12:
                    # At an exactly opposite target, choose one deterministic
                    # orthogonal arc; no head orientation truth is inferred.
                    axis = min(range(3),key=lambda i:abs(previous[i]))
                    tangent = tuple(float(i==axis)-previous[axis]*previous[i] for i in range(3))
                    norm = math.sqrt(sum(v*v for v in tangent))
                direction = tuple(a*math.cos(maximum)+b/norm*math.sin(maximum)
                                  for a,b in zip(previous,tangent))
                # Normalize before storing so repeated times cannot accumulate
                # unit-length roundoff or change the angular-rate contract.
                norm = math.sqrt(sum(v*v for v in direction))
                self._direction = tuple(v/norm for v in direction)
                self._direction_timestamp_ns = timestamp_ns
                self.last_target_error_rad = delta-maximum
                return replace(nose,position=tuple(a+b*length for a,b in zip(neck.position,self._direction)))
        self._direction = target
        self._direction_timestamp_ns = timestamp_ns
        return nose
