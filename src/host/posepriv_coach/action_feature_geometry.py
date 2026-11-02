"""Observed pixel geometry for the fixed physical-view supervised action schema.

Independent project implementation retained from the private action reference.
No view exchange, inferred coordinates, response calibration or model loading.
"""

import math

import numpy as np

from .multi_action_reference import angle

NAMES = (
    "knee_left",
    "knee_right",
    "elbow_left",
    "elbow_right",
    "shoulder_left",
    "shoulder_right",
    "hip_left",
    "hip_right",
    "trunk",
    "knee_asymmetry",
    "elbow_asymmetry",
    "shoulder_asymmetry",
    "depth_left",
    "depth_right",
)


def metrics(xy, mask):
    xy = np.asarray(xy)
    mask = np.asarray(mask)
    if xy.shape != (17, 2) or mask.shape != (17,) or mask.dtype != np.bool_:
        raise ValueError("Expected COCO17 pixel pairs and boolean observed mask")
    if not np.isfinite(xy[mask]).all():
        raise ValueError("Observed pixel coordinates must be finite")
    out = np.full(14, np.nan)

    def joint(ids):
        return angle(*xy[list(ids)]) if mask[list(ids)].all() else None

    for j, ids in enumerate(
        [
            (11, 13, 15),
            (12, 14, 16),
            (5, 7, 9),
            (6, 8, 10),
            (11, 5, 7),
            (12, 6, 8),
            (5, 11, 13),
            (6, 12, 14),
        ]
    ):
        v = joint(ids)
        if v is not None:
            out[j] = v
    if mask[[5, 6, 11, 12]].all():
        shoulder = xy[[5, 6]].mean(0)
        hip = xy[[11, 12]].mean(0)
        dx, dy = np.abs(shoulder - hip)
        if dx + dy > 1e-9:
            out[8] = math.degrees(math.atan2(dx, dy))
    for dst, a, b in [(9, 0, 1), (10, 2, 3), (11, 4, 5)]:
        if np.isfinite(out[[a, b]]).all():
            out[dst] = abs(out[a] - out[b])
    for dst, ids in [(12, [11, 13, 15]), (13, [12, 14, 16])]:
        if mask[ids].all():
            hip, knee, ankle = xy[ids, 1]
            den = abs(ankle - knee)
            if den > 1e-9:
                out[dst] = abs(ankle - hip) / den
    return out
