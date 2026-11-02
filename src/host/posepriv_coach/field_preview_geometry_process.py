"""Optional single-owner CPU geometry process; images, mesh and devices stay parent-side.

Project GPL-2.0-only geometry/scoring logic, existing CPython API and NumPy.
Only our own socketpair carries pickle metadata. Close after the dispatcher joins.
"""
from dataclasses import dataclass
import math
import multiprocessing as mp
import os
import pickle
import select
import signal
import socket
import struct
from threading import Event, Lock
import time
from types import SimpleNamespace

from .avatar_pipeline import AvatarFramePlan
from .avatar_skinning import AffineMatrixQ1_14, MatrixWrite
from .contracts import CameraView, ExerciseScore, Keypoint, PoseFrame, SyncedPosePair
from .field_preview_geometry import (
    DISPLAY_SCORING,NEURAL_ONLY_SCORING,score_poses_for_work,validate_scoring_input_mode,
)
from .native_rig_math import rig_math_library
from .native_rig_tail import rig_tail_library

VERSION = 'posepriv.geometry.v1'
_MAX_MESSAGE = 65536
_HEADER = struct.Struct('<I')
_MATRICES = struct.Struct('<192h')
_VIEWS = {'front', 'side'}
_REASONS = {'tracked', 'partial', 'held_body', 'neutral', 'rejected'}


def geometry_process_enabled(cfg):
    enabled = cfg.get('preview_geometry_process', False)
    if type(enabled) is not bool:
        raise ValueError('preview_geometry_process must be boolean')
    if enabled and (cfg.get('hdmi_display') is not True or cfg.get('preview_inference_process') is not True):
        raise ValueError('preview_geometry_process requires HDMI and an inference process')
    if enabled and (type(cfg.get('preview_geometry_pipeline', False)) is not bool or cfg.get('preview_geometry_pipeline', False)):
        raise ValueError('geometry process and geometry thread are mutually exclusive')
    return enabled


def make_geometry_spec(runtime, asset, cfg, planner_flags):
    """Read only fixed skeleton/topology and explicit CPU library/rules paths."""
    backend=cfg.get('preview_pose_backend','movenet')
    if type(backend)is not str or backend not in ('movenet','rtmpose_m_cpu','rtmpose_m_remote'):
        raise ValueError('unsupported preview_pose_backend')
    scoring_enabled=backend=='movenet'
    image_tracking=cfg.get('preview_image_tracking',False)
    if type(image_tracking)is not bool:raise ValueError('preview_image_tracking must be boolean')
    if image_tracking and not scoring_enabled:
        raise ValueError('image tracking neural scoring requires movenet and its original rules')
    if image_tracking and not cfg.get('rules'):
        raise ValueError('image tracking neural scoring requires the original rules')
    head_cue_hold=cfg.get('preview_head_cue_hold',False)
    if type(head_cue_hold)is not bool:raise ValueError('preview_head_cue_hold must be boolean')
    head_current_axes=cfg.get('preview_head_current_axes',False)
    if type(head_current_axes)is not bool:raise ValueError('preview_head_current_axes must be boolean')
    finite_arm_reach=cfg.get('preview_finite_arm_reach',False)
    if type(finite_arm_reach)is not bool:raise ValueError('preview_finite_arm_reach must be boolean')
    leg_recovery=cfg.get('preview_leg_recovery',False)
    if type(leg_recovery)is not bool:raise ValueError('preview_leg_recovery must be boolean')
    native_limb = cfg.get('preview_native_limb_axis', False)
    if native_limb is not None and type(native_limb) is not bool:
        raise ValueError('preview_native_limb_axis must be boolean or None')
    if native_limb and not cfg.get('preview_bone_affine_library'):
        raise ValueError('native limb axis requires preview_bone_affine_library')
    spec=dict(bone_names=tuple(asset.bone_names), bind_lengths=tuple(asset.bone_bind_lengths_q1_14),
        inverse_binds=tuple(tuple(matrix.values) for matrix in asset.inverse_bind_matrices_q1_14),
        topology=tuple(runtime.bone_topology_indices), planner_flags=dict(planner_flags),
        affine_library=os.fspath(cfg['preview_affine_library']) if cfg.get('preview_affine_library') else None,
        bone_library=os.fspath(cfg['preview_bone_affine_library']) if cfg.get('preview_bone_affine_library') else None,
        rig_math_library=rig_math_library(cfg),
        rig_tail_library=rig_tail_library(cfg),
        native_limb=bool(native_limb), head_cue_hold=head_cue_hold, head_current_axes=head_current_axes,
        finite_arm_reach=finite_arm_reach,leg_recovery=leg_recovery,
        scoring_enabled=scoring_enabled,rules=os.fspath(cfg['rules'])if scoring_enabled else None)
    if image_tracking:spec['scoring_input_mode']=NEURAL_ONLY_SCORING
    stabilization=cfg.get('preview_display_stabilization',False)
    if type(stabilization)is not bool:raise ValueError('preview_display_stabilization must be boolean')
    if stabilization:spec['display_stabilization']=True
    head_stabilization=cfg.get('preview_head_world_stabilization',False)
    if type(head_stabilization)is not bool:raise ValueError('preview_head_world_stabilization must be boolean')
    if head_stabilization:
        if not stabilization:raise ValueError('preview_head_world_stabilization requires preview_display_stabilization')
        spec['head_world_stabilization']=True
    return spec


