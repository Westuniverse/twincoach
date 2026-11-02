# SPDX-License-Identifier: GPL-2.0-only
"""Opt-in complete13-bone CPU tail; one frame boundary, original rig history.

Numeric sources: preview_rigid_avatar.py, skeleton_pose.py, bone_affine.c.
CPython public C API (PSF) and existing NumPy; GPL-2.0-only project implementation.
"""
from dataclasses import replace
from pathlib import Path
import ctypes,importlib.util,math,sys
from posepriv_coach.skeleton_pose import BoneSegment3D,SkeletonJoint3D
from posepriv_coach.avatar_skinning import AffineMatrixQ1_14,MatrixWrite
from posepriv_coach.display_hands import continuous_palm_down_x_axis

def rig_tail_library(cfg):
    path=cfg.get('preview_rig_tail_library')
    if path is not None and(type(path)is not str or not path):
        raise ValueError('preview_rig_tail_library must be None or a nonempty path string')
    return path


class NativeRigTail:
    def __init__(self,rig,library,*,axis_batch=False):
        if type(axis_batch)is not bool:
            raise ValueError('axis_batch must be boolean')
        import numpy as np
        self.library_path=Path(library).resolve(strict=True)
        self._library=ctypes.PyDLL(str(self.library_path))
        abi=self._library.posepriv_rig_tail_abi;abi.argtypes=[];abi.restype=ctypes.c_uint32
        if abi()!=1:raise ValueError('unsupported native rig tail ABI')
        python=self._library.posepriv_rig_tail_python_abi;python.argtypes=[];python.restype=ctypes.c_uint32
        if python()!=((sys.version_info.major<<16)|sys.version_info.minor):
            raise ValueError('native rig tail requires matching CPython major/minor')
        expected=('spine','neck','head','left_upper_arm','left_lower_arm',
                  'right_upper_arm','right_lower_arm','left_upper_leg','left_lower_leg',
                  'right_upper_leg','right_lower_leg','left_hand','right_hand')
        if rig.names!=expected or rig.indices!=tuple(range(13)):
            raise ValueError('native rig tail requires the ordered complete13-bone topology')
        spec=importlib.util.spec_from_file_location('_posepriv_rig_tail',self.library_path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);self.native=module.finish
        joints=[];metadata=[]
        for slot,name in enumerate(rig.names):
            link=rig.links[rig.indices[slot]]
            for joint in(link.parent,link.child):
                if joint not in joints:joints.append(joint)
            kind=2 if name.endswith('_hand')else 1 if'arm'in name or'leg'in name else 0
            metadata.append((name,link.parent,link.child,0 if name=='head'else int(round(rig.length[slot]*16384)),joints.index(link.parent),joints.index(link.child),kind))
        self.rig=rig
        self.params=(tuple(metadata),(BoneSegment3D,SkeletonJoint3D,AffineMatrixQ1_14,MatrixWrite),np.array,math.fsum,continuous_palm_down_x_axis,rig._limb_axis,np.array([b[:3,0]for b in rig.bind],dtype=np.float64).tobytes(),np.array([b.values for b in rig.inverse],dtype=np.int16).tobytes(),tuple(joints),None,None,None)
        if axis_batch:
            from .native_limb_axis import NativeLimbAxisBuilder
            from .preview_rigid_avatar import PreviewRigidAvatar
            if (getattr(rig._limb_axis,'__func__',None)is not PreviewRigidAvatar._limb_axis
                    or not isinstance(rig.limb_axis_builder,NativeLimbAxisBuilder)):
                raise ValueError('axis batch requires the original rig limb method and native limb builder')
            try:feature=self._library.posepriv_rig_tail_axis_batch_abi
            except AttributeError:
                raise ValueError('selected rig tail library does not enable axis batching')from None
            feature.argtypes=[];feature.restype=ctypes.c_uint32
            if feature()!=1:
                raise ValueError('selected rig tail library does not enable axis batching')
            self.params=self.params[:9]+(True,math.hypot,None)
        self.axis_batch=axis_batch
    def finish(self,render,observed,poses,scale,rotation,dt,head_reference,inferred):
        writes,joints,segments,bone_updates,hand_updates=self.native(self.rig,render,observed,poses,scale,rotation,dt,head_reference,inferred,self.params)
        plan=replace(render,joints=joints,segments=segments,matrix_writes=writes,matrix_valid=tuple(i<13 for i in range(16)))
        return plan,bone_updates,hand_updates
