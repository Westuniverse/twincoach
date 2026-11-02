# SPDX-License-Identifier: GPL-2.0-only
"""Small per-view extensions of the existing actor/factory; no image/lease clone."""
from dataclasses import dataclass
import json
from pathlib import Path
from time import monotonic_ns
from types import SimpleNamespace
import cv2
import numpy as np
from stream_actor import StreamingPoseActor
from stream_runtime import StreamingPreviewModel
from preview_adapter import TailHeads,identity_for
from posepriv_coach.capture import CapturedFrame
from posepriv_coach.contracts import CameraView
from posepriv_coach.movenet_cnn_worker import _Q7_RGB_LUT
from posepriv_coach.movenet_preprocess import OpenCVMoveNetPreprocessor
from posepriv_coach.pose_adapter import decode_movenet_singlepose
from posepriv_coach.world_avatar_preview import WorldDisplayObservation

BACKEND='hybrid_full33_side_lightning_front'
PROFILE='hybrid_side_full33_front_lightning_raw17_v1'


def pose_backend(cfg):
    if cfg.get('preview_pose_backend')!=BACKEND or cfg.get('tail4_avatar_view')!='side':
        raise ValueError('explicit hybrid backend and physical side avatar required')
    return BACKEND


@dataclass(frozen=True)
class LightGeometry:
    transform:object
    rect:None=None


@dataclass(frozen=True)
class FrontObservation:
    source:object
    pose:object
    world:object
    presence_score:None=None
    def next_roi(self):return None


class HybridActor(StreamingPoseActor):
    def __init__(self,*args,light_decode,**kwargs):
        super().__init__(*args,**kwargs);self.light_decode=light_decode
        self.counts.update(completed_full33_side_sources=0,completed_lightning_front_sources=0)

    def _prepare_one(self,pair,view):
        ticket=super()._prepare_one(pair,view)
        if view=='side'and ticket.result is not None:
            self.transport.finish_pair_without_side(identity_for(ticket.source))
        return ticket

    def _receive(self):
        progress=False
        while True:
            response=self.transport.take(timeout=0)
            if response is None:break
            identity,heads=response;ticket=self.inflight.pop(tuple(identity),None)
            if ticket is None:raise ValueError('hybrid completion has no current owned source')
            raw=heads['raw']
            if ticket.source.view=='side':
                if heads['model_kind']!=1 or len(raw)!=160370:raise ValueError('side must be true Full33 raw')
                ticket.result=self.decode(ticket.source,ticket.roi,TailHeads(tuple(identity),raw[:390],raw[390:624],raw[624:626]),raw[626:])
                self.counts['completed_full33_side_sources']+=1
            else:
                if heads['model_kind']!=2 or len(raw)!=198144:raise ValueError('front must be true Lightning raw')
                ticket.result=self.light_decode(ticket,raw);self.counts['completed_lightning_front_sources']+=1
            if ticket.result.source!=ticket.source:raise ValueError('hybrid source identity changed')
            ticket.decoded_ns=monotonic_ns();self.counts['completed_neural_sources']+=1;progress=True
        return progress

    def _work(self,pair):
        work=super()._work(pair);work.update(pose_backend=BACKEND,scoring_profile=PROFILE,
            model_by_physical_view=dict(front='Lightning',side='Full33+Tail35'))
        return work


class HybridPreviewModel(StreamingPreviewModel):
    def __init__(self,cfg,*,transport=None,**kwargs):
        pose_backend(cfg)
        if transport is None:
            from hybrid_transport import HybridTransport
            transport=HybridTransport(cfg,capacity=cfg.get('tail4_stream_capacity',3),timeout=cfg.get('tail4_worker_timeout',5))
        super().__init__(cfg,transport=transport,**kwargs)
        self.front_pre=OpenCVMoveNetPreprocessor(cv2_module=cv2,resize_before_color=True)
        self.front_lut=np.asarray(_Q7_RGB_LUT,np.int8);self.front_lut.flags.writeable=False;self.front_post=None

    def _prepare(self,source,bgr,state,epoch):
        if source.view=='side':return super()._prepare(source,bgr,state,epoch)
        frame=CapturedFrame(CameraView.FRONT,source.frame_id,source.timestamp_ns,source.width,source.height,'bgr8',bgr)
        prepared=self.front_pre.prepare(frame)
        return SimpleNamespace(source=source,roi=LightGeometry(prepared.transform),input_q7=cv2.LUT(prepared.pixels,self.front_lut).tobytes())

    def _light_decode(self,ticket,raw):
        values={};offset=0
        for role,size in zip(('center','heatmap','offset','regression'),(2304,39168,78336,78336)):
            values[role]=raw[offset:offset+size];offset+=size
        points=self.front_post(values)
        frame=ticket.pair.decoded['captured']['front'][0]
        pose=decode_movenet_singlepose(points,frame,resize_transform=ticket.roi.transform)
        return FrontObservation(ticket.source,pose,WorldDisplayObservation(ticket.source,(),()))

    def offer(self,decoded,rotations):
        if self.actor is None:
            self.transport.start()
            from ai_edge_litert.interpreter import Interpreter,OpResolverType
            from posepriv_coach.native_movenet_post import NativeRawPost
            old=json.loads(Path(self.cfg['hybrid_lightning_config']).read_text())
            self.front_post=NativeRawPost(old['preview_native_post_library'],old['post_model'],
                json.loads(Path(old['post_mapping']).read_text()),self.transport.worker.metadata['lightning_metadata'],
                json.loads(Path(old['mapping']).read_text()),old['preview_native_post_contract'],Interpreter,OpResolverType)
            self.rotations=dict(rotations);self.actor=HybridActor(self.transport,self._prepare,self.decoder,
                light_decode=self._light_decode,capacity=self.cfg.get('tail4_stream_capacity',3),rotations=self.rotations)
            self.actor.start()
        elif dict(rotations)!=self.rotations:raise ValueError('hybrid source rotation changed')
        self.actor.offer(decoded,epoch=decoded.get('capture_epoch',0))

    def close(self):
        try:
            if self.actor is not None:self.actor.close(timeout=self.cfg.get('tail4_worker_timeout',5)+2)
        finally:
            try:self.transport.close()
            finally:self.detector.close()

    def stats(self):
        value=super().stats();value.update(mode='single_pair_RPC_and_bounded_next_prepare',
            pose_backend=BACKEND,scoring_profile=PROFILE,scoring_qualified=False,
            actual_models=self.transport.stats())
        return value
