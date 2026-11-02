"""Optional CPU geometry preparation; this owner never executes DMA or GLES.

Inference leases are copied/released before preparation. One worker owns the
planner, rig and scorer; callers retain the existing source-age qualification.
Prepared plans/poses/scores use the project's frozen snapshot contracts.
"""
import math
from dataclasses import replace
from time import monotonic_ns

from .contracts import CameraView,Keypoint,PoseFrame,SyncedPosePair

DISPLAY_SCORING = 'display_v1'
NEURAL_ONLY_SCORING = 'neural_only_v1'
CURRENT_IMAGE_INFERRED_MASK = 'current_image_inferred_mask_v1'


def validate_scoring_input_mode(value):
    if type(value)is not str or value not in (DISPLAY_SCORING,NEURAL_ONLY_SCORING):
        raise ValueError('unsupported geometry scoring_input_mode')
    return value


def _validate_score_pose(pose,view):
    if type(pose)is not PoseFrame or pose.view is not CameraView(view):
        raise ValueError('neural scoring requires exact per-view PoseFrame values')
    if (any(type(v)is not int or v<0 for v in (pose.frame_id,pose.timestamp_ns))or
            any(type(v)is not int or v<1 for v in (pose.source_width,pose.source_height))):
        raise ValueError('neural scoring requires integer source identity and dimensions')
    for name,point in pose.keypoints.items():
        if (type(name)is not str or not name or type(point)is not Keypoint or
                any(type(v)not in (int,float)or not math.isfinite(v)
                    for v in (point.x,point.y,point.confidence))or not 0<=point.confidence<=1):
            raise ValueError('neural scoring requires finite primitive keypoint values')


def score_poses_for_work(work,mode):
    """Validate both raw sources before any scoring state can advance."""
    if mode==DISPLAY_SCORING:
        if 'neural_score_poses'in work or 'display_pose_semantics'in work:
            raise ValueError('image tracking score/display metadata requires neural_only_v1 scoring mode')
        return work['poses']
    if 'neural_score_poses'not in work:
        raise ValueError('neural_only_v1 requires explicit neural_score_poses')
    semantics=work.get('display_pose_semantics')
    if type(semantics)is not str or semantics!=CURRENT_IMAGE_INFERRED_MASK:
        raise ValueError('neural_only_v1 requires current_image_inferred_mask_v1 display_pose_semantics')
    display=work['poses'];raw=work['neural_score_poses']
    if type(display)is not dict or set(display)!={'front','side'}or any(type(v)is not str for v in display):
        raise ValueError('neural scoring requires both display views')
    for view,pose in display.items():_validate_score_pose(pose,view)
    if raw is None:return None
    if type(raw)is not dict or set(raw)!={'front','side'}or any(type(v)is not str for v in raw):
        raise ValueError('neural_score_poses must be None or both raw views')
    for view,pose in raw.items():
        _validate_score_pose(pose,view);current=display[view]
        if ((pose.frame_id,pose.timestamp_ns,pose.source_width,pose.source_height)!=
                (current.frame_id,current.timestamp_ns,current.source_width,current.source_height)):
            raise ValueError('neural scoring/display source identity or dimensions mismatch')
    return raw


def geometry_pipeline_enabled(cfg):
    enabled=cfg.get('preview_geometry_pipeline',False)
    if type(enabled)is not bool:raise ValueError('preview_geometry_pipeline must be boolean')
    if enabled and (cfg.get('hdmi_display')is not True or cfg.get('preview_inference_process')is not True):
        raise ValueError('preview_geometry_pipeline requires hdmi_display=True and preview_inference_process=True')
    return enabled


def make_display_stabilizer(cfg):
    enabled=cfg.get('preview_display_stabilization',False)
    if type(enabled)is not bool:raise ValueError('preview_display_stabilization must be boolean')
    if not enabled:return None
    from .preview_display_stabilization import AdaptiveDisplayPoseStabilizer
    return AdaptiveDisplayPoseStabilizer()


def make_head_stabilizer(cfg,asset):
    enabled=cfg.get('preview_head_world_stabilization',False)
    if type(enabled)is not bool:raise ValueError('preview_head_world_stabilization must be boolean')
    if not enabled:return None
    if cfg.get('preview_display_stabilization')is not True:
        raise ValueError('head world stabilization requires display stabilization')
    from .preview_head_stabilization import DisplayWorldHeadStabilizer
    return DisplayWorldHeadStabilizer(asset)


