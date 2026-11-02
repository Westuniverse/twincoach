"""GPL-2.0-only. Explicit independent current-pixel display core, no scoring."""
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Sequence
from dataclasses import dataclass,replace
from pathlib import Path
from threading import Lock,get_ident
from types import MappingProxyType
from time import monotonic_ns,thread_time_ns

from posepriv_coach.capture import CapturedFrame
from posepriv_coach.pose_adapter import MOVENET_COCO_KEYPOINTS as NAMES,decode_movenet_singlepose
from posepriv_coach.relative_object import _prove_bgr_storage

@dataclass(frozen=True)
class RuntimeFrame:
    epoch:str
    captured:CapturedFrame

@dataclass(frozen=True)
class SourceSnapshot:
    epoch:str
    physical_view:str
    model_view:str
    frame_id:int
    timestamp_ns:int
    sync_timestamp_ns:int
    width:int
    height:int

@dataclass(frozen=True)
class DisplayPoint:
    x:float|None
    y:float|None
    display_weight:int
    provenance:str
    anchor_source:SourceSnapshot|None
    inferred:bool=True

@dataclass(frozen=True)
class DisplayPose:
    source:SourceSnapshot
    points:object
    all_inferred:bool=True
    new_cnn_observation:bool=False

@dataclass(frozen=True)
class PairResult:
    sources:tuple
    raw_cnn_poses:tuple|None
    raw_cnn_inferences:tuple|None
    display_poses:tuple
    cnn_performed:bool
    cnn_reason:str
    tracking_diagnostics:tuple
    role_timing_ns:tuple=()

def decode_original(inference,frame):
    return decode_movenet_singlepose(inference.output,frame,resize_transform=inference.transform)

class _TrackingDiagnostics(Sequence):
    """Compact per-image measurements; materialize joint dictionaries on request."""
    _FAILURES=(None,'forward_status','forward_outside','backward_status',
               'backward_outside','experimental_patch_limit',
               'experimental_displacement_limit','experimental_FB_limit')
    def __init__(self,previous,source,anchors,eligible,track):
        self.previous=previous;self.source=source;self.anchors=anchors
        self.eligible=eligible;self.track=track;self.cnn=None;self.provenance=None
    def __len__(self):return 17
    def __getitem__(self,index):
        if isinstance(index,slice):return tuple(self[i]for i in range(*index.indices(17)))
        if index<0:index+=17
        if not 0<=index<17:raise IndexError(index)
        record=dict(joint_name=NAMES[index],previous_image_source=self.previous,
                    current_image_source=self.source,prior_anchor_source=self.anchors[index],
                    forward_status=None,forward_point_px=None,forward_patch_error=None,
                    backward_status=None,backward_point_px=None,backward_patch_error=None,
                    FB_error_px=None,displacement_px=None,accepted_current_LK=False,
                    failure='no_prior_point')
        try:k=self.eligible.index(index)
        except ValueError:k=None
        if k is not None:
            track=self.track;reason=int(track.reason[k]);status=int(track.forward_status[k])
            record.update(forward_status=status,failure=self._FAILURES[reason],
                          accepted_current_LK=bool(track.accepted[k]))
            if status==1:
                record.update(forward_point_px=tuple(map(float,track.points[k,0])),
                              forward_patch_error=float(track.forward_patch[k]),
                              displacement_px=float(track.displacement[k]))
            if reason not in (1,2):
                status=int(track.backward_status[k]);record['backward_status']=status
                if status==1:
                    record.update(backward_point_px=tuple(map(float,track.backward_points[k,0])),
                                  backward_patch_error=float(track.backward_patch[k]),
                                  FB_error_px=float(track.fb_error[k]))
        if self.cnn is not None:
            point=self.cnn.keypoints[NAMES[index]]
            record.update(current_CNN_confidence=point.confidence,
                          current_CNN_pixel=(point.x*self.source.width,point.y*self.source.height))
        if self.provenance is not None:record['display_provenance']=self.provenance[index]
        return record

