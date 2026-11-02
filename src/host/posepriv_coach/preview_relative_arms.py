"""Opt-in arm smoothing after original visibility/continuity decisions."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from .avatar_skinning import MatrixWrite, compose_affine_q1_14
from .display_hands import palm_down_x_axis, continuous_palm_down_x_axis
from .joint_filter import AdaptiveJointFilter, JointFilterConfig
from .skeleton_pose import (build_bone_segments, bone_affine_matrix_q1_14,
                            stable_bone_local_x_axis)


@dataclass(frozen=True, slots=True)
class RelativeArmPreview:
    render_plan: object
    applied_sides: tuple[str, ...]
    rejected_sides: tuple[str, ...]
    depth_reset_sides: tuple[str, ...]


class PreviewRelativeArms:
    """Filter current complete arms, retaining original masks and histories.

    The source plan and its original continuity history remain unchanged.
    Missing/held-only arms are never reconstructed. This adds display lag;
    it neither creates observations nor changes the original scoring inputs.
    Optional palm roll uses its own filtered-forearm history, committed only
    after the complete arm matrices succeed.
    """

    def __init__(self, bone_links, topology_indices, bind_lengths, inverse_bind,
                 *, affine_composer=None, palm_roll_stability=False):
        self.links = tuple(bone_links)
        self.indices = tuple(topology_indices)
        self.bind_lengths = tuple(bind_lengths)
        self.inverse_bind = tuple(inverse_bind)
        if (not 1 <= len(self.indices) <= 16
                or len(self.bind_lengths) != len(self.indices)
                or len(self.inverse_bind) != len(self.indices)
                or any(type(i) is not int or not 0 <= i < len(self.links) for i in self.indices)):
            raise ValueError('relative arms require matching bone topology/bind metadata')
        names = [self.links[i].name for i in self.indices]
        self.slots = {}
        for side in ('left', 'right'):
            required = [side+'_'+part for part in ('upper_arm', 'lower_arm', 'hand')]
            if not all(n in names for n in required):
                raise ValueError('relative arms require separate upper/lower/hand bones')
            self.slots[side] = tuple(names.index(n) for n in required)
        self.arm_links = {side: tuple(self.links[self.indices[slot]] for slot in slots)
                          for side, slots in self.slots.items()}
        self.compose = compose_affine_q1_14 if affine_composer is None else affine_composer
        if not callable(self.compose):
            raise TypeError('affine composer must be callable')
        self.filters = {side: AdaptiveJointFilter(JointFilterConfig(
            minimum_cutoff_hz=6., motion_beta=1.6, reset_gap_ms=100.))
            for side in ('left', 'right')}
        self._timestamp_ns = None
        self.palm_roll_stability = palm_roll_stability
        self._hand_history = {}

    def reset(self):
        self._hand_history.clear()
        self._timestamp_ns = None
        for filter in self.filters.values():
            filter.reset()

    def _filter_joints(self, joints, timestamp_ns):
        result = dict(joints)
        depth_resets = []
        for side, filter in self.filters.items():
            root = side+'_shoulder'
            children = [side+'_elbow', side+'_wrist']
            if not all(n in joints and joints[n].valid for n in [root]+children):
                filter.reset()
                continue
            parent = joints[root].position
            local = {n: replace(joints[n], position=tuple(a-b for a, b in
                zip(joints[n].position, parent))) for n in children}
            current = filter.update(local, timestamp_ns=timestamp_ns)
            wrist = side+'_wrist'
            # The temporal tail must not reverse the current wrist depth.
            if local[wrist].position[2]*current[wrist].position[2] < 0:
                filter.reset()
                current = filter.update(local, timestamp_ns=timestamp_ns)
                depth_resets.append(side)
            for n, joint in current.items():
                result[n] = replace(joint, position=tuple(a+b for a, b in
                    zip(joint.position, parent)))
        return result, tuple(depth_resets)

    def _palm_axis(self, side, segment, source_axis, timestamp_ns):
        if not self.palm_roll_stability:
            return palm_down_x_axis(segment, previous_axis=source_axis, max_roll_radians=0.)
        previous = self._hand_history.get(side)
        direction = tuple((b-a)/segment.length for a,b in zip(segment.start, segment.end))
        if previous is None or timestamp_ns-previous[0] > 100_000_000:
            return continuous_palm_down_x_axis(segment)
        if timestamp_ns == previous[0] and direction == previous[2]:
            return previous[1]
        return continuous_palm_down_x_axis(segment, previous_axis=previous[1],
            previous_direction=previous[2], max_roll_radians=12.*(timestamp_ns-previous[0])/1e9)

    def apply(self, observed, render, *, axes):
        if observed.pair is not render.pair:
            raise ValueError('relative arms require the same source pose pair')
        timestamp_ns = (observed.pair.front.timestamp_ns+observed.pair.side.timestamp_ns)//2
        if self._timestamp_ns is not None and timestamp_ns < self._timestamp_ns:
            raise ValueError('non-monotonic relative arm timestamp')
        if len(axes) != 16:
            raise ValueError('relative arms require sixteen source bone axes')
        self._timestamp_ns = timestamp_ns
        original = observed.joints
        current_input = dict(original)
        eligible = {}
        for side, slots in self.slots.items():
            eligible[side] = slots if all(observed.matrix_valid[i] and
                render.matrix_valid[i] for i in slots) else ()
            if not eligible[side]:
                self._hand_history.pop(side, None)
                for part in ('elbow', 'wrist'):
                    current_input.pop(side+'_'+part, None)
        candidate, depth_resets = self._filter_joints(current_input, timestamp_ns)
        joints = dict(render.joints)
        writes = list(render.matrix_writes)
        applied, rejected = [], []
        for side, slots in eligible.items():
            if not slots:
                continue
            names = [side+'_'+part for part in ('shoulder', 'elbow', 'wrist')]
            if not all(n in candidate and candidate[n].valid for n in names):
                self._hand_history.pop(side, None)
                continue
            local = dict(joints)
            for n in names[1:]:
                local[n] = candidate[n]
            hand_segment = observed.segments[self.indices[slots[2]]]
            hand_name = hand_segment.child
            direction = tuple(a-b for a, b in zip(local[names[2]].position,
                                                   local[names[1]].position))
            length = math.sqrt(sum(v*v for v in direction))
            if length <= 1.e-6:
                self._hand_history.pop(side, None)
                self.filters[side].reset()
                rejected.append(side)
                continue
            local[hand_name] = replace(original[hand_name], position=tuple(
                a+b*hand_segment.length/length for a, b in
                zip(local[names[2]].position, direction)))
            segments = build_bone_segments(local, self.arm_links[side])
            updates = []
            try:
                for index, slot in enumerate(slots):
                    segment = segments[index]
                    axis = (self._palm_axis(side, segment, axes[slot], timestamp_ns)
                        if slot == slots[2] else stable_bone_local_x_axis(segment, axes[slot]))
                    if slot == slots[2]:
                        hand_axis = axis
                    matrix = bone_affine_matrix_q1_14(segment,
                        local_x_reference=axis,
                        local_bone_length_q1_14=self.bind_lengths[slot])
                    if matrix is None:
                        raise ValueError('relative arm is degenerate')
                    matrix = self.compose(matrix, self.inverse_bind[slot])
                    updates.append(MatrixWrite(slot, matrix))
            except ValueError:
                # Reject the whole arm correction, never a subset of its mesh.
                self._hand_history.pop(side, None)
                self.filters[side].reset()
                rejected.append(side)
                continue
            if self.palm_roll_stability:
                hand_segment = segments[2]
                self._hand_history[side] = (timestamp_ns, hand_axis, tuple(
                    (b-a)/hand_segment.length for a,b in zip(hand_segment.start, hand_segment.end)))
            for update in updates:
                writes[update.index] = update
            joints = local
            applied.append(side)
        plan = (replace(render, joints=joints,
            segments=build_bone_segments(joints, self.links), matrix_writes=tuple(writes))
            if applied else render)
        return RelativeArmPreview(plan, tuple(applied), tuple(rejected), depth_resets)
