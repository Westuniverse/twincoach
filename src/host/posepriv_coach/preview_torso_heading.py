"""Explicit display-only shoulder heading for current spine, neck and head."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from .avatar_skinning import MatrixWrite, compose_affine_q1_14
from .skeleton_pose import bone_affine_matrix_q1_14, stable_bone_local_x_axis


@dataclass(frozen=True, slots=True)
class TorsoHeadingPreview:
    render_plan: object
    applied_slots: tuple[int, ...]
    reason: str
    yaw_radians: float | None
    target_yaw_radians: float | None
    heading_age_ns: int | None


class PreviewTorsoHeading:
    """Reuse current geometry while changing only core bone transverse axes.

    Missing yaw retains a display orientation prior, never a pose observation.
    Invalid core slots remain invalid; the original planner and masks are read-only.
    """

    def __init__(self, bone_links, topology_indices, bind_lengths, inverse_bind,
                 *, affine_composer=None,bone_affine_builder=None):
        self.links=tuple(bone_links);self.indices=tuple(topology_indices)
        self.bind_lengths=tuple(bind_lengths);self.inverse_bind=tuple(inverse_bind)
        if (not 1<=len(self.indices)<=16 or len(self.bind_lengths)!=len(self.indices)
                or len(self.inverse_bind)!=len(self.indices)
                or any(type(i) is not int or not 0<=i<len(self.links) for i in self.indices)):
            raise ValueError('torso heading requires matching topology and binding metadata')
        names=[self.links[i].name for i in self.indices]
        self.slots={name:names.index(name) for name in ('spine','neck','head')}
        self.compose=compose_affine_q1_14 if affine_composer is None else affine_composer
        if not callable(self.compose):raise TypeError('affine composer must be callable')
        self.bone_affine=bone_affine_matrix_q1_14 if bone_affine_builder is None else bone_affine_builder
        if not callable(self.bone_affine):raise TypeError('bone affine builder must be callable')
        self.reset()

    def reset(self):
        self._timestamp_ns=None;self._yaw=None;self._supported_ns=None

    @staticmethod
    def _visible(point):
        return (point is not None and point.confidence>=.5 and math.isfinite(point.x)
                and math.isfinite(point.y) and 0<=point.x<=1 and 0<=point.y<=1)

    def _target(self, observed, display_timestamp_ns):
        pair=observed.pair
        age=display_timestamp_ns-max(pair.front.timestamp_ns,pair.side.timestamp_ns)
        if age<0 or age>100_000_000:return None,'stale_source'
        for frame,names in ((pair.front,('left_shoulder','right_shoulder')),
                (pair.side,('left_shoulder','right_shoulder','left_hip','right_hip'))):
            if not all(self._visible(frame.keypoints.get(n)) for n in names):
                return None,'weak_view'
        joints=observed.joints
        if not all(n in joints and joints[n].valid for n in ('left_shoulder','right_shoulder')):
            return None,'missing_shoulders'
        left=joints['left_shoulder'].position;right=joints['right_shoulder'].position
        x,z=left[0]-right[0],left[2]-right[2]
        if not math.isfinite(x) or not math.isfinite(z) or math.hypot(x,z)<.08:
            return None,'degenerate_shoulders'
        return math.atan2(z,x),'supported'

    def apply(self, observed, render, *, display_timestamp_ns):
        if observed.pair is not render.pair:raise ValueError('torso heading requires the same source pair')
        timestamp=(observed.pair.front.timestamp_ns+observed.pair.side.timestamp_ns)//2
        previous=self._timestamp_ns
        if previous is not None and timestamp<previous:raise ValueError('non-monotonic torso timestamp')
        if previous is not None and timestamp-previous>250_000_000:self.reset()
        self._timestamp_ns=timestamp
        if not all(observed.matrix_valid[self.slots[n]] and render.matrix_valid[self.slots[n]]
                   for n in ('spine','neck')):
            self.reset();self._timestamp_ns=timestamp
            return TorsoHeadingPreview(render,(),'missing_torso',None,None,None)
        target,reason=self._target(observed,display_timestamp_ns)
        yaw=self._yaw;supported=self._supported_ns
        if target is not None:
            if yaw is None:yaw=target
            else:
                delta=(target-yaw+math.pi)%(2*math.pi)-math.pi
                limit=4.*(timestamp-previous)/1e9 if previous is not None else 0.
                yaw+=max(-limit,min(limit,delta))
            supported=max(observed.pair.front.timestamp_ns,observed.pair.side.timestamp_ns)
        elif yaw is None:
            return TorsoHeadingPreview(render,(),reason,None,None,None)
        else:reason='held_'+reason
        reference=(math.cos(yaw),0.,math.sin(yaw));updates=[]
        try:
            for name,slot in self.slots.items():
                if not observed.matrix_valid[slot] or not render.matrix_valid[slot]:continue
                segment=render.segments[self.indices[slot]]
                axis=stable_bone_local_x_axis(segment,reference)
                matrix=self.bone_affine(segment,local_x_reference=axis,
                    local_bone_length_q1_14=self.bind_lengths[slot] if name!='head' else None)
                if matrix is None or axis is None:raise ValueError('core axis is degenerate')
                matrix=self.compose(matrix,self.inverse_bind[slot]);updates.append(MatrixWrite(slot,matrix))
        except ValueError:
            self.reset();self._timestamp_ns=timestamp
            return TorsoHeadingPreview(render,(),'rejected',None,target,None)
        writes=list(render.matrix_writes)
        for update in updates:writes[update.index]=update
        self._yaw=yaw;self._supported_ns=supported
        return TorsoHeadingPreview(replace(render,matrix_writes=tuple(writes)),
            tuple(w.index for w in updates),reason,yaw,target,display_timestamp_ns-supported)
