# SPDX-License-Identifier: GPL-2.0-only
"""Opt-in world-arm display directions, independent of raw poses and scoring.

Uses the preview's existing optional NumPy dependency. Physical source objects,
normalized landmarks and model confidence values remain unchanged. Callers keep
the original observations; the returned copy is explicitly inferred geometry.
"""
from collections import deque
import math

import numpy as np

from .world_display_boundaries import filtered_observation

_ARMS = {"left": (11, 13, 15), "right": (12, 14, 16)}
_CORE = (11, 12, 23, 24)


def _unit(vector):
    return vector / np.linalg.norm(vector)


def _point(world, index):
    point = world[index]
    return np.array((point["x"], -point["y"], point["z"]))


def _quality(world, indices):
    return min(min(world[i]["presence"], world[i]["visibility"]) for i in indices)


def _body_basis(world):
    if len(world) != 33 or _quality(world, _CORE) < .5:
        return None
    vertical = (_point(world, 11) + _point(world, 12)
                - _point(world, 23) - _point(world, 24)) / 2
    lateral = _point(world, 11) - _point(world, 12)
    if np.linalg.norm(vertical) < 1e-8:
        return None
    y = _unit(vertical)
    lateral = lateral - (lateral @ y) * y
    if np.linalg.norm(lateral) < 1e-8:
        return None
    x = _unit(lateral)
    return np.column_stack((x, y, np.cross(x, y)))


def _directions(world, indices, basis):
    a, b, c = (_point(world, i) for i in indices)
    if min(np.linalg.norm(b - a), np.linalg.norm(c - b)) < 1e-8:
        return None
    return np.array((basis.T @ _unit(b - a), basis.T @ _unit(c - b)))


def _extrapolate(previous, latest, interval_ns, ahead_ns, maximum_rate, damping_tau_ms):
    """Integrate decaying angular velocity; source time differences stay integer."""
    result = []
    velocities = []
    ahead = ahead_ns / 1_000_000_000
    if damping_tau_ms:
        tau = damping_tau_ms / 1000
        ahead = tau * (-math.expm1(-ahead / tau))
    for a, b in zip(previous, latest):
        cosine = float(np.clip(a @ b, -1, 1))
        tangent = b * cosine - a
        magnitude = float(np.linalg.norm(tangent))
        if magnitude < 1e-8:
            result.append(b.copy())
            velocities.append(0.)
            continue
        rate = min(math.acos(cosine) * 1_000_000_000 / interval_ns, maximum_rate)
        step = rate * ahead
        result.append(_unit(math.cos(step) * b + math.sin(step) * tangent / magnitude))
        velocities.append(rate)
    return np.array(result), velocities


