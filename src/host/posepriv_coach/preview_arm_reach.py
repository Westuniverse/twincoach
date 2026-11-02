"""Continuous display reach with finite front-image priority (GPL-2.0-only)."""
from __future__ import annotations

import math


def project_arm_reach(offset, reach):
    """Closest reachable point under diag(4, 4, 1), in preview coordinates.

    Front-image XY has more weight than the uncalibrated profile depth, but
    neither is an exact constraint. Unlike reserving all XY reach first, this
    strictly convex projection does not erase depth at the XY reach boundary.
    It is display geometry, not triangulation or a physical arm measurement.
    """
    x, y, z = map(float, offset)
    reach = float(reach)
    if not all(math.isfinite(v) for v in (x, y, z, reach)) or reach <= 0:
        raise ValueError('arm reach requires finite coordinates and positive radius')
    if math.hypot(x,y,z) <= reach:
        return x, y, z
    # KKT: p_xy=4*v_xy/(4+mu), p_z=v_z/(1+mu). The monotonically
    # decreasing radius gives one positive root. Use the feasible upper bound
    # so the result remains inside the ball even at the final binary step.
    # Normalize by the largest component before forming squared terms. This
    # also works for finite coordinates whose unscaled Euclidean norm overflows.
    magnitude=max(abs(x),abs(y),abs(z))
    unit=(x/magnitude,y/magnitude,z/magnitude)
    norm=math.hypot(*unit)
    ux,uy,uz=(v/norm for v in unit)
    relative_reach=(reach/magnitude)/norm
    xy2=ux*ux+uy*uy
    lo = 0.
    hi = 4.
    for _ in range(32):
        mu = (lo + hi)*.5
        a = 4./(4.*relative_reach+mu)
        b = 1./(relative_reach+mu)
        if a*a*xy2 + b*b*uz*uz > 1.:
            lo = mu
        else:
            hi = mu
    a = 4./(4.*relative_reach+hi)
    b = 1./(relative_reach+hi)
    return reach*(a*ux), reach*(a*uy), reach*(b*uz)
