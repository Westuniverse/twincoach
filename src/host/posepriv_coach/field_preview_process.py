"""Isolated decoder/model owner, bounded shared results, no GPU/DMA in this process."""
from contextlib import ExitStack
import multiprocessing as mp
import os
from pathlib import Path
import pickle
import queue
import signal
import sys
import struct
import time
from .field_preview_shared import SharedResultChannel


WORK_HEADER=struct.Struct('<4sI')


def inference_metadata(work):
    value=dict(work)
    value['selected']={v:(r[0],r[1],b'')for v,r in work['selected'].items()}
    value['poses']={v:dict(view=p.view.value,frame_id=p.frame_id,timestamp_ns=p.timestamp_ns,
        source_width=p.source_width,source_height=p.source_height,
        keypoints={name:(point.x,point.y,point.confidence)for name,point in p.keypoints.items()})
        for v,p in work['poses'].items()}
    if 'neural_score_poses'in work:
        from .field_preview_geometry import score_poses_for_work,NEURAL_ONLY_SCORING
        raw=score_poses_for_work(work,NEURAL_ONLY_SCORING)
        value['neural_score_poses']=None if raw is None else {
            v:dict(view=p.view.value,frame_id=p.frame_id,timestamp_ns=p.timestamp_ns,
                source_width=p.source_width,source_height=p.source_height,
                keypoints={name:(point.x,point.y,point.confidence)for name,point in p.keypoints.items()})
            for v,p in raw.items()}
    return value


def inference_parts(work):
    value=inference_metadata(work)
    images={};buffers=[]
    for view,image in work['images'].items():
        if isinstance(image,bytes):
            data=memoryview(image);images[view]=dict(kind='bytes',length=len(image))
        else:
            import numpy as np
            array=np.ascontiguousarray(image);data=memoryview(array).cast('B')
            images[view]=dict(kind='array',length=data.nbytes,shape=array.shape,dtype=array.dtype.str)
        buffers.append(data)
    value['images']=images;metadata=pickle.dumps(value,protocol=5)
    return (WORK_HEADER.pack(b'PPI1',len(metadata)),metadata,*buffers)


def pack_inference(work):
    return b''.join(inference_parts(work))


def unpack_inference(data,*,zero_copy=False):
    from .contracts import PoseFrame,CameraView,Keypoint
    magic,size=WORK_HEADER.unpack_from(data)
    if magic not in(b'PPI1',b'PPI2'):raise ValueError('Invalid private inference result version')
    offset=WORK_HEADER.size+size
    work=pickle.loads(data[WORK_HEADER.size:offset])  # Own-process metadata only.
    work['poses']={v:PoseFrame(CameraView(p['view']),p['frame_id'],p['timestamp_ns'],
        {name:Keypoint(*point)for name,point in p['keypoints'].items()},p['source_width'],p['source_height'])
        for v,p in work['poses'].items()}
    if work.get('neural_score_poses')is not None:
        work['neural_score_poses']={v:PoseFrame(CameraView(p['view']),p['frame_id'],p['timestamp_ns'],
            {name:Keypoint(*point)for name,point in p['keypoints'].items()},p['source_width'],p['source_height'])
            for v,p in work['neural_score_poses'].items()}
    images={};maximum_end=offset
    for view,detail in work['images'].items():
        image_offset=detail.get('offset',offset);end=image_offset+detail['length'];maximum_end=max(maximum_end,end)
        packet=memoryview(data)[image_offset:end];offset=end
        if detail['kind']=='bytes':images[view]=bytes(packet)
        else:
            import numpy as np
            image=np.frombuffer(packet,dtype=detail['dtype']).reshape(detail['shape'])
            if zero_copy:image.flags.writeable=False
            else:image=image.copy()
            images[view]=image
    if (maximum_end if magic==b'PPI2'else offset)!=len(data):raise ValueError('Invalid private inference result size')
    work['images']=images
    return work


def _copy_images_into_lease(work,lease):
    """Prefetch owns its arrays; acquire an arena writer only after inference."""
    import numpy as np
    images={}
    for view,image in work['images'].items():
        started=time.monotonic_ns();destination=lease.image(view,image.shape)
        np.copyto(destination,image);images[view]=destination
        work['stages'][view]['decoded_arena_copy']=(time.monotonic_ns()-started)/1e6
    work['images']=images


def _pipeline_options(cfg):
    prefetch=cfg.get('preview_decode_prefetch',False);demand=cfg.get('preview_demand_pipeline',False)
    if type(prefetch)is not bool:raise ValueError('preview_decode_prefetch must be boolean')
    if type(demand)is not bool:raise ValueError('preview_demand_pipeline must be boolean')
    if demand and not prefetch:raise ValueError('preview_demand_pipeline requires preview_decode_prefetch')
    return prefetch,demand