class PredictiveArmDisplayDriver:
    """Current reliable directions or a labelled short-term display prediction.

    One instance belongs to one fixed primary camera/session. Same-name reliable
    arms from the other current view are transformed through each body's local
    basis. With neither chain reliable, at most150ms of prediction uses the last
    two reliable directions. Prediction never becomes reliable history.

    Endpoints use the current shoulder/body basis and fixed binding lengths.
    Unavailable chains remain unchanged for the caller's existing policy. This
    class provides inferred display geometry, never new pose/score observations.
    """

    def __init__(self, asset, torso_length, *, enabled=False, maximum_rate=8.,
                 damping_tau_ms=50.):
        # Configuration is checked once; input observations already have their
        # external immutable/finite contract. No new per-frame validation pass.
        if (type(enabled) is not bool or not math.isfinite(torso_length)
                or torso_length <= 0 or not math.isfinite(maximum_rate)
                or maximum_rate <= 0 or not math.isfinite(damping_tau_ms)
                or damping_tau_ms < 0):
            raise ValueError("arm prediction requires boolean enable and finite display parameters")
        self.enabled = enabled
        self.maximum_rate = maximum_rate
        self.damping_tau_ms = damping_tau_ms
        self.torso_length = torso_length
        self.lengths = {
            side: tuple(asset.bone_bind_lengths_q1_14[
                asset.bone_names.index(side + "_" + part)] / 16384
                for part in ("upper_arm", "lower_arm"))
            for side in _ARMS
        }
        if any(not math.isfinite(length) or length <= 0
               for chain in self.lengths.values() for length in chain):
            raise ValueError("arm prediction requires positive finite binding lengths")
        self.history = {side: deque(maxlen=2) for side in _ARMS}

    def _discard_display_arm(self, side):
        """Optional display state; reliable model history belongs to this owner."""
        return None

    def _display_direction(self, side, direction, ready_ns, provenance):
        return direction, provenance["display_reconstructed"]

    def apply(self, primary, other, *, ready_ns):
        if not self.enabled:
            return primary, {"enabled": False}
        world = primary.world_landmarks
        basis = _body_basis(world)
        alternate_basis = _body_basis(other.world_landmarks) if other is not None else None
        metadata = {
            "enabled": True,
            "physical_source": {"view": primary.source.view,
                                "frame_id": primary.source.frame_id,
                                "timestamp_ns": primary.source.timestamp_ns},
            "ready_ns": ready_ns,
            "arms": {},
        }
        changed = None
        for side, indices in _ARMS.items():
            if basis is None:
                metadata["arms"][side] = {
                    "mode": "unavailable", "inferred": True, "display_reconstructed": False,
                    "reason": "current_primary_body_basis_unavailable",
                }
                self._discard_display_arm(side)
                continue
            q = _quality(world, indices) if len(world) == 33 else 0.
            current = _directions(world, indices, basis) if basis is not None and q >= .5 else None
            mode, donor = "primary_current", primary.source
            rates = None
            if (current is None and alternate_basis is not None
                    and _quality(other.world_landmarks, indices) >= .5
                    and abs(other.source.timestamp_ns - primary.source.timestamp_ns) <= 50_000_000):
                current = _directions(other.world_landmarks, indices, alternate_basis)
                mode, donor = "other_current_body_local", other.source
            history = self.history[side]
            if current is not None:
                if not history or ready_ns > history[-1]["ready_ns"]:
                    history.append({
                        "ready_ns": ready_ns, "directions": current.copy(),
                        "source": {"view": donor.view, "frame_id": donor.frame_id,
                                   "timestamp_ns": donor.timestamp_ns},
                    })
            elif (basis is not None and len(history) == 2
                  and 0 <= ready_ns - history[-1]["ready_ns"] <= 150_000_000):
                current, rates = _extrapolate(
                    history[0]["directions"], history[1]["directions"],
                    history[1]["ready_ns"] - history[0]["ready_ns"],
                    ready_ns - history[1]["ready_ns"], self.maximum_rate, self.damping_tau_ms,
                )
                mode = "predictive_body_local"
            else:
                mode = "unavailable"
            provenance = {
                "mode": mode, "inferred": True,
                "display_reconstructed": mode in (
                    "other_current_body_local", "predictive_body_local"),
                "primary_chain_q": q,
            }
            if mode == "other_current_body_local":
                provenance["donor_source"] = {
                    "view": donor.view, "frame_id": donor.frame_id,
                    "timestamp_ns": donor.timestamp_ns,
                }
            if mode == "predictive_body_local":
                provenance.update(
                    reliable_sources=[h["source"] for h in history],
                    reliable_age_ns=ready_ns - history[-1]["ready_ns"],
                    angular_rate_rad_s=rates, damping_tau_ms=self.damping_tau_ms,
                )
            metadata["arms"][side] = provenance
            current, reconstruct = self._display_direction(side, current, ready_ns, provenance)
            if not reconstruct or basis is None:
                continue
            if changed is None:
                changed = [dict(point) for point in world]
            torso = np.linalg.norm((_point(world, 11) + _point(world, 12)
                                    - _point(world, 23) - _point(world, 24)) / 2)
            lengths = np.asarray(self.lengths[side]) * torso / self.torso_length
            elbow = _point(world, indices[0]) + lengths[0] * (basis @ current[0])
            wrist = elbow + lengths[1] * (basis @ current[1])
            for index, position in zip(indices[1:], (elbow, wrist)):
                changed[index].update(x=float(position[0]), y=float(-position[1]),
                                      z=float(position[2]))
        result = primary if changed is None else filtered_observation(primary, changed)
        return result, metadata
