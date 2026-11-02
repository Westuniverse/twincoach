"""Provisional phase-specific angle bands over the existing 2D depth ratio.

No template learning, extrapolation, per-repetition amplitude normalization, or
time warping is performed here. These pure functions are also the offline oracle.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping


ALGORITHM = "depth_angle_time_v1"
PHASES = ("descending", "bottom", "ascending")


def interpolate_depth(a: float, b: float, fraction: float) -> float:
    """Convex interpolation, bounded against floating-point endpoint roundoff.

    Callers validate both actual endpoints first. This does not extend the model
    domain or make an out-of-domain observation eligible.
    """
    if not 0 <= fraction <= 1:
        raise ValueError("interpolation fraction must be within [0, 1]")
    return min(max(a + fraction * (b - a), min(a, b)), max(a, b))


@dataclass(frozen=True, slots=True)
class PhaseAngleBand:
    phase: str
    knots: tuple[tuple[float, float, float], ...]

    def __post_init__(self) -> None:
        if self.phase not in PHASES or len(self.knots) < 2:
            raise ValueError("angle band requires an active phase and at least two knots")
        previous = -math.inf
        for knot in self.knots:
            if len(knot) != 3:
                raise ValueError("each angle band knot must contain depth, lower, upper")
            depth, lower, upper = knot
            if not all(math.isfinite(v) for v in knot):
                raise ValueError("angle band knots must be finite")
            if depth < 0 or depth <= previous or not 0 <= lower <= upper <= 180:
                raise ValueError("angle band depths must increase; angle bounds must be ordered")
            previous = depth

    def bounds(self, depth: float) -> tuple[float, float] | None:
        if not math.isfinite(depth) or not self.knots[0][0] <= depth <= self.knots[-1][0]:
            return None
        for a, b in zip(self.knots, self.knots[1:]):
            if depth <= b[0]:
                fraction = (depth - a[0]) / (b[0] - a[0])
                return (a[1] + fraction * (b[1] - a[1]),
                        a[2] + fraction * (b[2] - a[2]))
        raise AssertionError("in-domain depth must fall in a band segment")

    @property
    def envelope(self) -> tuple[float, float]:
        return min(k[1] for k in self.knots), max(k[2] for k in self.knots)

    def breakpoints(self, depth_a: float, angle_a: float, depth_b: float,
                    angle_b: float, tolerance: float) -> tuple[float, ...]:
        """Fractions where q changes slope along a linear observed interval.

        Splitting at band knots and at L/U and L-delta/U+delta makes the
        trapezoidal integral exact for the declared piecewise-linear model.
        """
        if not math.isfinite(tolerance) or tolerance <= 0:
            raise ValueError("angle tolerance must be finite and positive")
        if not all(math.isfinite(v) for v in (angle_a, angle_b)):
            raise ValueError("angles must be finite")
        if self.bounds(depth_a) is None or self.bounds(depth_b) is None:
            raise ValueError("interval endpoints must both be in the band domain")
        points = {0.0, 1.0}
        if depth_a != depth_b:
            points.update((k[0] - depth_a) / (depth_b - depth_a) for k in self.knots
                          if 0 < (k[0] - depth_a) / (depth_b - depth_a) < 1)
        initial = sorted(points)
        for lo, hi in zip(initial, initial[1:]):
            bounds_lo = self.bounds(interpolate_depth(depth_a, depth_b, lo))
            bounds_hi = self.bounds(interpolate_depth(depth_a, depth_b, hi))
            assert bounds_lo is not None and bounds_hi is not None
            angle_lo = angle_a + lo * (angle_b - angle_a)
            angle_hi = angle_a + hi * (angle_b - angle_a)
            for index, offset in ((0, -tolerance), (0, 0), (1, 0), (1, tolerance)):
                error_lo = angle_lo - bounds_lo[index] - offset
                error_hi = angle_hi - bounds_hi[index] - offset
                if error_lo * error_hi < 0:
                    points.add(lo + (hi - lo) * (-error_lo / (error_hi - error_lo)))
        return tuple(sorted(points))


@dataclass(frozen=True, slots=True)
class DepthAngleModel:
    algorithm: str
    depth_coordinate: str
    domain_policy: str
    phases: tuple[PhaseAngleBand, ...]

    def __post_init__(self) -> None:
        if (self.algorithm != ALGORITHM or self.depth_coordinate != "side_depth_ratio"
                or self.domain_policy != "invalid"):
            raise ValueError("unsupported depth-angle model semantics")
        if tuple(b.phase for b in self.phases) != PHASES:
            raise ValueError("depth-angle model requires the three ordered active phases")

    def for_phase(self, phase: str) -> PhaseAngleBand:
        for band in self.phases:
            if band.phase == phase:
                return band
        raise ValueError("no angle band for inactive phase")

    @classmethod
    def from_record(cls, raw: Mapping) -> DepthAngleModel:
        if not isinstance(raw, Mapping) or set(raw) != {"algorithm", "depth_coordinate", "domain_policy", "phases"}:
            raise ValueError("depth-angle model requires all versioned fields")
        phases = raw["phases"]
        if not isinstance(phases, Mapping) or set(phases) != set(PHASES):
            raise ValueError("depth-angle model requires exactly three active phases")
        try:
            bands = tuple(PhaseAngleBand(p, tuple(tuple(float(v) for v in k) for k in phases[p]))
                          for p in PHASES)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid depth-angle phase knots") from exc
        return cls(str(raw["algorithm"]), str(raw["depth_coordinate"]), str(raw["domain_policy"]), bands)
