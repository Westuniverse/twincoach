# SPDX-License-Identifier: GPL-2.0-only
"""Full33 world metadata extension of the existing fixed-asset GeometryProcess."""
from pathlib import Path
import signal
import sys
import time

HERE=Path(__file__).resolve().parent
sys.path[:0]=[str(HERE.parent/'tail4_stream_process_20261030'),
              str(HERE.parent/'tail4_hdmi_candidate_20261030')]
import posepriv_coach.field_preview_geometry_process as base
from posepriv_coach.field_preview_geometry import DISPLAY_SCORING
from preview_runtime import PreviewGeometryBuilder
from stream_process import primitive_world,reconstruct_world

Cancelled=base.Cancelled


def make_geometry_spec(runtime,asset,cfg,planner_flags):
    # Reuse the existing fixed topology/native-library/rules extraction. Only
    # its historical backend selector is normalized for this explicit profile.
    compatible=dict(cfg,preview_pose_backend='movenet'if cfg.get('tail4_scoring_enabled',False)else'rtmpose_m_cpu',
                    preview_image_tracking=False)
    spec=base.make_geometry_spec(runtime,asset,compatible,planner_flags)
    spec['scoring_enabled']=bool(cfg.get('tail4_scoring_enabled',False))
    spec['rules']=cfg.get('rules')if spec['scoring_enabled']else None
    spec['full33cfg']={k:cfg[k]for k in ('tail4_avatar_view','preview_rig_tail_library')if k in cfg}
    spec['scoring_input_mode']=DISPLAY_SCORING
    return spec


def build_full33_owner(spec):
    original=base._build_owner(spec)
    owner=PreviewGeometryBuilder(original.runtime,original.asset,original.rig_planner,original.scorer,
        stabilizer=original.stabilizer,head_stabilizer=original.head_stabilizer,config=spec['full33cfg'])
    owner.scoring_input_mode=DISPLAY_SCORING
    return owner


def request_wire(work,generation):
    request=base._request_wire(work,generation,scoring_input_mode=DISPLAY_SCORING)
    request['full33_world_records']=primitive_world(work)['full33_world_records']
    for name in ('stream_sources','capture_epoch','scoring_profile'):
        if name in work:request[name]=work[name]
    return request


def request_work(request):
    work=base._request_work(request,scoring_input_mode=DISPLAY_SCORING)
    work['full33_world_records']=request['full33_world_records']
    for name in ('stream_sources','capture_epoch','scoring_profile'):
        if name in request:work[name]=request[name]
    work=reconstruct_world(work)
    # The existing reply contract requires a timing value for disabled scores.
    for stage in work['stages'].values():stage.setdefault('score',0.)
    return work


def run_geometry(sock,spec):
    signal.signal(signal.SIGINT,signal.SIG_IGN)
    channel=base._FramedSocket(sock);generation=0
    try:
        owner=build_full33_owner(spec)
        channel.send(dict(schema=base.VERSION,kind='ready'),time.monotonic()+5)
        while True:
            try:request=channel.receive(time.monotonic()+.05)
            except TimeoutError:continue
            if type(request)is not dict or request.get('schema')!=base.VERSION:
                raise ValueError('invalid Full33 geometry request version')
            if request.get('kind')=='stop':
                channel.send(dict(schema=base.VERSION,kind='stopped',generation=generation),time.monotonic()+1)
                return
            base._expect(request,'prepare')
            if type(request['generation'])is not int or request['generation']!=generation+1:
                raise ValueError('out-of-order Full33 geometry generation')
            result=owner.prepare(request_work(request));generation=request['generation']
            reply=base._reply_wire(result,request)
            # Neutral identity includes integer constants; keep the existing
            # wire's float ABI and finite checks for every display state.
            reply['display_transform']=tuple(float(x)for x in reply['display_transform'])
            channel.send(reply,time.monotonic()+5)
    except(BrokenPipeError,ConnectionResetError,EOFError):pass
    except BaseException as exc:
        try:channel.send(dict(schema=base.VERSION,kind='error',exception=type(exc).__name__,message=str(exc)[:2048]),time.monotonic()+.2)
        except BaseException:pass
        raise
    finally:sock.close()


class GeometryProcess(base.GeometryProcess):
    def __init__(self,spec,stopped=None,*,_target=run_geometry,start_timeout=10):
        super().__init__(spec,stopped=stopped,_target=_target,start_timeout=start_timeout)

    def prepare(self,work,static_requests,*,timeout=2):
        # Same single RPC/partial-send/cancellation supervision as the existing
        # class; only request_wire adds validated primitive world metadata.
        if not self._request_lock.acquire(False):raise RuntimeError('geometry dispatcher already has an active request')
        try:
            if self.cancel.is_set():self.cancelled_on_close=True;raise Cancelled()
            if self.closed or self.failed or self.pending:raise RuntimeError('geometry owner unavailable; no implicit replay')
            request=request_wire(work,self.generation+1);packet=base._packet(request)
            self.generation+=1;self.pending=True;self._pending_request=request
            deadline=time.monotonic()+timeout
            try:
                self._channel.send_packet(packet,deadline,cancel=self.cancel,process=self.process)
                reply=self._channel.receive(deadline,cancel=self.cancel,process=self.process)
                self.pending=False;base._validate_reply(reply,request)
                result=base._restore_reply(reply,work,static_requests)
                self._completed+=1;self._pending_request=None
                return result
            except Cancelled:
                self.cancelled_on_close=True
                if self._channel.tx is not None and self._channel.sent_bytes==0:
                    self._channel.tx=None;self.pending=False;self._pending_request=None;self.generation-=1
                raise
            except BaseException as exc:self._failure(exc);raise
        finally:self._request_lock.release()

    def stats(self):
        result=super().stats();result.update(profile='full33_world_geometry_process',images_sent=False)
        return result