def _build_owner(spec):
    scoring_enabled=spec.get('scoring_enabled',True)
    if type(scoring_enabled)is not bool:raise ValueError('scoring_enabled must be boolean')
    mode=validate_scoring_input_mode(spec.get('scoring_input_mode',DISPLAY_SCORING))
    if mode==NEURAL_ONLY_SCORING and not scoring_enabled:
        raise ValueError('neural_only_v1 requires the original neural scorer and rules')
    from .field_preview_geometry import PreviewGeometryBuilder
    from .pose_avatar_preview import PoseAvatarPreviewPlanner
    from .preview_rigid_avatar import PreviewRigidAvatar
    asset = SimpleNamespace(bone_names=spec['bone_names'], bone_bind_lengths_q1_14=spec['bind_lengths'],
        inverse_bind_matrices_q1_14=tuple(AffineMatrixQ1_14(v) for v in spec['inverse_binds']))
    affine = bone = limb = rig_math = None
    if spec.get('affine_library'):
        from .native_affine import NativeAffineComposer
        affine = NativeAffineComposer(spec['affine_library'])
    if spec.get('bone_library'):
        from .native_bone_affine import NativeBoneAffineBuilder
        bone = NativeBoneAffineBuilder(spec['bone_library'])
    if spec.get('native_limb'):
        from .native_limb_axis import NativeLimbAxisBuilder
        limb = NativeLimbAxisBuilder(bone)
    if spec.get('rig_math_library'):
        from .native_rig_math import NativeRigMath
        rig_math = NativeRigMath(spec['rig_math_library'])
    planner = PoseAvatarPreviewPlanner(**spec['planner_flags'], affine_composer=affine, bone_affine_builder=bone)
    runtime = SimpleNamespace(planner=planner, vertex_requests=(), bone_topology_indices=spec['topology'])
    rig = PreviewRigidAvatar(asset, planner.config.bone_links, spec['topology'],
        affine_composer=affine, bone_affine_builder=bone, limb_axis_builder=limb,
        head_cue_hold=spec.get('head_cue_hold',False),head_current_axes=spec.get('head_current_axes',False),
        finite_arm_reach=spec.get('finite_arm_reach',False),leg_recovery=spec.get('leg_recovery',False),math_builder=rig_math,
        tail_library=spec.get('rig_tail_library'))
    scorer=None
    if scoring_enabled:
        from .session import load_squat_session_processor
        scorer=load_squat_session_processor(spec['rules'])
    from .field_preview_geometry import make_display_stabilizer,make_head_stabilizer
    stabilizer=make_display_stabilizer(dict(preview_display_stabilization=spec.get('display_stabilization',False)))
    head_stabilizer=make_head_stabilizer(dict(
        preview_head_world_stabilization=spec.get('head_world_stabilization',False),
        preview_display_stabilization=spec.get('display_stabilization',False)),asset)
    return PreviewGeometryBuilder(runtime, asset, rig, scorer,scoring_input_mode=mode,stabilizer=stabilizer,
        head_stabilizer=head_stabilizer)