def snapshot_inference_work(owner):
    """Detach a whole inference result from its single shared-image read lease."""
    work=owner.take()
    if work is None:return None
    try:
        started=monotonic_ns()
        images={}
        for view,image in work['images'].items():
            owned=image.copy(order='C');owned.flags.writeable=False;images[view]=owned
        stages={view:dict(stages)for view,stages in work['stages'].items()}
        snapshot_ms=(monotonic_ns()-started)/1e6
        snapshot=dict(work)
        snapshot['images']=images
        snapshot['stages']=stages
        snapshot['selected']=dict(work['selected'])
        snapshot['poses']=dict(work['poses'])
        if 'neural_score_poses'in work:
            raw=work['neural_score_poses']
            if raw is not None and type(raw)is not dict:
                raise ValueError('neural_score_poses must be None or both raw views')
            snapshot['neural_score_poses']=None if raw is None else dict(raw)
        snapshot['geometry_image_snapshot_ms']=snapshot_ms
        return snapshot
    finally:owner.release()


class PreviewGeometryBuilder:
    """One geometry owner; scorer=None disables quality scoring explicitly."""
    def __init__(self,runtime,asset,rig_planner,scorer,*,scoring_input_mode=DISPLAY_SCORING,stabilizer=None,head_stabilizer=None):
        self.scoring_input_mode=validate_scoring_input_mode(scoring_input_mode)
        if self.scoring_input_mode==NEURAL_ONLY_SCORING and scorer is None:
            raise ValueError('neural_only_v1 requires the original neural scorer')
        self.runtime=runtime;self.asset=asset;self.rig_planner=rig_planner;self.scorer=scorer
        self.stabilizer=stabilizer
        self.head_stabilizer=head_stabilizer
        self.repetition_count=0;self.completed_score=None

    def prepare(self,work):
        if work is None:return None
        poses=work['poses'];inference_stages=work['stages']
        score_poses=score_poses_for_work(work,self.scoring_input_mode)
        score_started=monotonic_ns()
        if self.scorer is None:
            self.repetition_count=0;self.completed_score=None
            for view in poses:inference_stages[view]['score']=0.
        elif score_poses is None:
            for view in poses:inference_stages[view]['score']=0.
        else:
            for view in score_poses:
                score_begin=monotonic_ns();update=self.scorer.push(score_poses[view])
                inference_stages[view]['score']=(monotonic_ns()-score_begin)/1e6
                if update is not None:
                    self.repetition_count=update.repetition_count
                    if update.repetition_completed:self.completed_score=update.repetition_score
        scored=monotonic_ns()
        if self.stabilizer is not None:
            poses={view:self.stabilizer.apply(pose)for view,pose in poses.items()}
        pair=SyncedPosePair(front=poses['front'],side=poses['side'],skew_ns=abs(poses['front'].timestamp_ns-poses['side'].timestamp_ns))
        runtime=self.runtime;asset=self.asset
        plan=runtime.planner.build(pair,vertex_requests=runtime.vertex_requests,
            bone_bind_lengths_q1_14=asset.bone_bind_lengths_q1_14,inverse_bind_matrices_q1_14=asset.inverse_bind_matrices_q1_14,
            bone_topology_indices=runtime.bone_topology_indices)
        observed_core={name:bool(plan.matrix_valid[asset.bone_names.index(name)])for name in ('spine','neck')}
        head_source=({'face_source':runtime.planner.current_face_source(plan)}
            if getattr(self.rig_planner,'head_current_axes',False)or getattr(self.rig_planner,'leg_recovery',False)else{})
        rig=self.rig_planner.apply(plan,plan,display_timestamp_ns=max(p.timestamp_ns for p in poses.values()),**head_source)
        if self.head_stabilizer is not None:
            rig=self.head_stabilizer.apply(rig,max(p.timestamp_ns for p in poses.values()),
                source_geometry=tuple((poses[v].source_width,poses[v].source_height)for v in ('front','side')))
        if self.scoring_input_mode==NEURAL_ONLY_SCORING or self.stabilizer is not None:
            rig=replace(rig,inferred_slots=tuple(range(len(asset.bone_names))),
                reason='partial'if rig.reason=='tracked'else rig.reason)
        plan=rig.render_plan
        planned=monotonic_ns()
        result=dict(work=work,pair=pair,plan=plan,rig=rig,observed_core=observed_core,
            repetition_count=self.repetition_count,completed_score=self.completed_score,
            score_started_ns=score_started,scored_ns=scored,planned_ns=planned)
        if self.stabilizer is not None:result['display_poses']=poses
        return result
