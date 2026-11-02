# SPDX-License-Identifier: GPL-2.0-only
"""Cold, single-head-owner native batch; Python retains source/state policy."""
from dataclasses import replace
import importlib.util
from pathlib import Path
import numpy as np
from posepriv_coach.avatar_skinning import AffineMatrixQ1_14,MatrixWrite
from posepriv_coach.avatar_image_source import avatar_image_source
from posepriv_coach.preview_head_orientation import HeadOrientationPreview
from posepriv_coach.preview_rigid_avatar import _dot
from posepriv_coach.skeleton_pose import build_bone_segments

class NativeHeadBatch:
    def __init__(self,head,library):
        self.head=head
        # Same generic affected-link contract as PreviewHeadOrientation's cold
        # segment-reuse path, including any downstream link sharing head.child.
        self.head_segment_indices=tuple(i for i,link in enumerate(head.links)
                                        if head.link.child in(link.parent,link.child))
        self.head_links=tuple(head.links[i]for i in self.head_segment_indices)
        spec=importlib.util.spec_from_file_location('_posepriv_head_batch',Path(library))
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.constants=np.array([*head.bind.flat,*head.inverse.values,head.length,head.maximum_rate],dtype=np.float64)
        self.input=np.zeros(56);self.output=np.zeros(28)
    def apply(self,preview,candidates=(),*,display_timestamp_ns):
        h=self.head;plan=preview.render_plan;front=avatar_image_source(plan);previous=h._timestamp
        if previous is not None and front.timestamp_ns<previous:raise ValueError('non-monotonic current head timestamp')
        if previous is not None and front.timestamp_ns-previous>250_000_000:h.reset();previous=None
        if preview.reason not in('tracked','partial')or preview.body_scale is None:
            h.reset();return HeadOrientationPreview(preview,'rig_reference','unavailable_body',None,None)
        source,reason,age='rig_reference','no_current_face',None;rejected=[];selected=None
        for candidate in candidates:
            if candidate.frame_id!=front.frame_id or candidate.timestamp_ns!=front.timestamp_ns:
                reason='source_mismatch';rejected.append((candidate.source,reason));continue
            age=display_timestamp_ns-candidate.timestamp_ns
            if not 0<=age<=100_000_000:reason='stale_face';rejected.append((candidate.source,reason));continue
            r,b=candidate.rotation,candidate.source_body_rotation
            if _dot((r[2],r[5],r[8]),(b[2],b[5],b[8]))<=0:
                reason='face_body_conflict';rejected.append((candidate.source,reason));continue
            selected=candidate;source,reason=candidate.source,'current_inferred';break
        recovering=source=='rig_reference';i=self.input;joints=plan.joints
        i[:12]=plan.matrix_writes[h.slot].matrix.values
        for n,name in enumerate(('neck','pelvis','left_shoulder','right_shoulder')):i[12+3*n:15+3*n]=joints[name].position
        if selected is not None:i[24:33]=selected.rotation;i[33:42]=selected.source_body_rotation
        if h._rotation is not None:i[42:51]=h._rotation.flat
        i[51:56]=preview.body_scale,(front.timestamp_ns-previous)/1e9 if previous is not None else 0.,h._rotation is not None,recovering,h._active
        self.module.batch(self.constants,i,self.output);o=self.output
        relative=o[:9].reshape(3,3).copy();error=float(o[24]);active=bool(o[25])
        if o[27]:reason='current_direction_priority'
        if not o[26]:
            h._timestamp,h._rotation,h._active=front.timestamp_ns,relative,False
            return HeadOrientationPreview(preview,source,reason,age,error,tuple(rejected))
        writes=list(plan.matrix_writes);writes[h.slot]=MatrixWrite(h.slot,AffineMatrixQ1_14(tuple(map(int,o[12:24]))))
        new_joints=dict(joints);new_joints[h.link.child]=replace(joints[h.link.child],position=tuple(o[9:12]),confidence=0.)
        if h.reuse_unchanged_segments:
            segments=list(plan.segments)
            for index,changed in zip(self.head_segment_indices,build_bone_segments(new_joints,self.head_links)):
                segments[index]=changed
            segments=tuple(segments)
        else:
            segments=build_bone_segments(new_joints,h.links)
        rendered=replace(plan,joints=new_joints,segments=segments,matrix_writes=tuple(writes))
        result=replace(preview,render_plan=rendered,head_mode=source if not recovering else'head_reference_recovery',
          reason='partial'if preview.reason=='tracked'else preview.reason,inferred_slots=tuple(sorted(set(preview.inferred_slots)|{h.slot})))
        h._timestamp,h._rotation,h._active=front.timestamp_ns,relative,active
        return HeadOrientationPreview(result,source,reason,age,error,tuple(rejected))

def install(head,library):
    batch=NativeHeadBatch(head,library);head.apply=batch.apply;head._native_head_batch=batch;return batch


def install_on_owner(owner,library):
    """Cold optional Full33 splice; head/filter owner and state stay the same."""
    batch=install(owner.world.head,library)
    owner.native_head_math_batch=batch
    return owner
