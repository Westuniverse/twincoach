# SPDX-License-Identifier: GPL-2.0-only
"""One explicit front-half batch; original rig policies/history/tail retained."""
import importlib.util
from pathlib import Path
import numpy as np

CHAINS=(('left','arm','shoulder','elbow','wrist'),('left','leg','hip','knee','ankle'),
        ('right','arm','shoulder','elbow','wrist'),('right','leg','hip','knee','ankle'))


def drivers(rig,observed,render,neutral):
    selected=[];inferred=[]
    for side,limb,parent,child,tip in CHAINS:
        upper,lower=side+'_upper_'+limb,side+'_lower_'+limb;i,j=rig.slot[upper],rig.slot[lower]
        current=render.joints;missing=[]
        if neutral:current=None
        elif not all(render.matrix_valid[k]for k in(i,j)):
            if limb!='arm':current=None
            else:
                names=tuple(side+'_'+part for part in(parent,child,tip))
                if(not all(observed.segments[rig.indices[k]].valid for k in(i,j))or
                   not all(n in observed.joints and observed.joints[n].valid and observed.joints[n].confidence>=.5 for n in names)):current=None
                else:current=observed.joints;missing.extend((upper,lower))
        if current is not None and not all(observed.matrix_valid[k]for k in(i,j)):missing.extend((upper,lower))
        selected.append(current);inferred.append(missing)
    return selected,inferred


class NativeRigLimbBatch:
    def __init__(self,rig,library):
        library=Path(library).resolve(strict=True)
        spec=importlib.util.spec_from_file_location('_posepriv_rig_limb_batch',str(library));module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        lengths=[];proportions=[];starts=[];names=[]
        for side,limb,parent,child,tip in CHAINS:
            upper,lower=side+'_upper_'+limb,side+'_lower_'+limb
            starts.extend(rig.start[upper]);lengths.extend([rig.length[rig.slot[upper]],rig.length[rig.slot[lower]]])
            proportions.extend([rig.proportions['upper_'+limb],rig.proportions['lower_'+limb]])
            names.append(tuple(side+'_'+p for p in(parent,child,tip)))
        spine=[v for name in('spine','neck')for end in(rig.start[name],rig.end[name])for v in end]
        from .world_avatar_preview import _WorldRig
        from .preview_rigid_avatar import PreviewRigidAvatar
        solver=getattr(rig._ik,'__func__',None)
        if solver not in(_WorldRig._ik,PreviewRigidAvatar._ik):raise ValueError('native limb batch requires the original selected rig solver')
        world_direct=solver is _WorldRig._ik
        self.module=module;self.context=module.create(tuple(rig.origin),starts,spine,lengths,proportions,names,world_direct);self.rig=rig;self.calls=0

    def prepare(self,observed,render,pelvis,rotation,scale,neutral):
        selected,inferred=drivers(self.rig,observed,render,neutral)
        torso,limbs=self.module.batch(self.context,pelvis,rotation.flat,scale,selected,self.rig.finite_arm_reach)
        self.calls+=1
        for n,row in enumerate(limbs):
            if row[7]:
                side,limb,*_=CHAINS[n];inferred[n].extend((side+'_upper_'+limb,side+'_lower_'+limb))
        return torso,limbs,inferred



class NativeRigLimbBatchFactory:
    """Cold factory passed as PreviewRigidAvatar.native_limb_builder.

    The constructor calls this once after fixed geometry/tail initialization;
    the bound prepare is the sole per-frame native boundary. No global patches.
    """
    def __init__(self,library):self.library=Path(library).resolve(strict=True)

    def __call__(self,rig):return NativeRigLimbBatch(rig,self.library).prepare