def _stages(value):
    if type(value) is not tuple or len(value) != 2:
        raise ValueError('geometry stages require both views')
    result = {}
    for entry in value:
        if type(entry) is not tuple or len(entry) != 2:
            raise ValueError('invalid geometry stage entry')
        view, stage = entry
        if type(view) is not str or view not in _VIEWS or view in result or type(stage) is not dict:
            raise ValueError('invalid geometry stage view')
        if any(type(k) is not str or type(v) not in (int, float) or not math.isfinite(v) or v < 0 for k, v in stage.items()):
            raise ValueError('geometry stage milliseconds must be finite and non-negative')
        result[view] = dict(stage)
    return result


def _selected(value):
    if type(value) is not tuple or len(value) != 2:
        raise ValueError('geometry source identity requires both views')
    views = set()
    for entry in value:
        if (type(entry) is not tuple or len(entry) != 3 or type(entry[0]) is not str or entry[0] not in _VIEWS or
                entry[0] in views or any(type(v) is not int or v < 0 for v in entry[1:])):
            raise ValueError('invalid geometry source identity')
        views.add(entry[0])
    return value


def _pose_values(pose):
    return (pose.view.value,pose.frame_id,pose.timestamp_ns,pose.source_width,pose.source_height,
        tuple((name,p.x,p.y,p.confidence)for name,p in pose.keypoints.items()))


def _strict_wire_poses(values):
    """Reject malformed primitives/duplicates before constructing any pose."""
    if type(values)is not tuple or len(values)!=2:
        raise ValueError('neural geometry wire requires both pose views')
    poses={}
    for entry in values:
        if type(entry)is not tuple or len(entry)!=2:
            raise ValueError('invalid neural geometry pose entry')
        view,value=entry
        if type(view)is not str or view not in _VIEWS or view in poses or type(value)is not tuple or len(value)!=6:
            raise ValueError('invalid or duplicate neural geometry pose view')
        camera,frame,ns,width,height,points=value
        if (type(camera)is not str or camera!=view or
                any(type(v)is not int or v<0 for v in (frame,ns))or
                any(type(v)is not int or v<1 for v in (width,height))or type(points)is not tuple):
            raise ValueError('invalid neural geometry source identity or dimensions')
        keypoints={}
        for point in points:
            if type(point)is not tuple or len(point)!=4:
                raise ValueError('invalid neural geometry keypoint entry')
            name,x,y,confidence=point
            if (type(name)is not str or not name or name in keypoints or
                    any(type(v)not in (int,float)or not math.isfinite(v)for v in (x,y,confidence))or
                    not 0<=confidence<=1):
                raise ValueError('invalid or duplicate neural geometry keypoint')
            keypoints[name]=Keypoint(x,y,confidence)
        poses[view]=PoseFrame(CameraView(camera),frame,ns,keypoints,width,height)
    return poses


def _request_wire(work, generation,*,scoring_input_mode=DISPLAY_SCORING):
    mode=validate_scoring_input_mode(scoring_input_mode)
    score_poses=score_poses_for_work(work,mode)
    selected = _selected(tuple((v, r[0], r[1]) for v, r in work['selected'].items()))
    if set(work['poses']) != _VIEWS or set(work['stages']) != _VIEWS:
        raise ValueError('geometry work requires both views')
    poses = []
    identities = {v: (frame, ns) for v, frame, ns in selected}
    for view, pose in work['poses'].items():
        if (type(pose.frame_id) is not int or type(pose.timestamp_ns) is not int or
                pose.view is not CameraView(view) or (pose.frame_id, pose.timestamp_ns) != identities[view]):
            raise ValueError('geometry pose/source identity mismatch')
        poses.append((view,_pose_values(pose)))
    stages = tuple((view, dict(stage)) for view, stage in work['stages'].items())
    _stages(stages)
    request=dict(schema=VERSION, kind='prepare', generation=generation, selected=selected, poses=tuple(poses), stages=stages)
    if mode==NEURAL_ONLY_SCORING:
        request['display_pose_semantics']=work['display_pose_semantics']
        request['neural_score_poses']=None if score_poses is None else tuple((v,_pose_values(p))for v,p in score_poses.items())
    return request


