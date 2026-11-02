"""Display-only arm-chain replay; no synthesized joints or score observations."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from .avatar_pipeline import AvatarFramePlan
from .avatar_skinning import AffineMatrixQ1_14, MatrixWrite, MATRIX_COUNT


@dataclass(frozen=True, slots=True)
class PreviewLimbFrame:
    observed_plan: AvatarFramePlan
    render_plan: AvatarFramePlan
    held_slots: tuple[int, ...]
    chain_held_slots: tuple[int, ...]
    suppressed_slots: tuple[int, ...]
    hold_ages_ns: tuple[tuple[str, int], ...]
    reasons: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _CompleteArm:
    timestamp_ns: int
    matrices: tuple[AffineMatrixQ1_14, ...]
    anchors: tuple[tuple[float, float, float], ...]


class PreviewLimbContinuity:
    """Reuse a complete suffix up to 100 display-clock ms after its observation.

    The first missing bone selects an actually observed root anchor. Available
    downstream anchors must stay close to the translated historical chain.
    Unsupported suffixes are suppressed together instead of leaving a hand
    detached from a missing forearm. All original joints remain untouched.
    """
    def __init__(self, slot_links, *, hold_ms=100., maximum_anchor_shift=.1):
        links=tuple(slot_links)
        if not 1<=len(links)<=MATRIX_COUNT:raise ValueError('one to sixteen mapped bones required')
        if not math.isfinite(hold_ms) or not 0<hold_ms<=100:raise ValueError('limb hold must be within (0,100] ms')
        if not math.isfinite(maximum_anchor_shift) or maximum_anchor_shift<=0:raise ValueError('finite positive anchor shift required')
        by_name={link.name:(slot,link) for slot,link in enumerate(links)}
        self.chains=[]
        for side in ('left','right'):
            names=[side+'_upper_arm',side+'_lower_arm']
            if not all(name in by_name for name in names):continue
            if side+'_hand' in by_name:names.append(side+'_hand')
            self.chains.append((side,tuple(by_name[name] for name in names)))
        if not self.chains:raise ValueError('mapped upper and lower arm required')
        self.hold_ns=round(hold_ms*1e6);self.maximum_anchor_shift=maximum_anchor_shift
        self.reset()

    def reset(self):
        self._timestamp_ns=None
        self._source_timestamp_ns=None
        self._arms={}

    @staticmethod
    def _anchor(plan, name):
        joint=plan.joints.get(name)
        return (tuple(joint.position) if joint is not None and joint.valid
                and all(math.isfinite(v) for v in joint.position) else None)

    def apply(self, presentation, *, display_timestamp_ns):
        observed=presentation.observed_plan;render=presentation.render_plan
        if not observed.pose_preview:raise ValueError('limb continuity requires pose_preview')
        source_timestamp=max(observed.pair.front.timestamp_ns,observed.pair.side.timestamp_ns)
        timestamp=display_timestamp_ns
        if type(timestamp) is not int or timestamp<source_timestamp:
            raise ValueError('display timestamp must be integer and not precede source ready time')
        if ((self._timestamp_ns is not None and timestamp<self._timestamp_ns)
                or (self._source_timestamp_ns is not None and source_timestamp<self._source_timestamp_ns)):
            raise ValueError('non-monotonic limb continuity timestamp')
        if ((self._timestamp_ns is not None and timestamp-self._timestamp_ns>250_000_000)
                or (self._source_timestamp_ns is not None and source_timestamp-self._source_timestamp_ns>250_000_000)):
            self.reset()
        self._timestamp_ns=timestamp
        self._source_timestamp_ns=source_timestamp
        writes=list(render.matrix_writes);valid=list(render.matrix_valid)
        held=set(presentation.held_slots);chain_held=[];suppressed=[];ages=[];reasons=[]
        for side,chain in self.chains:
            anchors=tuple(self._anchor(observed,link.parent) for _,link in chain)
            if all(observed.matrix_valid[slot] for slot,_ in chain):
                if all(anchor is not None for anchor in anchors):
                    self._arms[side]=_CompleteArm(source_timestamp,tuple(AffineMatrixQ1_14(tuple(observed.matrix_writes[slot].matrix.values)) for slot,_ in chain),anchors)
                else:self._arms.pop(side,None)
                continue
            first=next(i for i,(slot,_) in enumerate(chain) if not observed.matrix_valid[slot])
            suffix=chain[first:];previous=self._arms.get(side);reason=None;shifted=[]
            age=timestamp-previous.timestamp_ns if previous is not None else None
            if previous is None:reason='no_complete_history'
            elif age>self.hold_ns:
                reason='expired';self._arms.pop(side,None)
            elif anchors[first] is None:reason='missing_root_anchor'
            else:
                delta=tuple(a-b for a,b in zip(anchors[first],previous.anchors[first]))
                if math.sqrt(sum(v*v for v in delta))>self.maximum_anchor_shift:
                    reason='anchor_motion'
                elif any(anchor is not None and math.dist(anchor,tuple(a+d for a,d in zip(old,delta)))>self.maximum_anchor_shift
                         for anchor,old in zip(anchors[first+1:],previous.anchors[first+1:])):
                    reason='downstream_motion'
                else:
                    try:
                        for matrix in previous.matrices[first:]:
                            values=list(matrix.values)
                            for index,d in zip((3,7,11),delta):values[index]+=round(d*16384)
                            shifted.append(AffineMatrixQ1_14(tuple(values)))
                    except ValueError:reason='matrix_range'
                if reason is not None:self._arms.pop(side,None)
            held.difference_update(slot for slot,_ in suffix)
            if reason is None:
                for (slot,_),matrix in zip(suffix,shifted):
                    writes[slot]=MatrixWrite(slot,matrix);valid[slot]=True
                    held.add(slot);chain_held.append(slot)
                ages.append((side,age))
            else:
                for slot,_ in suffix:
                    if valid[slot]:suppressed.append(slot)
                    valid[slot]=False
                reasons.append((side,reason))
        changed=bool(chain_held or suppressed)
        output=replace(render,matrix_writes=tuple(writes),matrix_valid=tuple(valid)) if changed else render
        return PreviewLimbFrame(observed,output,tuple(sorted(held)),tuple(sorted(chain_held)),
                                tuple(sorted(suppressed)),tuple(ages),tuple(reasons))
