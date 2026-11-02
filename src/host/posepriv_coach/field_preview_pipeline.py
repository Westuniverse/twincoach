"""One pending inference result; model thread and render thread have separate owners."""
from threading import Condition, Event, Thread


class LatestInferenceWorker:
    """Run bounded work until stopped; take atomically transfers a whole result.

    Work must respect its existing bounded I/O. close joins before callers release
    the model; LivePreview's outer process supervisor retains its shutdown timeout.
    """
    def __init__(self, work, stopped,*,name='posepriv-inference',block_when_ready=False):
        self._work=work;self._external_stop=stopped;self._stop=Event()
        self._block_when_ready=block_when_ready
        self._changed=Condition();self._ready=None;self._error=None;self._reported_error=False
        self._completed=0;self._superseded=0;self._discarded=0;self._closed=False
        self._thread=Thread(target=self._run,name=name,daemon=True)
        self._thread.start()

    def _run(self):
        try:
            while not self._stop.is_set()and not self._external_stop.is_set():
                if self._block_when_ready:
                    with self._changed:
                        while self._ready is not None and not self._stop.is_set()and not self._external_stop.is_set():
                            self._changed.wait(.05)
                        if self._stop.is_set()or self._external_stop.is_set():break
                item=self._work()
                if item is None:
                    self._stop.wait(.001);continue
                with self._changed:
                    self._completed+=1
                    if self._stop.is_set()or self._external_stop.is_set():self._discarded+=1
                    else:
                        if self._ready is not None:self._superseded+=1
                        self._ready=item
                    self._changed.notify_all()
        except BaseException as exc:
            with self._changed:
                self._error=exc;self._changed.notify_all()

    def take(self, timeout=.05):
        with self._changed:
            self._changed.wait_for(lambda:self._ready is not None or self._error is not None or
                self._stop.is_set()or self._external_stop.is_set(),timeout=timeout)
            if self._error is not None:
                self._reported_error=True;raise self._error
            item=self._ready;self._ready=None
            if self._block_when_ready:self._changed.notify_all()
            return item

    def stats(self):
        with self._changed:
            return dict(completed_pairs=self._completed,superseded_results=self._superseded,
                discarded_on_close=self._discarded,pending=int(self._ready is not None),
                closed=self._closed,failed=self._error is not None,block_when_ready=self._block_when_ready)

    def close(self):
        self._stop.set()
        with self._changed:self._changed.notify_all()
        self._thread.join()
        with self._changed:
            if self._ready is not None:self._discarded+=1
            self._ready=None;self._closed=True;self._changed.notify_all()
            if self._error is not None and not self._reported_error:
                self._reported_error=True;raise self._error


def decode_preview_frame(view, record, rotation, decode_flag, target=None):
    """Independent decode work owns its image; frame identity stays on that image."""
    from time import monotonic_ns
    import cv2
    import numpy as np
    from .capture import CapturedFrame
    from .contracts import CameraView
    frame_id,timestamp,jpeg=record;started=monotonic_ns()
    image=cv2.imdecode(np.frombuffer(jpeg,np.uint8),decode_flag)
    if image is None:raise ValueError('Preview JPEG decode failed')
    code={0:None,90:cv2.ROTATE_90_CLOCKWISE,180:cv2.ROTATE_180,270:cv2.ROTATE_90_COUNTERCLOCKWISE}[rotation]
    if target is not None:
        shape=(image.shape[1],image.shape[0],3)if rotation in(90,270)else image.shape
        destination=target(view,shape)
        if code is None:np.copyto(destination,image);image=destination
        else:
            image=cv2.rotate(image,code,dst=destination)
            if image is not destination:raise RuntimeError('Rotation did not use shared decoded destination')
    elif code is not None:image=cv2.rotate(image,code)
    image=np.ascontiguousarray(image)
    captured=CapturedFrame(view=CameraView(view),frame_id=frame_id,timestamp_ns=timestamp,
        sync_timestamp_ns=timestamp,width=image.shape[1],height=image.shape[0],pixel_format='bgr8',payload=image)
    finished=monotonic_ns()
    return captured,finished-started,finished


def select_latest_pair(inputs,latest):
    """Only the caller owns LatestPair; selection and its timestamps stay whole."""
    import time,queue
    fetch_started=time.monotonic_ns();updated=False
    for view,q in inputs.items():
        try:record=q.get(timeout=.01)
        except queue.Empty:continue
        latest.put(view,record);updated=True
    selected=latest.take()if updated else None
    if selected is None:return None
    if time.monotonic_ns()-min(r[1]for r in selected.values())>500_000_000:return None
    selection=dict(selected=selected,begin=time.monotonic_ns(),fetch_started=fetch_started)
    if latest.capture_epoch is not None:selection['capture_epoch']=latest.capture_epoch
    return selection