def _request_work(request,*,scoring_input_mode=DISPLAY_SCORING):
    mode=validate_scoring_input_mode(scoring_input_mode)
    if mode==NEURAL_ONLY_SCORING:
        if 'neural_score_poses'not in request:
            raise ValueError('neural_only_v1 requires explicit neural_score_poses')
        poses=_strict_wire_poses(request['poses'])
    else:
        if 'neural_score_poses'in request or 'display_pose_semantics'in request:
            raise ValueError('image tracking score/display metadata requires neural_only_v1 scoring mode')
        poses = {}
        for view, value in request['poses']:
            camera, frame, ns, width, height, points = value
            poses[view] = PoseFrame(CameraView(camera), frame, ns,
                {name: Keypoint(x, y, confidence) for name, x, y, confidence in points}, width, height)
    work = dict(poses=poses, selected={v: (frame, ns, b'') for v, frame, ns in request['selected']},
        stages=_stages(request['stages']))
    identities = {v: (frame, ns) for v, frame, ns in _selected(request['selected'])}
    if (set(poses) != _VIEWS or any(pose.view is not CameraView(view) or
            (pose.frame_id, pose.timestamp_ns) != identities[view] for view, pose in poses.items())):
        raise ValueError('geometry pose/source identity mismatch')
    if mode==NEURAL_ONLY_SCORING:
        work['display_pose_semantics']=request.get('display_pose_semantics')
        raw=request['neural_score_poses']
        work['neural_score_poses']=None if raw is None else _strict_wire_poses(raw)
        score_poses_for_work(work,mode)
    return work


def _reply_wire(prepared, request):
    plan = prepared['plan']; rig = prepared['rig']
    reply = dict(schema=VERSION, kind='prepared', generation=request['generation'], selected=request['selected'],
        matrices_q14=_MATRICES.pack(*(v for write in plan.matrix_writes for v in write.matrix.values)),
        valid_mask=sum(int(v) << i for i, v in enumerate(plan.matrix_valid)),
        display_transform=tuple(rig.display_transform), reason=rig.reason, inferred_slots=rig.inferred_slots,
        observed_core=tuple(prepared['observed_core'].items()), repetition_count=prepared['repetition_count'],
        completed_score=prepared['completed_score'], coach=prepared.get('coach', {}), stages=tuple((v, dict(s)) for v, s in prepared['work']['stages'].items()),
        **{name: prepared[name] for name in ('score_started_ns', 'scored_ns', 'planned_ns')})
    if 'display_poses' in prepared:
        reply['display_poses'] = tuple((view, _pose_values(pose)) for view, pose in prepared['display_poses'].items())
    return reply


def _expect(message, kind):
    if type(message) is not dict or message.get('schema') != VERSION:
        raise ValueError('invalid geometry protocol version')
    if message.get('kind') == 'error':
        raise RuntimeError(str(message.get('exception')) + ': ' + str(message.get('message')))
    if message.get('kind') != kind:
        raise ValueError('invalid geometry protocol message: expected ' + kind)


def _validate_ready(message,mode):
    _expect(message,'ready')
    if mode==DISPLAY_SCORING and 'scoring_input_mode'not in message:return
    actual=message.get('scoring_input_mode')
    if type(actual)is not str or actual!=mode:
        raise ValueError('geometry scoring_input_mode handshake mismatch')


