# SPDX-License-Identifier: GPL-2.0-only
"""Explicit camera-bound experimental v2 input + typed late spatial transforms."""
from dataclasses import replace
import math
from time import monotonic_ns
from types import MethodType
import numpy as np
from posepriv_coach.avatar_skinning import AffineMatrixQ1_14,MatrixWrite
from posepriv_coach.skeleton_pose import _quantize_q1_14
from posepriv_coach.two_view_display_geometry import install_two_view_camera_input
from torso_input_translate_head import TwoViewTorsoHeadTranslationInput
from neutral_root_runtime import prepare_delta

SCHEMA='posepriv.experimental_display_v2_camera.v1'


class TypedDisplayV2:
    def __init__(self,owner,profile):
        self.geometry=owner.two_view_input_driver.expected_geometry
        self.grounding=profile['grounding_reference']
        pitch=profile['head_post_pitch_radians'];c,s=math.cos(-pitch),math.sin(-pitch)
        self.C=np.array(((1.,0.,0.),(0.,c,-s),(0.,s,c)))
        self.head_slot=tuple(owner.asset.bone_names).index('head')
        self.head_topology=owner.runtime.bone_topology_indices[self.head_slot]

    def apply(self,work,prepared):
        # One late spatial boundary, including empty/neutral head, with explicit
        # camera geometry. The old different-camera clips remain outside it.
        observations=work['world_observations']
        if any((observations[v].source.view,observations[v].source.width,
                observations[v].source.height,observations[v].source.rotation_degrees)!=g
               for v,g in self.geometry.items()):return prepared
        rig,plan=prepared['rig'],prepared['plan']
        delta,active,_=prepare_delta(prepared,work,self.grounding)
        display=rig.display_transform
        if active and delta!=0.:
            values=list(display);values[7]+=delta;display=tuple(values)
        D=np.asarray(display).reshape(4,4);A=D[:3,:3]
        neck=np.asarray(plan.joints['neck'].position)
        Q=np.linalg.solve(A,self.C@A)
        H=np.eye(4);H[:3,:3]=Q;H[:3,3]=neck-Q@neck
        skin=np.eye(4);skin[:3]=np.asarray(plan.matrix_writes[self.head_slot].matrix.values).reshape(3,4)/16384
        ideal=H@skin
        matrix=AffineMatrixQ1_14(tuple(_quantize_q1_14(float(v))for v in ideal[:3].flat))
        matrices=list(plan.matrix_writes);matrices[self.head_slot]=MatrixWrite(self.head_slot,matrix)
        tip=np.asarray(plan.joints['nose'].position);new_tip=neck+Q@(tip-neck)
        joints=dict(plan.joints);joints['nose']=replace(joints['nose'],position=tuple(map(float,new_tip)))
        segments=list(plan.segments);head=segments[self.head_topology]
        segments[self.head_topology]=replace(head,start=joints[head.parent].position,end=joints[head.child].position,
            length=math.dist(joints[head.parent].position,joints[head.child].position))
        new_plan=replace(plan,matrix_writes=tuple(matrices),joints=joints,segments=tuple(segments))
        return dict(prepared,plan=new_plan,rig=replace(rig,render_plan=new_plan,display_transform=display))


def install_display_v2_bridge(owner,profile=None):
    """Called once after build_full33_owner; absent/disabled stays original."""
    if not isinstance(profile,dict)or profile.get('enabled')is not True:return owner
    if profile.get('schema')!=SCHEMA or profile.get('experimental_display_only')is not True:
        raise ValueError('explicit experimental display-v2 camera profile required')
    if owner.world.head_reference is not None:
        raise ValueError('median postC profile is for original unreferenced head path, not old0220 frontend reference')
    if profile.get('reuse_unchanged_head_segments') is True:
        owner.world.head.reuse_unchanged_segments = True
    camera=profile['camera_profile'];geometry=camera['source_geometry']
    if (camera['primary_avatar_view']!='side' or camera['minimum_q']!=.5 or
        {v:(g['width'],g['height'],g['rotation_degrees'])for v,g in geometry.items()}!={'front':(360,640,90),'side':(360,640,270)}):
        raise ValueError('display-v2 reviewed physical camera geometry differs')
    driver=getattr(owner,'two_view_input_driver',None)
    if driver is None:
        install_two_view_camera_input(owner,camera)
        driver=getattr(owner,'two_view_input_driver',None)
        if driver is None:raise ValueError('camera-bound input installation was not enabled')
    elif owner.two_view_input_receipt['profile_id']!=camera['profile_id']:
        raise ValueError('already-installed camera input differs from experimental profile')
    native_library=profile.get('native_geometry_library')
    if native_library:
        from native_v2_math import NativeV2Math
        native=NativeV2Math(driver,native_library)
        driver.apply=native.apply
        owner.display_v2_native_math=native
    else:
        driver.apply=MethodType(TwoViewTorsoHeadTranslationInput.apply,driver)
    head_batch_library=profile.get('head_batch_library')
    if head_batch_library:
        from native_head_batch import install_on_owner
        install_on_owner(owner,head_batch_library)
    rig_limb_library=profile.get('rig_limb_library')
    if rig_limb_library:
        from posepriv_coach.native_rig_limb_batch import NativeRigLimbBatchFactory
        owner.world.rig.native_limb_builder=NativeRigLimbBatchFactory(rig_limb_library)(owner.world.rig)
    spatial=TypedDisplayV2(owner,profile);original=owner.prepare
    def prepare(work):
        value=spatial.apply(work,original(work))
        # Keep existing end-of-prepare timestamp semantics; GL/owner confirmation
        # clocks and all original inference/source clocks are untouched.
        if 'planned_ns'in value:value['planned_ns']=monotonic_ns()
        return value
    owner.prepare=prepare
    owner.display_v2_bridge=spatial
    owner.display_v2_receipt=dict(enabled=True,profile_id=profile['profile_id'],experimental_display_only=True,
      original_world_filter=type(owner.world_stabilizer).__name__,added_filter=False,
      geometry_math_backend='native_double_batch' if native_library else 'original_python_reference',
      head_math_backend='native_double_batch' if head_batch_library else 'original_python_reference',
      limb_math_backend='native_double_batch' if rig_limb_library else 'original_python_reference',
      reuse_unchanged_head_segments=profile.get('reuse_unchanged_head_segments') is True,
      preworld='v2 head0..10 translation; shoulder/arm11..22 rotation then original arm fit',
      late='same grounding then fixed postC, immutable plan and rig.render_plan together',
      head_post_pitch_radians=profile['head_post_pitch_radians'],grounding_source=profile['grounding_reference_source'],
      live_quality_qualified=False,recorded18_hybrid_causal_quality_qualified=False)
    return owner