def model_ready_overlap_enabled(cfg):
    """Permit one model call beside READY, keeping the decoded-work credit."""
    enabled=cfg.get('preview_model_ready_overlap',False)
    if type(enabled)is not bool:raise ValueError('preview_model_ready_overlap must be boolean')
    if enabled:
        required=('preview_inference_process','preview_decode_prefetch','preview_demand_pipeline',
                  'preview_decoded_arena','preview_paired_requests')
        if any(cfg.get(key)is not True for key in required) or cfg.get('preview_pose_backend','movenet')!='movenet':
            raise ValueError('preview_model_ready_overlap requires the paired MoveNet inference process with decode prefetch, demand and decoded arena enabled')
    return enabled


def _pending_result_count(output):
    if not isinstance(output,SharedResultChannel):return output.pending_count()
    # The producer's _reading is process-local. A locked JPEG slot has already
    # been borrowed by the renderer, which may overlap the next model request.
    # Only this producer writes slots, and it is not writing during this probe.
    pending=0
    for slot in output.slots:
        if slot._ready.is_set()and slot._lock.acquire(False):
            try:pending+=int(slot._ready.is_set())
            finally:slot._lock.release()
    return pending


def _run_inference(cfg,inputs,rotations,external_stop,stop,output,final,done,counts,lock,native_pid):
    # The foreground parent owns Ctrl+C and coordinates normal child shutdown.
    signal.signal(signal.SIGINT,signal.SIG_IGN)
    def interrupted(*_):raise KeyboardInterrupt('Inference owner terminated')
    signal.signal(signal.SIGTERM,interrupted)
    model=None;error=None;runners=[];decode_worker=None
    try:
        sys.path.insert(0,cfg['python_path'])
        os.environ.update(LD_LIBRARY_PATH='/opt/vitis-ai-compat/lib:/lib/aarch64-linux-gnu:/lib',
            XLNX_VART_FIRMWARE=cfg['xclbin'],XLNX_DISABLE_LOAD_XCLBIN='0')
        import cv2
        from .movenet_cnn_worker import load_movenet_cnn_worker
        from .pipeline import MoveNetPoseEstimator
        from .field_stronger_backend import pose_backend,make_stronger_model
        from .field_image_tracking import image_tracking_enabled,PreviewImageTrackingModel
        from .field_preview import LatestPair
        from .field_preview_pipeline import (LatestInferenceWorker,infer_latest_pair,
            decode_latest_pair,estimate_decoded_pair)
        cv2.setNumThreads(1)
        flag={1:cv2.IMREAD_COLOR,2:cv2.IMREAD_REDUCED_COLOR_2,4:cv2.IMREAD_REDUCED_COLOR_4}[cfg.get('preview_decode_scale',1)]
        prefetch,demand=_pipeline_options(cfg)
        model_overlap=model_ready_overlap_enabled(cfg)
        backend=pose_backend(cfg)
        image_tracking=image_tracking_enabled(cfg)
        with ExitStack()as resources:
            if backend=='movenet':
                model=load_movenet_cnn_worker(*(cfg[k]for k in ('worker','model','mapping','post_model','post_mapping')),
                    on_process_started=lambda pid:setattr(native_pid,'value',pid),
                    **{k:cfg['preview_'+k]for k in ('native_post_library','native_post_contract')if cfg.get('preview_'+k)is not None})
                if image_tracking:model=PreviewImageTrackingModel(model,cfg)
                estimator=MoveNetPoseEstimator(model)
            else:model=make_stronger_model(cfg);estimator=None
            runners.append(model);resources.callback(model.close)
            paired=cfg.get('preview_paired_requests',False)and backend=='movenet'
            if paired:model.start()
            latest=LatestPair(max_skew_ns=50_000_000)if backend!='movenet'else LatestPair();decoder=None
            extra=dict(pose_backend=backend)if backend!='movenet'else{}
            if image_tracking:extra['image_tracking']=True
            if cfg.get('preview_parallel_decode',False)or paired or prefetch:
                from concurrent.futures import ThreadPoolExecutor
                decoder=ThreadPoolExecutor(max_workers=2,thread_name_prefix='posepriv-decode')
                resources.callback(decoder.shutdown,wait=True,cancel_futures=True)
            writer=decoder
            if prefetch:
                if paired:
                    # Next-pair decode must not delay the current native writer.
                    writer=ThreadPoolExecutor(max_workers=1,thread_name_prefix='posepriv-model-write')
                    resources.callback(writer.shutdown,wait=True,cancel_futures=True)
                decode_worker=LatestInferenceWorker(
                    lambda:decode_latest_pair(inputs,latest,rotations,flag,decoder),stop,
                    name='posepriv-decode-prefetch',block_when_ready=demand)
                resources.callback(decode_worker.close)
            def next_work(decode_target=None):
                if decode_worker is None:
                    return infer_latest_pair(inputs,latest,rotations,estimator,model,flag,decoder,
                        paired_requests=paired,decode_target=decode_target,**extra)
                decoded=decode_worker.take()
                if decoded is None:return None
                work=estimate_decoded_pair(decoded,estimator,model,writer,
                    paired_requests=paired,discard_stale=True,**(dict(extra,rotations=rotations)if extra else{}))
                if work is not None:work['decode_prefetch']=decode_worker.stats()
                return work
            while not stop.is_set()and not external_stop.is_set():
                if demand and not model_overlap and _pending_result_count(output)>0:
                    stop.wait(.002);continue
                if cfg.get('preview_decoded_arena',False):
                    work=next_work()if prefetch else None
                    if prefetch and work is None:continue
                    with output.writing()as lease:
                        if prefetch:_copy_images_into_lease(work,lease)
                        else:work=next_work(lease.image)
                        if work is None:continue
                        with lock:counts[0]+=1;counts[3]=work['dpu_frames'];sequence=counts[0]
                        if stop.is_set()or external_stop.is_set():
                            with lock:counts[2]+=1
                            break
                        metadata=inference_metadata(work);metadata['images']=lease.images;encoded=pickle.dumps(metadata,protocol=5)
                        packet=WORK_HEADER.pack(b'PPI2',len(encoded))+encoded
                        lost=output.publish_lease(lease,sequence,max(r[1]for r in work['selected'].values()),packet)
                        with lock:counts[1]+=lost
                    continue
                work=next_work()
                if work is None:continue
                with lock:
                    counts[0]+=1;counts[3]=work['dpu_frames'];sequence=counts[0]
                if stop.is_set()or external_stop.is_set():
                    with lock:counts[2]+=1
                    break
                parts=inference_parts(work)
                lost=output.publish_parts(sequence,max(r[1]for r in work['selected'].values()),parts)
                with lock:counts[1]+=lost
    except BaseException as exc:error=f'{type(exc).__name__}: {exc}'
    finally:
        with lock:counts[3]=sum(r.stats()['frames']for r in runners)if cfg.get('preview_pose_backend','movenet')=='movenet'else 0
        result=dict(error=error)
        if model is not None:result['model_stats']=model.stats()
        if decode_worker is not None:result['decode_prefetch']=decode_worker.stats()
        final.put(result);done.set()


