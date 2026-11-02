"""Explicit preview-only short gap compensation; raw observations remain separate."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from .avatar_pipeline import AvatarFramePlan
from .avatar_skinning import AffineMatrixQ1_14, MatrixWrite, MATRIX_COUNT


@dataclass(frozen=True, slots=True)
class PreviewPresentationFrame:
    observed_plan: AvatarFramePlan
    render_plan: AvatarFramePlan
    held_slots: tuple[int, ...]
    expired_slots: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _ObservedBone:
    timestamp_ns: int
    matrix: AffineMatrixQ1_14
    parent: tuple[float, float, float]


class PreviewMeshContinuity:
    """Hold only previously observed bones, for a bounded source-time interval.

    The current observed parent anchors translation. Missing parents do not
    acquire synthesized positions or become confidence/score observations.
    """
    def __init__(self, slot_links, *, hold_ms=100., maximum_anchor_shift=.1):
        self.slot_links = tuple(slot_links)
        if not 1 <= len(self.slot_links) <= MATRIX_COUNT:
            raise ValueError('one to sixteen mapped preview bones required')
        if not math.isfinite(hold_ms) or not 0 < hold_ms <= 250:
            raise ValueError('preview hold must be within (0,250] ms')
        if not math.isfinite(maximum_anchor_shift) or maximum_anchor_shift <= 0:
            raise ValueError('finite positive anchor shift limit required')
        self.hold_ns = round(hold_ms * 1_000_000)
        self.maximum_anchor_shift = maximum_anchor_shift
        self.reset()

    def reset(self):
        self._timestamp_ns = None
        self._bones = [None] * len(self.slot_links)

    def apply(self, plan: AvatarFramePlan) -> PreviewPresentationFrame:
        if not plan.pose_preview:
            raise ValueError('continuity compensation requires a display pose_preview')
        timestamp_ns = (plan.pair.front.timestamp_ns + plan.pair.side.timestamp_ns) // 2
        if self._timestamp_ns is not None and timestamp_ns < self._timestamp_ns:
            raise ValueError('non-monotonic preview continuity timestamp')
        if self._timestamp_ns is not None and timestamp_ns - self._timestamp_ns > 250_000_000:
            self.reset()
        self._timestamp_ns = timestamp_ns
        writes, valid = list(plan.matrix_writes), list(plan.matrix_valid)
        held, expired = [], []
        for slot, link in enumerate(self.slot_links):
            parent = plan.joints.get(link.parent)
            anchor = (tuple(parent.position) if parent is not None and parent.valid
                      and all(math.isfinite(v) for v in parent.position) else None)
            if plan.matrix_valid[slot]:
                self._bones[slot] = (None if anchor is None else _ObservedBone(timestamp_ns,
                    AffineMatrixQ1_14(tuple(writes[slot].matrix.values)), anchor))
                continue
            previous = self._bones[slot]
            if previous is None:
                continue
            if timestamp_ns - previous.timestamp_ns > self.hold_ns:
                self._bones[slot] = None
                expired.append(slot)
                continue
            if anchor is None:
                continue
            delta = tuple(a-b for a,b in zip(anchor,previous.parent))
            if math.sqrt(sum(v*v for v in delta)) > self.maximum_anchor_shift:
                self._bones[slot] = None
                continue
            values = list(previous.matrix.values)
            for index, movement in zip((3,7,11),delta):
                values[index] += round(movement * 16384)
            try:
                matrix = AffineMatrixQ1_14(tuple(values))
            except ValueError:
                self._bones[slot] = None
                continue
            writes[slot], valid[slot] = MatrixWrite(slot,matrix), True
            held.append(slot)
        render_plan = (replace(plan,matrix_writes=tuple(writes),matrix_valid=tuple(valid))
                       if held else plan)
        return PreviewPresentationFrame(plan,render_plan,tuple(held),tuple(expired))