class _Role:
    def __init__(self,physical,np,cv,flow_factory):
        self.physical=physical;self.np=np;self.cv=cv;self.factory=flow_factory
        self.owner=None;self.flow=None;self.last=None;self.points=[None]*17;self.anchors=[None]*17;self.pending=None;self.closed=False
        self.last_timing_ns={}
        self.counts=dict(images=0,seeds=0,forward_directions=0,backward_directions=0,forward_points=0,accepted_LK_points=0,current_CNN_anchors=0,resets=0)
    def _owner(self):
        if self.owner is None:self.owner=get_ident()
        if self.owner!=get_ident():raise RuntimeError('Physical flow changed owner thread')
    def _drop(self):
        if self.flow is not None:
            flow,self.flow=self.flow,None;flow.close()
        self.last=None;self.points=[None]*17;self.anchors=[None]*17;self.pending=None
    def advance(self,source,frame,reset_reason):
        wall=monotonic_ns();cpu=thread_time_ns()
        self._owner()
        if self.closed or self.pending is not None:raise RuntimeError('Role closed or pending transaction')
        if reset_reason is not None:
            self._drop();self.counts['resets']+=1
        if self.flow is None:self.flow=self.factory(source.width,source.height)
        gray=self.cv.cvtColor(frame.payload,self.cv.COLOR_BGR2GRAY)
        # Source ordering and geometry are bound once by process_pair; the native
        # ABI owns gray/point storage validation and all LK output qualification.
        np=self.np;eligible=tuple(i for i,p in enumerate(self.points)if p is not None)
        candidates=[None]*17;track=None
        if self.last is None or not eligible:
            self.flow.seed(gray);self.counts['seeds']+=1
        else:
            initial=np.array([self.points[i]for i in eligible],np.float32).reshape(-1,1,2)
            track=self.flow.track_current(gray,initial)
            self.counts['forward_directions']+=1;self.counts['forward_points']+=len(eligible)
            reasons=np.bincount(track.reason,minlength=8)
            if sum(reasons[3:])+reasons[0]:self.counts['backward_directions']+=1
            accepted=np.flatnonzero(track.accepted)
            self.counts['accepted_LK_points']+=len(accepted)
            for k in accepted:
                i=eligible[k];point=track.points[k,0]
                candidates[i]=((float(point[0]),float(point[1])),self.anchors[i])
        records=_TrackingDiagnostics(self.last,source,tuple(self.anchors),eligible if track is not None else (),track)
        self.last=source;self.counts['images']+=1;self.pending=(source,candidates,records)
        self.last_timing_ns=dict(current_image_advance_wall=monotonic_ns()-wall,
                                 current_image_advance_thread_cpu=thread_time_ns()-cpu)
        return records
    def finalize(self,source,cnn_pose):
        wall=monotonic_ns();cpu=thread_time_ns()
        self._owner()
        if self.pending is None or self.pending[0]!=source or self.last!=source:raise RuntimeError('Final anchor/current image transaction mismatch')
        _,candidates,records=self.pending;points=[];provenances=[];np=self.np
        if cnn_pose is not None:
            cnn_points=tuple(cnn_pose.keypoints[name]for name in NAMES)
            pixels=np.array([(p.x*source.width,p.y*source.height)for p in cnn_points],np.float32)
            confidence=np.fromiter((p.confidence for p in cnn_points),np.float64,count=17)
            cnn_valid=(confidence>=.5)&np.isfinite(pixels).all(axis=1)&(pixels[:,0]>=0)&(pixels[:,0]<source.width)&(pixels[:,1]>=0)&(pixels[:,1]<source.height)
            records.cnn=cnn_pose
        for i in range(17):
            chosen=None;provenance='invalid';anchor=None
            if cnn_pose is not None and cnn_valid[i]:
                pixel=pixels[i];chosen=(float(pixel[0]),float(pixel[1]));anchor=source
                provenance='current_CNN_anchor_display';self.counts['current_CNN_anchors']+=1
            if chosen is None and candidates[i]is not None:chosen,anchor=candidates[i];provenance='current_image_LK_display'
            self.points[i]=chosen;self.anchors[i]=anchor;provenances.append(provenance)
            points.append(DisplayPoint(None if chosen is None else chosen[0]/source.width,None if chosen is None else chosen[1]/source.height,0 if chosen is None else 1,provenance,anchor))
        records.provenance=tuple(provenances);self.pending=None
        self.last_timing_ns.update(current_anchor_merge_wall=monotonic_ns()-wall,
                                   current_anchor_merge_thread_cpu=thread_time_ns()-cpu)
        return DisplayPose(source,MappingProxyType(dict(zip(NAMES,points))))
    def close(self):
        self._owner()
        if not self.closed:
            try:self._drop()
            finally:self.closed=True