def _validate_reply(reply, request):
    _expect(reply, 'prepared')
    if type(reply['generation']) is not int or reply['generation'] != request['generation'] or _selected(reply['selected']) != request['selected']:
        raise ValueError('geometry source generation mismatch')
    if type(reply['matrices_q14']) is not bytes or len(reply['matrices_q14']) != _MATRICES.size:
        raise ValueError('geometry matrices must contain 384 immutable bytes')
    if type(reply['valid_mask']) is not int or not 0 <= reply['valid_mask'] <= 65535:
        raise ValueError('invalid geometry matrix validity mask')
    display = reply['display_transform']
    if type(display) is not tuple or len(display) != 16 or any(type(v) is not float or not math.isfinite(v) for v in display):
        raise ValueError('geometry display transform requires 16 finite floats')
    if type(reply['reason']) is not str or reply['reason'] not in _REASONS:
        raise ValueError('invalid geometry rig reason')
    inferred = reply['inferred_slots']
    if type(inferred) is not tuple or any(type(v) is not int or not 0 <= v < 13 for v in inferred) or len(set(inferred)) != len(inferred):
        raise ValueError('invalid geometry inferred slots')
    core = reply['observed_core']
    if (type(core) is not tuple or len(core) != 2 or any(type(e) is not tuple or len(e) != 2 for e in core) or
            tuple(e[0] for e in core) != ('spine', 'neck') or any(type(e[1]) is not bool for e in core)):
        raise ValueError('invalid geometry observed core')
    if type(reply['repetition_count']) is not int or reply['repetition_count'] < 0:
        raise ValueError('invalid geometry repetition count')
    if reply['completed_score'] is not None and type(reply['completed_score']) is not ExerciseScore:
        raise ValueError('geometry score must be a complete ExerciseScore or None')
    if any('score' not in stage for stage in _stages(reply['stages']).values()):
        raise ValueError('geometry reply stages require score timing for both views')
    clocks = tuple(reply[name] for name in ('score_started_ns', 'scored_ns', 'planned_ns'))
    if any(type(v) is not int or v < 0 for v in clocks) or not clocks[0] <= clocks[1] <= clocks[2]:
        raise ValueError('invalid geometry preparation clocks')


@dataclass(frozen=True, slots=True)
class RenderExecutionPlan:
    """Narrow current-GLES/execute inputs, not a complete reconstructed avatar plan."""
    matrix_writes: tuple
    matrix_valid: tuple
    vertex_requests: tuple

    execute = AvatarFramePlan.execute
    matrix_bank = AvatarFramePlan.matrix_bank


def _restore_reply(reply, work, static_requests):
    values = _MATRICES.unpack(reply['matrices_q14']); mask = reply['valid_mask']; poses = work['poses']
    display_poses = None
    if 'display_poses' in reply:
        display_poses = _strict_wire_poses(reply['display_poses'])
        for view, display in display_poses.items():
            raw = poses[view]
            if ((display.view, display.frame_id, display.timestamp_ns, display.source_width, display.source_height) !=
                    (raw.view, raw.frame_id, raw.timestamp_ns, raw.source_width, raw.source_height) or
                    display.keypoints.keys() != raw.keypoints.keys() or
                    any(point.confidence != raw.keypoints[name].confidence for name, point in display.keypoints.items())):
                raise ValueError('geometry display pose source/dimensions/confidence mismatch')
    plan = RenderExecutionPlan(tuple(MatrixWrite(i, AffineMatrixQ1_14(values[12*i:12*i+12])) for i in range(16)),
        tuple(bool(mask & (1 << i)) for i in range(16)), static_requests)
    work['stages'] = _stages(reply['stages'])
    result = dict(work=work, pair=SyncedPosePair(poses['front'], poses['side'], abs(poses['front'].timestamp_ns-poses['side'].timestamp_ns)),
        plan=plan, rig=SimpleNamespace(reason=reply['reason'], inferred_slots=reply['inferred_slots'], display_transform=reply['display_transform']),
        observed_core=dict(reply['observed_core']), repetition_count=reply['repetition_count'], completed_score=reply['completed_score'], coach=reply.get('coach', {}),
        **{name: reply[name] for name in ('score_started_ns', 'scored_ns', 'planned_ns')})
    if display_poses is not None:
        result['display_poses'] = display_poses
    return result


class Cancelled(Exception):
    """The parent explicitly stopped startup or the single dispatcher."""


def _packet(value):
    payload = pickle.dumps(value, protocol=5)
    if len(payload) > _MAX_MESSAGE:
        raise ValueError('geometry metadata exceeds 64 KiB')
    return _HEADER.pack(len(payload)) + payload