def decode_selected_pair(selection,rotations,decode_flag,decoder=None,*,decode_target=None,defer=False):
    """Decode into owned arrays unless an explicit synchronous target is given.

    defer retains the serial infer/decode ordering of the existing default path.
    Prefetch always materializes a complete pair before handing it to the model.
    """
    selected=selection['selected']
    futures={}if decoder is None else {view:decoder.submit(decode_preview_frame,view,record,rotations[view],decode_flag,decode_target)
        for view,record in selected.items()}
    def captured():
        for view,record in selected.items():
            yield view,(decode_preview_frame(view,record,rotations[view],decode_flag,decode_target)
                if decoder is None else futures[view].result())
    return dict(selection,captured=captured()if defer else dict(captured()))


def decode_latest_pair(inputs,latest,rotations,decode_flag,decoder):
    selection=select_latest_pair(inputs,latest)
    return None if selection is None else decode_selected_pair(selection,rotations,decode_flag,decoder)


def estimate_decoded_pair(decoded,estimator,model,executor=None,*,paired_requests=False,discard_stale=False,pose_backend='movenet',rotations=None,image_tracking=False):
    """The caller alone owns the estimator/model; decoded images are read-only input."""
    import time
    if type(image_tracking)is not bool:
        raise ValueError('current image tracking flag must be boolean')
    selected=decoded['selected']
    if discard_stale and time.monotonic_ns()-min(r[1]for r in selected.values())>500_000_000:return None
    if image_tracking:
        if not paired_requests or pose_backend!='movenet':
            raise ValueError('current image tracking requires explicit paired MoveNet ownership')
        from .field_image_tracking import estimate_image_tracked_pair
        return estimate_image_tracked_pair(decoded,model,executor)
    if pose_backend!='movenet':
        from .field_stronger_backend import estimate_stronger_pair
        return estimate_stronger_pair(decoded,model,rotations,pose_backend)
    poses={};images={};inference_stages={}
    captured=decoded['captured'];work_clocks={}
    if paired_requests:
        from .pose_adapter import decode_movenet_singlepose
        pair_begin=time.monotonic_ns()
        work_clocks={'decode_ready_ns':max(row[2]for row in captured.values()),'model_begin_ns':pair_begin}
        results=model.infer_pair([r[0]for r in captured.values()],executor);pair_elapsed=time.monotonic_ns()-pair_begin
        for (view,(frame,decode_ns,decode_finished)),result,timing in zip(captured.items(),results,model.last_pair_timing_ns,strict=True):
            images[view]=frame.payload
            poses[view]=decode_movenet_singlepose(result.output,frame,resize_transform=result.transform)
            inference_stages[view]=dict(decode_rotate=decode_ns/1e6,paired_request_wall=pair_elapsed/1e6,
                **{k:v/1e6 for k,v in timing.items()})
        dpu_frames=model.stats()['frames']
    else:
        items=captured.items()if isinstance(captured,dict)else captured
        for view,(frame,decode_ns,decode_finished)in items:
            images[view]=frame.payload;ready=time.monotonic_ns()
            poses[view]=estimator.infer(frame)
            estimated=time.monotonic_ns()
            inference_stages[view]=dict(decode_rotate=decode_ns/1e6,decode_wait=(ready-decode_finished)/1e6,
                estimate=(estimated-ready)/1e6,
                **{k:v/1e6 for k,v in model.last_timing_ns.items()})
        dpu_frames=model.stats()['frames']
    return dict(dpu_frames=dpu_frames,selected=selected,poses=poses,images=images,stages=inference_stages,
        begin=decoded['begin'],inferred=time.monotonic_ns(),fetch_started=decoded['fetch_started'],**work_clocks)


def infer_latest_pair(inputs, latest, rotations, estimator, model, decode_flag, decoder=None,*,paired_requests=False,decode_target=None,pose_backend='movenet',image_tracking=False):
    selection=select_latest_pair(inputs,latest)
    if selection is None:return None
    decoded=decode_selected_pair(selection,rotations,decode_flag,decoder,
        decode_target=decode_target,defer=not paired_requests and pose_backend=='movenet')
    return estimate_decoded_pair(decoded,estimator,model,decoder,paired_requests=paired_requests,
        pose_backend=pose_backend,rotations=rotations,image_tracking=image_tracking)