class CurrentImageTrackingCore:
    def __init__(self,base,*,native_library=None,physical_by_view=None,decoder=decode_original,flow_factory=None,numpy_module=None,cv2_module=None):
        import numpy as np
        if numpy_module is not None:np=numpy_module
        if cv2_module is None:import cv2 as cv2_module
        mapping={'front':'front','side':'side'}if physical_by_view is None else dict(physical_by_view)
        if set(mapping)!={'front','side'}or set(mapping.values())!={'front','side'}:raise ValueError('Frozen front/side physical permutation required')
        if hasattr(base,'trackers'):raise ValueError('Stateless original Lightning base required, not causal ROI wrapper')
        if flow_factory is None:
            if not isinstance(native_library,(str,Path)):raise ValueError('Explicit native body17 library required')
            from .native_body_tracking import Body17
            flow_factory=lambda width,height:Body17(native_library,width,height)
        self.base=base;self.decoder=decoder;self.np=np;self.mapping=MappingProxyType(mapping);self._lock=Lock();self.closed=False;self.failed=False;self.error=None;self.cleanup_errors=[]
        self._roles={v:_Role(v,np,cv2_module,flow_factory)for v in ('front','side')}
        self._executors={v:ThreadPoolExecutor(max_workers=1,thread_name_prefix='body17-'+v)for v in self._roles}
        self._last={};self._since_cnn=0;self.source_pairs=0;self.cnn_pairs=0;self.cnn_results=0;self._stopped_roles=False
    def _freeze(self,wrapped):
        if type(wrapped)is not RuntimeFrame or type(wrapped.epoch)is not str or not wrapped.epoch:raise ValueError('Explicit immutable RuntimeFrame(epoch,captured) required')
        f=wrapped.captured
        if not isinstance(f,CapturedFrame)or f.view.value not in self.mapping:raise ValueError('Explicit captured front/side required')
        if any(type(getattr(f,k))is not int for k in ('frame_id','timestamp_ns','sync_timestamp_ns','width','height'))or min(f.frame_id,f.timestamp_ns,f.sync_timestamp_ns)<0 or not(1<=f.width<=4096 and 1<=f.height<=2160):raise ValueError('Exact nonnegative integer source identity/dimensions required')
        if f.pixel_format!='bgr8'or type(f.payload)is not self.np.ndarray or f.payload.dtype!=self.np.uint8 or f.payload.shape!=(f.height,f.width,3):raise ValueError('Exact current BGR8 source pixels required')
        _prove_bgr_storage(f.payload,self.np);pixels=f.payload.copy(order='C');pixels.flags.writeable=False
        frame=replace(f,payload=pixels);source=SourceSnapshot(wrapped.epoch,self.mapping[f.view.value],f.view.value,f.frame_id,f.timestamp_ns,f.sync_timestamp_ns,f.width,f.height)
        return source,frame
    def _abort_roles(self):
        if self._stopped_roles:return
        jobs=[self._executors[v].submit(role.close)for v,role in self._roles.items()]
        for job in jobs:
            try:job.result()
            except BaseException as exc:self.cleanup_errors.append(type(exc).__name__+': '+str(exc))
        self._stopped_roles=True
    def process_pair(self,frames,executor):
        if not self._lock.acquire(False):raise RuntimeError('process_pair/close cannot reenter')
        futures=[];started=False
        try:
            if self.closed or self.failed:raise RuntimeError('Core closed or failed; no fallback')
            frames=tuple(frames)
            if len(frames)!=2:raise ValueError('Exactly one front and one side source required')
            frozen=tuple(self._freeze(f)for f in frames)
            if {s.model_view for s,_ in frozen}!={'front','side'}:raise ValueError('Pair must contain distinct front and side')
            if (abs(frozen[0][0].timestamp_ns-frozen[1][0].timestamp_ns)>80_000_000 or
                    abs(frozen[0][0].sync_timestamp_ns-frozen[1][0].sync_timestamp_ns)>80_000_000):
                raise ValueError('Current source pair exceeds original80ms synchronization limit')
            resets={}
            for source,frame in frozen:
                old=self._last.get(source.physical_view);reason=None
                if old is None:reason='startup'
                elif source.epoch!=old.epoch:reason='explicit_epoch_changed'
                elif (source.frame_id<=old.frame_id or source.timestamp_ns<=old.timestamp_ns or
                      source.sync_timestamp_ns<=old.sync_timestamp_ns):raise ValueError('Same-epoch frame ID and source clocks must strictly increase')
                elif (source.width,source.height)!=(old.width,old.height):reason='dimensions_changed'
                elif max(source.timestamp_ns-old.timestamp_ns,source.sync_timestamp_ns-old.sync_timestamp_ns)>200_000_000:reason='source_gap'
                resets[source.physical_view]=reason
            cnn=any(resets.values())or self._since_cnn>=1
            reason='reset_or_startup'if any(resets.values())else'fixed_every_two_new_pairs'if cnn else'current_image_tracking_only'
            started=True
            for source,frame in frozen:futures.append(self._executors[source.physical_view].submit(self._roles[source.physical_view].advance,source,frame,resets[source.physical_view]))
            inferences=raw=None
            if cnn:
                self.cnn_pairs+=1
                inferences=tuple(self.base.infer_pair(tuple(f for _,f in frozen),executor))
                if len(inferences)!=2:raise RuntimeError('Actual CNN result count differs')
                raw=tuple(self.decoder(inference,frame)for inference,(_,frame)in zip(inferences,frozen))
                for p,(s,f)in zip(raw,frozen):
                    if (p.view.value,p.frame_id,p.timestamp_ns,p.source_width,p.source_height)!=(f.view.value,f.frame_id,f.sync_timestamp_ns,f.width,f.height)or set(p.keypoints)!=set(NAMES):raise RuntimeError('Raw CNN decoder/source identity differs')
                self.cnn_results+=2
            diagnostics=tuple(job.result()for job in futures)
            final=[self._executors[s.physical_view].submit(self._roles[s.physical_view].finalize,s,None if raw is None else raw[i])for i,(s,_)in enumerate(frozen)]
            displays=tuple(job.result()for job in final)
            self._last={s.physical_view:s for s,_ in frozen};self._since_cnn=0 if cnn else self._since_cnn+1;self.source_pairs+=1
            timings=tuple(dict(self._roles[s.physical_view].last_timing_ns)for s,_ in frozen)
            return PairResult(tuple(s for s,_ in frozen),raw,inferences,displays,cnn,reason,diagnostics,timings)
        except BaseException as exc:
            if started:
                for job in futures:
                    try:job.result()
                    except BaseException:pass
                self.failed=True;self.error=type(exc).__name__+': '+str(exc);self._abort_roles()
            raise
        finally:self._lock.release()
    def stats(self):
        if not self._lock.acquire(False):raise RuntimeError('Stats require quiescent core')
        try:return dict(base=self.base.stats(),source_pairs_completed=self.source_pairs,source_images_completed=2*self.source_pairs,CNN_pairs_requested=self.cnn_pairs,CNN_pose_results_returned=self.cnn_results,physical_by_view=dict(self.mapping),role_counts={v:dict(r.counts)for v,r in self._roles.items()},closed=self.closed,failed=self.failed,error=self.error,cleanup_errors=list(self.cleanup_errors),scope='Independent display experiment; raw CNN only scoring eligibility. No score/FPS/human accuracy claim.')
        finally:self._lock.release()
    def close(self):
        if not self._lock.acquire(False):raise RuntimeError('Close requires no active pair')
        try:
            if self.closed:return
            self._abort_roles()
            for executor in self._executors.values():executor.shutdown(wait=True)
            try:self.base.close()
            finally:self.closed=True
            if self.cleanup_errors:raise RuntimeError('Owner close errors: '+repr(self.cleanup_errors))
        finally:self._lock.release()