class _FramedSocket:
    """Bounded nonblocking single-owner I/O; partial RX survives cleanup handoff."""
    def __init__(self, sock):
        self.sock = sock; sock.setblocking(False); self.rx = bytearray(); self.tx = None; self.sent_bytes = 0

    def _wait(self, deadline, *, writing=False, cancel=None, process=None):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('geometry RPC deadline')
        readers = [] if writing else [self.sock]
        if process is not None:
            readers.append(process.sentinel)
        ready, _, _ = select.select(readers, [self.sock] if writing else [], [], min(.025, remaining))
        if process is not None and process.sentinel in ready and (writing or not select.select([self.sock], [], [], 0)[0]):
            raise RuntimeError('geometry child exited during RPC')

    def send(self, value, deadline, **kwargs):
        self.send_packet(_packet(value), deadline, **kwargs)

    def send_packet(self, packet, deadline, *, cancel=None, process=None):
        if self.tx is not None:
            raise RuntimeError('geometry partial request cannot be replayed')
        self.tx = memoryview(packet); self.sent_bytes = 0
        while self.tx:
            self._wait(deadline, writing=True, cancel=cancel, process=process)
            try:
                sent = self.sock.send(self.tx)
            except BlockingIOError:
                continue
            if not sent:
                raise EOFError('geometry peer closed during send')
            self.sent_bytes += sent; self.tx = self.tx[sent:]
        self.tx = None

    def receive(self, deadline, *, cancel=None, process=None):
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if len(self.rx) >= _HEADER.size:
                size = _HEADER.unpack_from(self.rx)[0]
                if size > _MAX_MESSAGE:
                    raise ValueError('geometry message exceeds 64 KiB')
                if len(self.rx) >= _HEADER.size + size:
                    value = pickle.loads(self.rx[_HEADER.size:_HEADER.size+size])
                    del self.rx[:_HEADER.size+size]
                    return value
            self._wait(deadline, cancel=cancel, process=process)
            try:
                chunk = self.sock.recv(4096)
            except BlockingIOError:
                continue
            if not chunk:
                raise EOFError('geometry peer closed before complete message')
            self.rx.extend(chunk)


def _run_geometry(sock, spec):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    channel = _FramedSocket(sock); generation = 0
    try:
        owner = _build_owner(spec)
        mode=validate_scoring_input_mode(owner.scoring_input_mode)
        ready=dict(schema=VERSION,kind='ready')
        if mode!=DISPLAY_SCORING:ready['scoring_input_mode']=mode
        channel.send(ready, time.monotonic()+5)
        while True:
            try:
                request = channel.receive(time.monotonic()+.05)
            except TimeoutError:
                continue
            if type(request) is not dict or request.get('schema') != VERSION:
                raise ValueError('invalid geometry request version')
            if request.get('kind') == 'stop':
                channel.send(dict(schema=VERSION, kind='stopped', generation=generation), time.monotonic()+1)
                return
            _expect(request, 'prepare')
            if type(request['generation']) is not int or request['generation'] != generation+1:
                raise ValueError('out-of-order geometry generation')
            result = owner.prepare(_request_work(request,scoring_input_mode=mode)); generation = request['generation']
            channel.send(_reply_wire(result, request), time.monotonic()+5)
    except (BrokenPipeError, ConnectionResetError, EOFError):
        pass
    except BaseException as exc:
        try:
            channel.send(dict(schema=VERSION, kind='error', exception=type(exc).__name__, message=str(exc)[:2048]), time.monotonic()+.2)
        except BaseException:
            pass
        raise
    finally:
        sock.close()