class InferenceProcess:
    def __init__(self,cfg,inputs,rotations,stopped,*,_target=_run_inference):
        _pipeline_options(cfg)
        self._model_ready_overlap=model_ready_overlap_enabled(cfg)
        ctx=mp.get_context('spawn');self.cfg=cfg;self._external_stop=stopped;self._inputs=inputs;self._rotations=rotations
        self.stop=ctx.Event();self.done=ctx.Event()
        if cfg.get('preview_decoded_arena',False):
            from .field_preview_images import SharedImageArena
            self.output=SharedImageArena(ctx)
        else:self.output=SharedResultChannel(ctx)
        self.final=ctx.Queue(1)
        self.counts=ctx.RawArray('Q',4);self.lock=ctx.Lock();self.native_pid=ctx.RawValue('Q',0)
        self._final=None;self._closed=False;self._reported=False;self._lease=None
        self._counts_snapshot=(0,0,0,0);self._diagnostic_error=None
        self.process=ctx.Process(target=_target,args=(cfg,inputs,rotations,stopped,self.stop,self.output,
            self.final,self.done,self.counts,self.lock,self.native_pid))
        self.process.start()

    def _read_final(self,timeout):
        if self._final is None:
            try:self._final=self.final.get(timeout=timeout)
            except queue.Empty:return
        if self._final.get('error')and not self._reported:
            self._reported=True;raise RuntimeError(self._final['error'])

    def take(self,timeout=.05):
        self.release()
        if self.process.exitcode is not None:
            self._read_final(.1)
            if self.process.exitcode!=0 or self._final is None:
                raise RuntimeError(f'Inference owner exited {self.process.exitcode} without successful completion')
        elif self.done.is_set():self._read_final(.1)
        try:
            context=self.output.borrowed(timeout=timeout);_,_,data=context.__enter__();self._lease=context
            work=unpack_inference(data,zero_copy=True)
        except queue.Empty:
            if not self.process.is_alive():
                self._read_final(.1)
                if not self.stop.is_set():raise RuntimeError(f'Inference owner exited {self.process.exitcode}')
            return None
        if self.done.is_set():self._read_final(.1)
        return work

    def release(self):
        if self._lease is not None:
            context=self._lease;self._lease=None;context.__exit__(None,None,None)

    def stats(self):
        abnormal=self.process.exitcode not in(None,0)
        pending=0 if self._closed else None
        if not self._closed and not abnormal:
            if self.lock.acquire(timeout=.05):
                try:self._counts_snapshot=tuple(self.counts)
                finally:self.lock.release()
            else:self._diagnostic_error='Inference counters lock unavailable'
            try:pending=self.output.pending_count(timeout=.05)
            except TimeoutError as exc:self._diagnostic_error=str(exc)
        elif abnormal:self._diagnostic_error=self._diagnostic_error or f'Inference owner exited {self.process.exitcode}'
        completed,superseded,discarded,dpu=self._counts_snapshot
        return dict(completed_pairs=completed,superseded_results=superseded,discarded_on_close=discarded,
            dpu_frames=dpu,pending=pending,reading=int(self._lease is not None),
            final_model_stats=None if self._final is None else self._final.get('model_stats'),
            closed=self._closed,runners=1,paired_requests=bool(self.cfg.get('preview_paired_requests',False))and self.cfg.get('preview_pose_backend','movenet')=='movenet',
            decode_prefetch=bool(self.cfg.get('preview_decode_prefetch',False)),
            demand_pipeline=bool(self.cfg.get('preview_demand_pipeline',False)),
            model_ready_overlap=self._model_ready_overlap,
            decode_prefetch_final=None if self._final is None else self._final.get('decode_prefetch'),
            failed=bool(self._final and self._final.get('error'))or abnormal or self._diagnostic_error is not None,
            diagnostics_incomplete=self._diagnostic_error is not None,diagnostic_error=self._diagnostic_error,owner='process')

    def _cleanup_native(self):
        self._cleanup_native_pid(self.native_pid.value)

    def _cleanup_native_pid(self,pid):
        if not pid:return
        try:
            args=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            if args[:2]!=[os.fsencode(self.cfg['worker']),os.fsencode(self.cfg['model'])]or os.getpgid(pid)!=pid:return
            os.killpg(pid,signal.SIGTERM)
            deadline=time.monotonic()+.5
            while time.monotonic()<deadline and Path(f'/proc/{pid}').exists():time.sleep(.02)
            if Path(f'/proc/{pid}').exists():os.killpg(pid,signal.SIGKILL)
        except(ProcessLookupError,FileNotFoundError):pass

    def close(self):
        if self._closed:return
        deadline=time.monotonic()+10;pending_error=None;forced=False
        try:
            try:self.release()
            except TimeoutError as exc:pending_error=exc;self._diagnostic_error=str(exc)
            if self.process.is_alive():self.stop.set()
            while self._final is None and time.monotonic()<deadline:
                try:self._read_final(min(.1,max(0,deadline-time.monotonic())))
                except BaseException as exc:pending_error=exc;break
                if not self.process.is_alive():break
            self.process.join(timeout=max(0,deadline-time.monotonic()))
            if self.process.is_alive():forced=True;self.process.terminate();self.process.join(timeout=2)
            if self.process.is_alive():self.process.kill();self.process.join(timeout=1)
            self._cleanup_native()
            # Do not touch locks left behind by a crashed or forcibly stopped owner.
            if forced or self._final is None or self.process.exitcode!=0:
                reason=f'Inference owner exited {self.process.exitcode}'if self.process.exitcode!=0 else'Inference owner did not complete'
                self._diagnostic_error=self._diagnostic_error or reason
                if pending_error is None:pending_error=RuntimeError(reason)
            else:
                try:
                    discarded=self.output.discard_pending(timeout=.05)
                    if not self.lock.acquire(timeout=.05):raise TimeoutError('Inference counters lock unavailable')
                    try:
                        self.counts[2]+=discarded;self._counts_snapshot=tuple(self.counts)
                    finally:self.lock.release()
                except TimeoutError as exc:
                    self._diagnostic_error=str(exc)
                    if pending_error is None:pending_error=exc
        finally:
            self._closed=True;self.output.close();self.final.cancel_join_thread();self.final.close()
        if pending_error is not None:raise pending_error
