"""Current dual-view leg support for derived display geometry, GPL-2.0-only."""
from dataclasses import replace
import math

from .preview_face_source import FaceAxisSource
from .skeleton_pose import SkeletonJoint3D


def recover_current_legs(observed, render, slots, *, face_source, display_ns):
    """Return a separate render driver and its explicitly inferred leg slots.

    A weak front point is never upgraded in the observed plan. All three current
    front coordinates (>=.30) and corresponding profile points (>=.50) must be
    finite/in-frame; both source frames must be at most 100ms old. The caller
    supplies the forward sign bound to exactly this source pair.
    """
    pair=observed.pair
    if (type(face_source)is not FaceAxisSource or not face_source.matches(pair) or
            face_source.side_forward_sign not in (-1.,1.) or
            not all(0<=display_ns-f.timestamp_ns<=100_000_000 for f in (pair.front,pair.side))):
        return render,()
    def good(p,threshold):
        return (p is not None and all(math.isfinite(v)for v in (p.x,p.y,p.confidence)) and
                p.confidence>=threshold and 0<=p.x<=1 and 0<=p.y<=1)
    joints=None;valid=None;recovered=[]
    hips=[pair.side.keypoints.get(n)for n in ('left_hip','right_hip')]
    f,s=pair.front,pair.side
    fs,ss=max(f.source_width,f.source_height),max(s.source_width,s.source_height)
    for side in ('left','right'):
        chain=tuple(slots[side+'_'+part]for part in ('upper_leg','lower_leg'))
        if all(render.matrix_valid[i]for i in chain):continue
        names=tuple(side+'_'+part for part in ('hip','knee','ankle'))
        front=[f.keypoints.get(n)for n in names];profile=[s.keypoints.get(n)for n in names]
        if not all(good(p,.30)for p in front)or not all(good(p,.50)for p in profile):continue
        # Only root-relative differences enter the leg solver. When the other
        # hip is weak the current leg root is a sufficient common depth origin.
        origin=sum(p.x for p in hips)/2 if all(good(p,.50)for p in hips)else profile[0].x
        positions=[((p.x-.5)*2*f.source_width/fs,(.5-p.y)*2*f.source_height/fs,
            (origin-q.x)*face_source.side_forward_sign*2*s.source_width/ss)for p,q in zip(front,profile)]
        if any(math.dist(a,b)<1e-7 for a,b in zip(positions,positions[1:])):continue
        if joints is None:joints=dict(render.joints);valid=list(render.matrix_valid)
        for name,p,q,position in zip(names,front,profile,positions):
            joints[name]=SkeletonJoint3D(name,position,min(p.confidence,q.confidence),math.inf,True)
        for i in chain:valid[i]=True
        recovered.extend(chain)
    return (render if joints is None else replace(render,joints=joints,matrix_valid=tuple(valid))),tuple(recovered)
