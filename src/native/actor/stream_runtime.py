# SPDX-License-Identifier: GPL-2.0-only
"""Streaming model actor factory/HDMI glue, independent of stable pair runtime."""
from pathlib import Path
import sys
import cv2

HERE=Path(__file__).resolve().parent
sys.path[:0]=[str(HERE.parent/'preview'),
              str(HERE.parent/'frontend'),
              str(HERE.parent/'adapter'),
              str(HERE.parent/'stream')]
from preview_runtime import AsyncDetector,PreviewGeometryBuilder,map_display_work,pose_backend
from live_frontend import PoseDetector,alignment_roi,crop_rgb,Q7_RGB_LUT
from preview_adapter import PreparedImage,decode
from stream_actor import StreamingPoseActor


class StreamingPreviewModel:
    def __init__(self,cfg,*,transport=None,detector=None,cropper=None,decoder=None):
        self.cfg=cfg;self.actor=None;self.rotations=None
        if detector is None:
            from ai_edge_litert.interpreter import Interpreter,OpResolverType
            resolver=(OpResolverType.BUILTIN_WITHOUT_DEFAULT_DELEGATES
                      if cfg.get('tail4_detector_resolver','default')=='builtin' else OpResolverType.BUILTIN)
            detector=PoseDetector(interpreter=Interpreter(model_path=cfg['tail4_detector_model'],
                num_threads=cfg.get('tail4_detector_threads',2),experimental_op_resolver_type=resolver))
        self.detector=AsyncDetector(detector,max_roi_age_ns=cfg.get('tail4_detector_max_roi_age_ns',1_000_000_000))
        if transport is None:
            from stream_client import StreamCombinedWorker
            transport=StreamCombinedWorker(cfg['tail4_worker_command'],capacity=cfg.get('tail4_stream_capacity',4),
                timeout=cfg.get('tail4_worker_timeout',5))
        self.transport=transport
        if cropper is None and cfg.get('preview_roi_crop') is True:
            sys.path.insert(0,str(HERE.parent/'crop'))
            from roi_crop import NativeRoiCropper
            cropper=NativeRoiCropper(cfg['tail4_roi_crop_library'],cfg['preview_roi_dma_fd'],
                timeout_ms=cfg.get('tail4_roi_crop_timeout_ms',200))
        elif cropper is None and cfg.get('tail4_crop_library'):
            sys.path.insert(0,str(HERE.parent/'crop'))
            from fast_crop import NativeCropper
            cropper=NativeCropper(cfg['tail4_crop_library'])
        self.cropper=cropper
        if decoder is None and cfg.get('tail4_decode_library'):
            sys.path.insert(0,str(HERE.parent/'decode'))
            from native_decoder import NativeDecoder
            decoder=NativeDecoder(cfg['tail4_decode_library']).decode
        self.decoder=decoder or decode

    def _prepare(self,source,bgr,state,epoch):
        if state.rect is None:
            job=self.detector.jobs.get(source.view)
            detector_source=None if job is None else job[0]
            detections=self.detector.take(source,epoch)
            if detections:
                rect=alignment_roi(detections[0].keypoints4,source.width,source.height)
                if min(rect[2:4])>0:state.rect=rect;state.prior_source=detector_source
            if state.rect is None:
                if source.view not in self.detector.jobs:
                    self.detector.submit(source,cv2.cvtColor(bgr,cv2.COLOR_BGR2RGB),epoch)
                return None
        if self.cropper is not None:
            pixels,roi=self.cropper.crop(bgr,state.rect)
        else:
            # BGR-first retains frozen sampling, avoiding whole-source RGB work.
            sampled,roi=crop_rgb(bgr,state.rect,256,keep_aspect=False,border_mode=cv2.BORDER_REPLICATE)
            pixels=cv2.LUT(cv2.cvtColor(sampled,cv2.COLOR_BGR2RGB),Q7_RGB_LUT).tobytes()
        return PreparedImage(source,roi,pixels)

    def offer(self,decoded,rotations):
        if self.actor is None:
            self.rotations=dict(rotations)
            self.actor=StreamingPoseActor(self.transport,self._prepare,self.decoder,
                capacity=self.cfg.get('tail4_stream_capacity',4),rotations=self.rotations)
            self.actor.start()
        elif dict(rotations)!=self.rotations:raise ValueError('restart stream before camera rotation changes')
        self.actor.offer(decoded,epoch=decoded.get('capture_epoch',0))

    def take(self,timeout=0):return None if self.actor is None else self.actor.take(timeout)

    @property
    def frames(self):return 0 if self.actor is None else self.actor.counts['completed_neural_sources']

    def stats(self):
        result=dict(frames=self.frames,detector=self.detector.stats(),
            stream_actor=None if self.actor is None else self.actor.stats(),
            online_compute='KV260 ARM+DPU+Tail4 PL',mode='cross_pair_capacity'+str(self.cfg.get('tail4_stream_capacity',4)))
        if self.cfg.get('preview_roi_crop') is True:result['roi_crop']=self.cropper.stats()
        return result

    def close(self):
        try:
            if self.actor is not None:self.actor.close(timeout=self.cfg.get('tail4_worker_timeout',5)+2)
            else:self.transport.close()
        finally:self.detector.close()

    def cancel(self):
        if self.actor is not None:self.actor.cancel()
        else:self.transport.cancel()


def make_stronger_model(cfg):return StreamingPreviewModel(cfg)


def infer_latest_pair(inputs,latest,rotations,estimator,model,decode_flag,decoder=None,**ignored):
    from posepriv_coach.field_preview_pipeline import decode_latest_pair
    decoded=decode_latest_pair(inputs,latest,rotations,decode_flag,decoder)
    if decoded is not None:model.offer(decoded,rotations)
    return model.take(timeout=0)