class GeometryProcess:
    """One synchronous RPC at a time; cancel/join dispatcher before close."""
    def __init__(self, spec, stopped=None, *, _target=_run_geometry, start_timeout=10):
        self.scoring_input_mode=validate_scoring_input_mode(spec.get('scoring_input_mode',DISPLAY_SCORING))
        parent, child = socket.socketpair(); self._channel = _FramedSocket(parent)
        self.cancel = Event(); self._request_lock = Lock(); self._ready = False
        self.generation = 0; self.pending = False; self.closed = False; self.failed = False
        self.cancelled_on_close = False; self.close_result = None; self._error = None; self._completed = 0
        self._pending_request = None
        self.process = mp.get_context('spawn').Process(target=_target, args=(child, spec))
        try:
            self.process.start(); child.close()
            ready = self._channel.receive(time.monotonic()+start_timeout, process=self.process, cancel=stopped)
            _validate_ready(ready,self.scoring_input_mode); self._ready = True
        except Cancelled:
            self.cancelled_on_close = True
            self.close()
            raise
        except BaseException as exc:
            self._failure(exc)
            self.close()
            raise
        finally:
            child.close()

    def _failure(self, exc):
        self.failed = True
        if self._error is None:
            self._error = type(exc).__name__ + ': ' + str(exc)

    def prepare(self, work, static_requests, *, timeout=2):
        if not self._request_lock.acquire(False):
            raise RuntimeError('geometry dispatcher already has an active request')
        try:
            if self.cancel.is_set():
                self.cancelled_on_close = True
                raise Cancelled()
            if self.closed or self.failed or self.pending:
                raise RuntimeError('geometry owner unavailable; no implicit replay')
            request = _request_wire(work, self.generation+1,scoring_input_mode=self.scoring_input_mode); packet = _packet(request)
            self.generation += 1; self.pending = True; self._pending_request = request
            deadline = time.monotonic()+timeout
            try:
                self._channel.send_packet(packet, deadline, cancel=self.cancel, process=self.process)
                reply = self._channel.receive(deadline, cancel=self.cancel, process=self.process)
                self.pending = False
                _validate_reply(reply, request)
                result = _restore_reply(reply, work, static_requests)
                self._completed += 1; self._pending_request = None
                return result
            except Cancelled:
                self.cancelled_on_close = True
                if self._channel.tx is not None and self._channel.sent_bytes == 0:
                    # No byte crossed the boundary, so the child never accepted this generation.
                    self._channel.tx = None; self.pending = False; self._pending_request = None; self.generation -= 1
                raise
            except BaseException as exc:
                self._failure(exc)
                raise
        finally:
            self._request_lock.release()

    def close(self, *, timeout=1):
        if self.closed:
            return self.close_result
        self.closed = True; self.cancelled_on_close |= self.cancel.is_set(); self.cancel.clear()
        deadline = time.monotonic()+timeout
        result = self.close_result = dict(graceful=False, error=self._error, cleanup_error=None, forced_actions=[],
            abandoned_pending=self.pending, cancelled=self.cancelled_on_close, exitcode=None)
        try:
            if self.process.pid is None:
                return result
            if self._channel.tx is not None:
                raise RuntimeError('geometry partial request cancelled; cannot replay')
            if not self._ready:
                ready = self._channel.receive(deadline, process=self.process)
                _validate_ready(ready,self.scoring_input_mode); self._ready = True
            if self.pending:
                reply = self._channel.receive(deadline, process=self.process); self.pending = False
                _validate_reply(reply, self._pending_request); self._pending_request = None
            self._channel.send(dict(schema=VERSION, kind='stop'), deadline, process=self.process)
            reply = self._channel.receive(deadline, process=self.process); _expect(reply, 'stopped')
            if type(reply.get('generation')) is not int or reply['generation'] != self.generation:
                raise ValueError('geometry stopped generation mismatch')
            result['graceful'] = True
        except BaseException as exc:
            result['cleanup_error'] = type(exc).__name__ + ': ' + str(exc)
            if self._error is None:
                self._failure(exc); result['error'] = self._error
        finally:
            try:
                self._channel.sock.close()
                if self.process.pid is not None:
                    self.process.join(max(0, deadline-time.monotonic()))
                    if self.process.is_alive():
                        result['forced_actions'].append('terminate_owned_geometry_child'); self.process.terminate(); self.process.join(.25)
                    if self.process.is_alive():
                        result['forced_actions'].append('kill_owned_geometry_child'); self.process.kill(); self.process.join(.25)
                    result['exitcode'] = self.process.exitcode
                    if result['exitcode'] != 0 and result['error'] is None:
                        self._failure(RuntimeError('geometry child exited '+str(result['exitcode']))); result['error'] = self._error
            except BaseException as exc:
                if result['cleanup_error'] is None:
                    result['cleanup_error'] = type(exc).__name__ + ': ' + str(exc)
                if self._error is None:
                    self._failure(exc); result['error'] = self._error
        return result

    def stats(self):
        if not self.closed and self.process.exitcode is not None:
            self._failure(RuntimeError('geometry child exited before close: '+str(self.process.exitcode)))
        return dict(owner='process', generation=self.generation, completed_pairs=self._completed, pending=int(self.pending),
            failed=self.failed, cancelled_on_close=self.cancelled_on_close, closed=self.closed,
            error=self._error, close_result=self.close_result)
