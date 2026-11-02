# SPDX-License-Identifier: GPL-2.0-only
"""Short display recovery after predicted arms regain a current observation."""
import math

import numpy as np

from .preview_world_arm_prediction import PredictiveArmDisplayDriver, _unit


def _follow(previous, current, gain):
    result = []
    for a, b in zip(previous, current):
        cosine = float(np.clip(a @ b, -1, 1))
        angle = math.acos(cosine)
        if angle < 1e-8:
            result.append(b.copy())
            continue
        tangent = b - cosine * a
        if np.linalg.norm(tangent) < 1e-8:
            tangent = np.eye(3)[int(np.argmin(np.abs(a)))]
            tangent = tangent - (tangent @ a) * a
        result.append(_unit(math.cos(angle * gain) * a
                            + math.sin(angle * gain) * _unit(tangent)))
    return np.array(result)


class RecoveryArmDisplayDriver(PredictiveArmDisplayDriver):
    """Blend only a predictive-to-current transition for at most150 source ms.

    Current directions contribute immediately. Body basis, shoulder anchoring,
    binding lengths and reliable history use the original prediction owner.
    Normal reliable motion passes through without a recovery filter. All online
    preparation reuses the already computed body-local directions.
    """

    def __init__(self, asset, torso_length, *, enabled=False, recovery_tau_ms=50.,
                 first_recovery_gain=.35, maximum_recovery_ms=150., **kwargs):
        super().__init__(asset, torso_length, enabled=enabled, **kwargs)
        if (not math.isfinite(recovery_tau_ms) or recovery_tau_ms <= 0
                or not math.isfinite(first_recovery_gain)
                or not 0 < first_recovery_gain <= 1
                or not math.isfinite(maximum_recovery_ms)
                or not 0 < maximum_recovery_ms <= 150):
            raise ValueError("finite recovery parameters and a maximum of150ms required")
        self.recovery_tau_ms = recovery_tau_ms
        self.first_recovery_gain = first_recovery_gain
        self.maximum_recovery_ns = round(maximum_recovery_ms * 1_000_000)
        self.display_history = {}
        self.recovery = {}
        self.predicted = set()

    def _discard_display_arm(self, side):
        self.display_history.pop(side, None)
        self.recovery.pop(side, None)
        self.predicted.discard(side)

    def _display_direction(self, side, direction, ready_ns, provenance):
        mode = provenance["mode"]
        if mode == "unavailable" or direction is None:
            self._discard_display_arm(side)
            return direction, False
        previous = self.display_history.get(side)
        first = False
        reconstruct = provenance["display_reconstructed"]
        if mode == "predictive_body_local":
            self.predicted.add(side)
            self.recovery.pop(side, None)
        elif side in self.predicted and previous is not None:
            self.predicted.remove(side)
            self.recovery[side] = ready_ns
            first = True
        if mode != "predictive_body_local" and side in self.recovery and previous is not None:
            age = ready_ns - self.recovery[side]
            if 0 <= age < self.maximum_recovery_ns:
                dt = (ready_ns - previous[0]) / 1_000_000_000
                gain = -math.expm1(-dt / (self.recovery_tau_ms / 1000))
                if first:
                    gain = min(gain, self.first_recovery_gain)
                direction = _follow(previous[1], direction, gain)
                provenance.update(mode="recovery_body_local", recovery_origin_mode=mode,
                    display_reconstructed=True, recovery_elapsed_ns=age,
                    recovery_gain=gain, recovery_tau_ms=self.recovery_tau_ms,
                    recovery_first_sample=first)
                reconstruct = True
            else:
                self.recovery.pop(side, None)
        self.display_history[side] = (ready_ns, direction.copy())
        return direction, reconstruct
