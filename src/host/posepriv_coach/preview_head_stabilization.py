# SPDX-License-Identifier: GPL-2.0-only
"""Adaptive final-display-world head rotation; no feedback to recognition or rig history."""
from dataclasses import replace
import math
import numpy as np
from posepriv_coach.preview_body_reference import _quaternion,_rotation
from posepriv_coach.avatar_skinning import AffineMatrixQ1_14,MatrixWrite
from posepriv_coach.skeleton_pose import _quantize_q1_14
from posepriv_coach.preview_rigid_avatar import _leg_inverse3


def proper_rotation(linear):
    u,_,vt=np.linalg.svd(linear)
    return u@np.diag((1.,1.,np.linalg.det(u@vt)))@vt

def rotvec(matrix):
    q=np.asarray(_quaternion(tuple(matrix.flat)))
    if q[0]<0:q=-q
    length=float(np.linalg.norm(q[1:]))
    return np.zeros(3)if length<1e-12 else q[1:]*(2*math.atan2(length,float(q[0]))/length)

def slerp(a,b,alpha):
    qa=np.array(_quaternion(tuple(a.flat)));qb=np.array(_quaternion(tuple(b.flat)))
    dot=float(qa@qb)
    if dot<0:qb=-qb;dot=-dot
    dot=max(-1.,min(1.,dot))
    if dot>1-1e-12:q=qa+alpha*(qb-qa);q/=np.linalg.norm(q)
    else:
        angle=math.acos(dot)
        q=(math.sin((1-alpha)*angle)*qa+math.sin(alpha*angle)*qb)/math.sin(angle)
    return np.asarray(_rotation(tuple(q))).reshape(3,3)


def solve_affine(display,filtered):
    """Invert the contracted affine display transform, without dense LAPACK."""
    inverse=_leg_inverse3(tuple(float(display[r,c])for r in range(3)for c in range(3)))
    translation=tuple(float(display[r,3])for r in range(3))
    rows=[inverse[3*r:3*r+3]+(-math.fsum(inverse[3*r+c]*translation[c]for c in range(3)),)
          for r in range(3)]
    return np.asarray(rows+[(0.,0.,0.,1.)])@filtered


class AdaptiveWorldHeadRotation:
    def __init__(self,min_cutoff=12.,beta=2.,derivative_cutoff=1.):
        self.min_cutoff=min_cutoff;self.beta=beta;self.derivative_cutoff=derivative_cutoff
        self.reset()
    def reset(self):self.raw=None;self.filtered=None;self.derivative=np.zeros(3);self.clock=None
    def apply(self,world,clock,mode='tracked'):
        target=proper_rotation(world[:3,:3]);old=self.filtered
        if mode in ('neutral','rejected'):
            self.reset();return world.copy()
        if old is None or not 0<clock-self.clock<=200_000_000:
            filtered=target
            self.derivative=np.zeros(3)
        else:
            dt=(clock-self.clock)*1e-9
            velocity=rotvec(target@self.raw.T)/dt
            ad=dt/(dt+1/(2*math.pi*self.derivative_cutoff))
            self.derivative+=ad*(velocity-self.derivative)
            cutoff=self.min_cutoff+self.beta*float(np.linalg.norm(self.derivative))
            alpha=dt/(dt+1/(2*math.pi*cutoff))
            filtered=slerp(old,target,alpha)
        result=world.copy()
        # Left proper rotation preserves original scale, shear, and column norms;
        # the current transform origin is copied exactly, without head/neck drift.
        result[:3,:3]=(filtered@target.T)@world[:3,:3]
        self.raw=target;self.filtered=filtered;self.clock=clock
        return result


class DisplayWorldHeadStabilizer:
    """Change only the returned head palette, retaining its current neck origin."""
    def __init__(self,asset):
        self.slot=tuple(asset.bone_names).index('head')
        self.inverse=np.eye(4)
        self.inverse[:3]=np.asarray(asset.inverse_bind_matrices_q1_14[self.slot].values).reshape(3,4)/16384
        from .preview_rigid_avatar import _inverse_bind
        self.bind=_inverse_bind(asset.inverse_bind_matrices_q1_14[self.slot])
        self.rotation=AdaptiveWorldHeadRotation()
        self.source_geometry=None

    def apply(self,rig,clock,*,source_geometry=None):
        if source_geometry!=self.source_geometry:
            self.rotation.reset();self.source_geometry=source_geometry
        if rig.reason in ('neutral','rejected'):
            self.rotation.reset();return rig
        plan=rig.render_plan;D=np.asarray(rig.display_transform).reshape(4,4)
        skin=np.eye(4)
        skin[:3]=np.asarray(plan.matrix_writes[self.slot].matrix.values).reshape(3,4)/16384
        world=D@skin@self.bind
        filtered=self.rotation.apply(world,clock,rig.reason)
        updated=solve_affine(D,filtered)@self.inverse
        values=tuple(_quantize_q1_14(float(v))for v in updated[:3].flat)
        writes=list(plan.matrix_writes)
        writes[self.slot]=MatrixWrite(self.slot,AffineMatrixQ1_14(values))
        return replace(rig,render_plan=replace(plan,matrix_writes=tuple(writes)))
