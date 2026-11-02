# SPDX-License-Identifier: GPL-2.0-only
"""Board-only Full33 detector -> DPU/Tail4 -> current-image/world preview glue."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import math
from pathlib import Path
import sys
from time import monotonic_ns

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent/'frontend'),
                str(HERE.parent/'adapter'),
                str(HERE.parent/'client')]
from live_frontend import PoseDetector, LiveRoiFrontend, alignment_roi
from preview_adapter import (TailHeads, identity_for, DecodedFull33)
from posepriv_coach.avatar_image_source import AvatarImageSource
from posepriv_coach.contracts import PoseFrame, CameraView, SyncedPosePair
from posepriv_coach.scoring_view_mapping import ScorerViewAdapter
from posepriv_coach.world_avatar_preview import WorldDisplayObservation, WorldAvatarPreview

BACKEND = 'full33_dpu_tail4'
VIEWS = ('front', 'side')


def pose_backend(cfg):
    if cfg.get('preview_pose_backend') != BACKEND:
        raise ValueError('private candidate requires full33_dpu_tail4')
    return BACKEND


def empty_decoded(source):
    pose = PoseFrame(CameraView(source.view),source.frame_id,source.timestamp_ns,{},source.width,source.height)
    return DecodedFull33(source,pose,WorldDisplayObservation(source,(),()),(),0.)


class AsyncDetector:
    """One serialized detector owner; UI/inference never waits on cold detection.

    A completed box is only a bounded-age crop prior for a later CURRENT image.
    No landmark/world observation is borrowed from the detector's older image.
    """
    def __init__(self,detector,*,max_roi_age_ns=1_000_000_000):
        self.detector=detector;self.max_age_ns=max_roi_age_ns
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='tail4-detector')
        self.jobs={};self.submitted=0;self.completed=0;self.expired=0

    @staticmethod
    def geometry(source):
        return source.view,source.width,source.height,source.rotation_degrees

    def take(self,source,epoch):
        job=self.jobs.get(source.view)
        if job is None or not job[2].done():return None
        previous,old_epoch,future=self.jobs.pop(source.view)
        detections=future.result();self.completed+=1
        if (old_epoch!=epoch or self.geometry(previous)!=self.geometry(source) or
                not 0<=source.timestamp_ns-previous.timestamp_ns<=self.max_age_ns):
            self.expired+=1;return None
        return detections

    def submit(self,source,rgb,epoch):
        if source.view in self.jobs:return
        self.jobs[source.view]=(source,epoch,self.pool.submit(self.detector.detect,rgb))
        self.submitted+=1

    def close(self):
        for _,_,future in self.jobs.values():future.cancel()
        self.pool.shutdown(wait=True,cancel_futures=True)

    def stats(self):
        return dict(submitted=self.submitted,completed=self.completed,expired=self.expired,
                    pending=len(self.jobs),actual_detector_invocations=self.detector.calls)


class AsyncViewFrontend:
    def __init__(self,broker):
        self.broker=broker;self.frontend=LiveRoiFrontend(broker.detector);self.decoder=None

    def prepare(self,source,rgb,epoch):
        f=self.frontend;old=f.previous
        if old is not None:
            if epoch==f.epoch and source.timestamp_ns<old.timestamp_ns:
                raise ValueError('source moved backwards within capture epoch')
            if (self.broker.geometry(old)!=self.broker.geometry(source) or epoch!=f.epoch
                    or source.timestamp_ns-old.timestamp_ns>250_000_000):
                f.rect=None
        if f.rect is None:
            detections=self.broker.take(source,epoch)
            if detections:
                rect=alignment_roi(detections[0].keypoints4,source.width,source.height)
                if min(rect[2:4])>0:f.rect=rect
            if f.rect is None:
                self.broker.submit(source,rgb,epoch)
                f.previous,f.epoch=source,epoch
                return None
        return f.prepare(source,rgb,epoch=epoch)

    def finish(self,output):
        source=self.frontend.pending.source
        heads=TailHeads(identity_for(source),output['head195'],output['world117'],output['presence_logit'])
        if self.decoder is None:return self.frontend.finish(heads,output['heat233'])
        decoded=self.decoder.decode(source,self.frontend.pending.roi,heads,output['heat233'])
        self.frontend.update(decoded)
        return decoded

    def cancel(self):self.frontend.cancel()


class Tail4PreviewModel:
    def __init__(self,cfg,*,worker=None,detector=None):
        self.cfg=cfg;self.epoch=0;self.frames=0;self.pairs=0
        if detector is None:
            from ai_edge_litert.interpreter import Interpreter,OpResolverType
            resolver=(OpResolverType.BUILTIN_WITHOUT_DEFAULT_DELEGATES
                      if cfg.get('tail4_detector_resolver','default')=='builtin' else OpResolverType.BUILTIN)
            detector=PoseDetector(interpreter=Interpreter(model_path=cfg['tail4_detector_model'],
                num_threads=cfg.get('tail4_detector_threads',2),experimental_op_resolver_type=resolver))
        self.detector=AsyncDetector(detector,max_roi_age_ns=cfg.get('tail4_detector_max_roi_age_ns',1_000_000_000))
        self.frontends={v:AsyncViewFrontend(self.detector)for v in VIEWS}
        self.native_cropper=None
        if cfg.get('tail4_crop_library'):
            sys.path.insert(0,str(HERE.parent/'crop'))
            from fast_crop import NativeCropper
            from fast_frontend import FastBgrViewFrontend
            self.native_cropper=NativeCropper(cfg['tail4_crop_library'])
            self.frontends={v:FastBgrViewFrontend(self.detector,self.native_cropper)for v in VIEWS}
        self.native_decoder=None
        if cfg.get('tail4_decode_library'):
            sys.path.insert(0,str(HERE.parent/'decode'))
            from native_decoder import NativeDecoder
            self.native_decoder=NativeDecoder(cfg['tail4_decode_library'])
            for frontend in self.frontends.values():frontend.decoder=self.native_decoder
        if worker is None:
            from combined_client import CombinedWorker
            worker=CombinedWorker(cfg['tail4_worker_command'],timeout=cfg.get('tail4_worker_timeout',5))
        self.worker=worker;worker.start()

    def infer_frames(self,frames,rotations):
        import cv2
        prepared={};decoded={};times={};sources={}
        for view,frame in zip(VIEWS,frames):
            source=AvatarImageSource(view,frame.frame_id,frame.timestamp_ns,
                                    frame.width,frame.height,rotations[view])
            sources[view]=source
            started=monotonic_ns()
            rgb=frame.payload if self.native_cropper is not None else cv2.cvtColor(frame.payload,cv2.COLOR_BGR2RGB)
            value=self.frontends[view].prepare(source,rgb,self.epoch)
            times[view]={'roi_crop_prepare':(monotonic_ns()-started)/1e6}
            if value is None:decoded[view]=empty_decoded(source)
            else:prepared[view]=value
        if prepared:
            views=tuple(v for v in VIEWS if v in prepared)
            identities=[identity_for(prepared[v].source)for v in views]
            pixels=[prepared[v].input_q7 for v in views]
            started=monotonic_ns()
            try:
                def finish(view,output):
                    decode_started=monotonic_ns()
                    decoded[view]=self.frontends[view].finish(output)
                    times[view].update(combined_response_wall=(decode_started-started)/1e6,
                                       full33_tail_decode=(monotonic_ns()-decode_started)/1e6)
                if len(views)==2:
                    with self.worker.pipelined(identities,pixels,None)as outputs:
                        for view,output in zip(views,outputs,strict=True):finish(view,output)
                else:finish(views[0],self.worker.infer(identities[0],pixels[0]))
            except BaseException:
                for view in views:self.frontends[view].cancel()
                raise
            self.frames+=len(views)
            if len(views)==2:self.pairs+=1
        return decoded,times

    def stats(self):
        return dict(frames=self.frames,pairs=self.pairs,detector=self.detector.stats(),
                    combined_worker=self.worker.stats(),online_compute='KV260 ARM+DPU+PL')

    def close(self):
        try:self.detector.close()
        finally:self.worker.close()


def make_stronger_model(cfg):return Tail4PreviewModel(cfg)


def estimate_pair(decoded,model,rotations):
    captured=decoded['captured']
    frames=[captured[v][0]for v in VIEWS]
    # This is the single source-ticket boundary: exact integers, role, dimensions.
    for view,frame in zip(VIEWS,frames):
        if ((frame.frame_id,frame.timestamp_ns)!=decoded['selected'][view][:2] or
                frame.view is not CameraView(view) or frame.pixel_format!='bgr8'):
            raise ValueError('captured frame differs from original physical source')
    started=monotonic_ns();observations,times=model.infer_frames(frames,rotations)
    finished=monotonic_ns()
    return dict(selected=decoded['selected'],poses={v:observations[v].pose for v in VIEWS},
        world_observations={v:observations[v].world for v in VIEWS},
        full33_presence={v:observations[v].presence_score for v in VIEWS},
        images={v:captured[v][0].payload for v in VIEWS},
        stages={v:dict(times[v],decode_rotate=captured[v][1]/1e6,
                       full33_pair_wall=(finished-started)/1e6)for v in VIEWS},
        begin=decoded['begin'],inferred=finished,fetch_started=decoded['fetch_started'],
        decode_ready_ns=max(captured[v][2]for v in VIEWS),model_begin_ns=started,
        dpu_frames=model.frames,pose_frames=model.frames,pose_backend=BACKEND,
        tail4_model_stats=model.stats(),scoring_profile='full33_raw17_candidate_v1')


def infer_latest_pair(inputs,latest,rotations,estimator,model,decode_flag,decoder=None,**ignored):
    from posepriv_coach.field_preview_pipeline import decode_latest_pair
    decoded=decode_latest_pair(inputs,latest,rotations,decode_flag,decoder)
    return None if decoded is None else estimate_pair(decoded,model,rotations)


def map_display_work(work,cfg):
    if work is None:return None
    # Physical role names stay intact. Avatar-view choice is separate and explicit.
    work['physical_source_ids']={v:tuple(work['selected'][v][:2])for v in VIEWS}
    work['display_view_mapping']='device_names'
    return work


class WorldPointStabilizer:
    """Display-only 3D vector One Euro; never modifies confidence/raw score input."""
    def __init__(self,min_cutoff=5.,beta=30.,derivative_cutoff=1.):
        self.min_cutoff=min_cutoff;self.beta=beta;self.derivative_cutoff=derivative_cutoff
        self.states={}

    def apply(self,observation):
        s=observation.source;key=(s.view,s.width,s.height,s.rotation_degrees)
        old=self.states.get(key);points=observation.world_landmarks
        if not points:
            self.states.pop(key,None);return observation
        continuing=old is not None and 0<s.timestamp_ns-old[0]<=200_000_000
        dt=(s.timestamp_ns-old[0])*1e-9 if continuing else 0.
        ad=dt/(dt+1/(2*math.pi*self.derivative_cutoff))if continuing else 0.
        saved=[];result=[]
        for index,p in enumerate(points):
            raw=tuple(p[c]for c in ('x','y','z'))
            valid=min(p['visibility'],p['presence'])>=.5
            prior=old[1][index]if continuing else None
            if valid and prior is not None and prior[3]:
                velocity=tuple(d+ad*((x-r)/dt-d)for x,r,d in zip(raw,prior[0],prior[2]))
                cutoff=self.min_cutoff+self.beta*math.sqrt(sum(x*x for x in velocity))
                alpha=dt/(dt+1/(2*math.pi*cutoff))
                filtered=tuple(f+alpha*(x-f)for x,f in zip(raw,prior[1]))
            else:filtered=raw;velocity=(0.,0.,0.)
            saved.append((raw,filtered,velocity,valid))
            result.append(dict(p,**dict(zip(('x','y','z'),filtered))))
        self.states[key]=(s.timestamp_ns,saved)
        return WorldDisplayObservation(s,tuple(result),observation.normalized_landmarks)


class PreviewGeometryBuilder:
    """Original raw17 scorer, separate image smoothing, Full33 world rig/head."""
    def __init__(self,runtime,asset,rig_planner,scorer,*,stabilizer=None,head_stabilizer=None,config=None):
        self.runtime=runtime;self.asset=asset;self.scorer=scorer;self.stabilizer=stabilizer
        self.head_stabilizer=head_stabilizer;self.world_stabilizer=WorldPointStabilizer()if stabilizer else None
        self.config=config or {};self.view=self.config.get('tail4_avatar_view','front')
        scorer_views=ScorerViewAdapter(scorer,self.config.get('tail4_scoring_view_mapping'))
        self.scoring_view_metadata=scorer_views.metadata
        if scorer is not None and not scorer_views.is_identity:
            self.scorer=scorer_views
        if self.view not in VIEWS:raise ValueError('tail4_avatar_view is front or side')
        self.world=WorldAvatarPreview(asset,runtime.planner.config.bone_links,
            runtime.bone_topology_indices,vertex_requests=runtime.vertex_requests,
            full_body_basis=True)
        # Reuse the existing P4 native geometry leaves with this single-image
        # world's source-clock/body/head overrides, rather than dropping to the
        # old Python13-bone tail merely because the pose model changed.
        if rig_planner is not None:
            self.world.rig=type(self.world.rig)(asset,runtime.planner.config.bone_links,
                runtime.bone_topology_indices,full_body_basis=True,
                affine_composer=rig_planner.compose,bone_affine_builder=rig_planner.bone_affine,
                limb_axis_builder=rig_planner.limb_axis_builder,math_builder=rig_planner.math_builder,
                tail_library=self.config.get('preview_rig_tail_library'))
        self.repetition_count=0;self.completed_score=None
        from posepriv_coach.compact_coach import CompactCoach
        self.coach=CompactCoach()

    def prepare(self,work):
        raw=work['poses'];started=monotonic_ns()
        work['scoring_view_mapping']=self.scoring_view_metadata
        # Confidence remains min(actual Full33 presence, visibility); the old
        # rules thresholds are reused unchanged as an explicitly new profile.
        if self.scorer is not None:
            for view in VIEWS:
                begin=monotonic_ns();update=self.scorer.push(raw[view])
                work['stages'][view]['score']=(monotonic_ns()-begin)/1e6
                if update is not None:
                    self.coach.update(update)
                    self.repetition_count=update.repetition_count
                    if update.repetition_completed:self.completed_score=update.repetition_score
        scored=monotonic_ns()
        poses=({v:self.stabilizer.apply(raw[v])for v in VIEWS}if self.stabilizer else raw)
        pair=SyncedPosePair(poses['front'],poses['side'],abs(poses['front'].timestamp_ns-poses['side'].timestamp_ns))
        observation=work['world_observations'][self.view]
        if self.world_stabilizer:observation=self.world_stabilizer.apply(observation)
        rig=self.world.apply(observation,display_timestamp_ns=observation.source.timestamp_ns)
        if self.head_stabilizer:
            rig=self.head_stabilizer.apply(rig,observation.source.timestamp_ns,
                source_geometry=(self.view,observation.source.width,observation.source.height))
        plan=rig.render_plan;observed_core={name:bool(plan.matrix_valid[self.asset.bone_names.index(name)])
                                          for name in ('spine','neck')}
        result=dict(work=work,pair=pair,plan=plan,rig=rig,observed_core=observed_core,
                    repetition_count=self.repetition_count,completed_score=self.completed_score,coach=dict(self.coach.payload),
                    score_started_ns=started,scored_ns=scored,planned_ns=monotonic_ns())
        if self.stabilizer:result['display_poses']=poses
        return result
