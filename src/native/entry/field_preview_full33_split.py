# SPDX-License-Identifier: GPL-2.0-only
# Private candidate derived from project field_preview.py; live/public default untouched.
"""Optional latest-only hardware preview; raw MJPEG recording owns the cameras."""
from __future__ import annotations
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import signal
import sys
from threading import Condition, Lock, Thread
from .field_preview_shared import SharedJpegSlot
import time

PAGE = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>PosePriv 现场拍摄</title>
<style>body{background:#0d121c;color:#e9eef8;font:18px sans-serif;margin:16px}img{width:100%;max-width:1280px}#s{white-space:pre-wrap;padding:12px} .bad{color:#ffb8a8}</style>
<h2>PosePriv · 当次拍摄实时预览</h2><div id="s">正在等待板端预览；原始录像请查看录制终端。</div><img id="v" src="/stream.mjpg">
<script>let busy=false,activeSession=null,streamFailed=false;const video=document.getElementById('v');
video.onerror=()=>{streamFailed=true;}; async function tick(){if(busy)return;busy=true;try{let x=await(await fetch('/status.json',{cache:'no-store'})).json();
let s=document.getElementById('s');s.className=x.status!=='running'||x.source_age_ms>1000?'bad':'';
s.textContent='状态：'+x.status+'　原始录像独立保存，预览允许跳帧\n'+(x.error||'')+'　当前源图像年龄：'+(x.source_age_ms??'--')+' ms\n'+(x.frames||0)+' 对已处理；生成均速 '+(x.fps||0).toFixed(2)+' 对/秒；源帧 '+(x.source_ids||[]).join(' / ');
if(!x.hdmi_display&&x.status==='running'&&(streamFailed||activeSession!==x.session_id)){
activeSession=x.session_id;streamFailed=false;video.src='/stream.mjpg?session='+encodeURIComponent(x.session_id);}
video.style.display=x.hdmi_display?'none':'';
document.getElementById('v').style.opacity=x.status==='running'&&x.source_age_ms<=1000?'1':'.3';
if(x.hdmi_display)s.textContent+='\n当前画面输出至板卡 HDMI。';
}catch(e){document.getElementById('s').textContent='预览连接已断开；检查录制终端是否完成。';document.getElementById('v').style.opacity='.3';}finally{busy=false;setTimeout(tick,200)}}tick();</script></html>'''


class LatestPair:
    """Keep physical source IDs; an explicit epoch separates cyclic captures."""
    def __init__(self, max_skew_ns=80_000_000):
        self.max_skew_ns=max_skew_ns
        self.latest={};self.used={};self.epochs={};self.capture_epoch=None

    def put(self, view, record):
        frame_id,timestamp,payload=record[:3]
        explicit=len(record)==4
        epoch=record[3]if explicit else None
        if (view not in ('front','side')or type(frame_id)is not int or type(timestamp)is not int
                or frame_id<0 or timestamp<=0 or len(record)not in(3,4)
                or(explicit and(type(epoch)is not int or epoch<0))):
            raise ValueError('Invalid preview identity')
        previous=self.latest.get(view);previous_epoch=self.epochs.get(view)
        if previous is not None and(timestamp<=previous[1]or
                (epoch==previous_epoch and frame_id<=previous[0])or
                (epoch!=previous_epoch and(previous_epoch is None or epoch is None or epoch<previous_epoch))):
            raise ValueError('Non-increasing preview source')
        self.latest[view]=(frame_id,timestamp,payload);self.epochs[view]=epoch

    def take(self):
        if len(self.latest)!=2 or self.epochs['front']!=self.epochs['side']:return None
        a,b=(self.latest[v]for v in ('front','side'))
        if abs(a[1]-b[1])>self.max_skew_ns:return None
        if any(self.used.get(v)==(self.epochs[v],self.latest[v][0])for v in ('front','side')):return None
        result=dict(self.latest);self.capture_epoch=self.epochs['front']
        self.used={v:(self.epochs[v],r[0])for v,r in result.items()}
        return result


class PreviewState:
    def __init__(self):
        self.lock=Lock();self.changed=Condition(self.lock)
        self.data={'status':'starting','frames':0};self.jpeg=None;self.sequence=0
        self.session_id=str(time.monotonic_ns())

    def publish(self, data, jpeg=None):
        with self.changed:
            self.data=dict(data)
            if jpeg is not None:
                self.jpeg=jpeg;self.sequence+=1
            self.changed.notify_all()

    def snapshot(self):
        with self.lock:data=dict(self.data,session_id=self.session_id)
        stamp=data.get('source_timestamp_ns')
        data['source_age_ms']=None if stamp is None else max(0,(time.monotonic_ns()-stamp)/1e6)
        return data


def make_server(state, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def stream_frames(self):
            self.connection.settimeout(3)
            self.send_response(200)
            self.send_header('Content-Type','multipart/x-mixed-replace; boundary=posepriv')
            self.send_header('Cache-Control','no-store')
            self.end_headers();previous=-1
            while True:
                with state.changed:
                    state.changed.wait_for(lambda:state.sequence!=previous or
                        state.data.get('status')in('failed','complete'),timeout=.5)
                    if state.data.get('status')in('failed','complete'):return
                    stamp=state.data.get('source_timestamp_ns')
                    current=state.sequence
                    jpeg=state.jpeg
                    fresh=(stamp is not None and 0<=time.monotonic_ns()-stamp<=1_000_000_000)
                    if current==previous or jpeg is None or not fresh:
                        previous=current
                        continue
                    previous=current
                # A slow browser owns only this socket; no producer lock during I/O.
                try:
                    self.wfile.write(b'--posepriv\r\nContent-Type: image/jpeg\r\nContent-Length: '+
                        str(len(jpeg)).encode()+b'\r\n\r\n'+jpeg+b'\r\n')
                except (OSError,TimeoutError):return
        def do_GET(self):
            path=self.path.split('?',1)[0]
            if path=='/stream.mjpg':
                self.stream_frames();return
            if path=='/':data=PAGE.encode();kind='text/html; charset=utf-8'
            elif path=='/status.json':data=json.dumps(state.snapshot()).encode();kind='application/json'
            elif path=='/frame.jpg':
                with state.lock:data=state.jpeg
                kind='image/jpeg'
                if data is None:self.send_error(503);return
            else:self.send_error(404);return
            self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(data)));self.end_headers()
            try:self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler);server.daemon_threads=True
    return server


class CachedTextBand:
    """Draw a changing label in its own band; reuse bounded exact pixel snapshots."""
    def __init__(self, bounds, background, max_entries=32):
        from collections import OrderedDict
        self.bounds=bounds;self.background=background;self.max_entries=max_entries
        self.cache=OrderedDict()

    def draw(self, canvas, overlay, item):
        from dataclasses import replace
        x0,y0,x1,y1=self.bounds
        band=self.cache.pop(item,None)
        if band is None:
            band=canvas[y0:y1,x0:x1].copy();band[:]=self.background
            overlay.draw_many(band,[replace(item,x=item.x-x0,y=item.y-y0)])
        self.cache[item]=band
        if len(self.cache)>self.max_entries:self.cache.popitem(last=False)
        canvas[y0:y1,x0:x1]=band


class NativePreviewFreshness:
    """Status transitions use source age; waiting screens are never fresh frames."""
    def __init__(self,stale_after_ns=1_000_000_000):
        self.stale_after_ns=stale_after_ns;self.source_timestamp_ns=None;self.state='loading'

    def accept(self,source_timestamp_ns):
        resumed=self.state!='live'
        self.source_timestamp_ns=source_timestamp_ns;self.state='live'
        return resumed

    def waiting(self,now_ns):
        if self.state=='live'and now_ns-self.source_timestamp_ns>self.stale_after_ns:
            self.state='stale';return True
        return False


class PendingPresentation:
    """One submitted frame; only a successful native completion permits publication."""
    def __init__(self):self._pending=None

    def submit(self,record):
        if self._pending is not None:raise RuntimeError('A presentation is already pending')
        self._pending=record

    def drain(self,finish,publish):
        if self._pending is None:return False
        record=self._pending;self._pending=None
        completion=finish()
        if completion is None:raise RuntimeError('Pending presentation has no native completion')
        publish(record,completion)
        return True


def _run(config, port, rotations, inputs, stopped, result, speech_volume=None):
    # The foreground parent owns Ctrl+C and coordinates normal child shutdown.
    signal.signal(signal.SIGINT,signal.SIG_IGN)
    def interrupted(*_):raise KeyboardInterrupt('Preview terminated')
    signal.signal(signal.SIGTERM,interrupted)
    state=PreviewState();server=None;frames=0;pose_updates=0;image_tracking=False;started=None;last={};error=None;inference_worker=None;model=None;presentations=None;inference_process=False;speech=None;geometry_worker=None;geometry_process_owner=None;skinning_pipeline=False;skinning_worker=None
    try:
        if port is not None:
            server=make_server(state,port);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        cfg=json.loads(Path(config).read_text())
        sys.path.insert(0,cfg['python_path'])
        from stream_runtime import pose_backend,make_stronger_model,map_display_work
        backend=pose_backend(cfg);stronger=backend!='movenet'
        from .field_image_tracking import image_tracking_enabled
        image_tracking=image_tracking_enabled(cfg)
        from .field_preview_process import model_ready_overlap_enabled
        model_ready_overlap_enabled(cfg)
        from .native_skinning_validator import native_upper64_scan_enabled
        native_upper64_scan=native_upper64_scan_enabled(cfg)
        from .field_preview_geometry import geometry_pipeline_enabled
        from leased_pipeline import (snapshot_leased_work as snapshot_inference_work,
            transform_owned,LeasedLatestWorker,UploadLeaseRelease)
        from stream_runtime import PreviewGeometryBuilder
        from .field_preview_geometry_process import geometry_process_enabled
        geometry_thread=geometry_pipeline_enabled(cfg)
        geometry_process=geometry_process_enabled(cfg)
        geometry_pipeline=geometry_thread or geometry_process
        from .field_preview_skinning import skinning_pipeline_enabled,execute_prepared_skinning
        skinning_pipeline=skinning_pipeline_enabled(cfg)
        if not skinning_pipeline:raise ValueError('split pipeline profile requires preview_skinning_pipeline=true')
        static_mesh=cfg.get('preview_static_mesh',False)
        if type(static_mesh)is not bool:raise ValueError('preview_static_mesh must be boolean')
        from .native_limb_axis import native_limb_axis_enabled,NativeLimbAxisBuilder
        from .native_rig_math import rig_math_library,NativeRigMath
        from .native_rig_tail import rig_tail_library
        rig_math_path=rig_math_library(cfg)
        rig_tail_path=rig_tail_library(cfg)
        native_limb_axis=native_limb_axis_enabled(cfg)
        from .compact_presentation_trace import make_presentation_trace
        presentations=make_presentation_trace(cfg)
        os.environ.update(LD_LIBRARY_PATH='/opt/vitis-ai-compat/lib:/lib/aarch64-linux-gnu:/lib',
                          XLNX_VART_FIRMWARE=cfg['xclbin'],XLNX_DISABLE_LOAD_XCLBIN='0',
                          DISPLAY=':0',XAUTHORITY='/run/user/1000/gdm/Xauthority')
        import cv2
        import numpy as np
        from .movenet_cnn_worker import load_movenet_cnn_worker
        from .pipeline import MoveNetPoseEstimator
        from .capture import CapturedFrame
        from .contracts import CameraView
        from .avatar_setup import load_avatar_preview_setup
        from .pose_avatar_preview import PoseAvatarPreviewPlanner
        from .preview_rigid_avatar import PreviewRigidAvatar
        from .native_skinning_validator import NativeSkinningValidator
        from .linux_shared_pose_dma import LinuxSharedPoseDma
        from .field_preview_gles import make_painter
        from .field_preview_pipeline import LatestInferenceWorker
        from stream_runtime import infer_latest_pair
        from .field_playback import pause_recorded_demo
        from .skeleton_display import SKELETON_EDGES
        from .text_overlay import OptionalCjkTextOverlay,OverlayText,default_cjk_font
        from .session import load_squat_session_processor
        cv2.setNumThreads(1)
        decode_scale=cfg.get('preview_decode_scale',1)
        if type(decode_scale) is not int or decode_scale not in (1,2,4):
            raise ValueError('preview_decode_scale must be 1, 2 or 4')
        decode_flag={1:cv2.IMREAD_COLOR,2:cv2.IMREAD_REDUCED_COLOR_2,4:cv2.IMREAD_REDUCED_COLOR_4}[decode_scale]
        with ExitStack() as resources, ExitStack() as inference_resources:
            inference_process=cfg.get('preview_inference_process',False)
            estimator=None
            roi_fd_owner=None
            if cfg.get('preview_roi_crop') is True:
                if not inference_process:raise ValueError('ROI crop requires the inference process')
                from roi_shared_fd import SharedDmaFdOwner
                roi_fd_owner=resources.enter_context(SharedDmaFdOwner())
            if inference_process:
                from leased_stream_process import InferenceProcess
                inference_options={} if roi_fd_owner is None else dict(shared_dma_fd=roi_fd_owner.for_child())
                inference_worker=InferenceProcess(cfg,inputs,rotations,stopped,**inference_options)
                inference_resources.callback(inference_worker.close)
            if not inference_process:
                if stronger:model=make_stronger_model(cfg)
                else:
                    model=load_movenet_cnn_worker(*(cfg[k]for k in ('worker','model','mapping','post_model','post_mapping')),
                        **{k:cfg['preview_'+k]for k in ('native_post_library','native_post_contract')if cfg.get('preview_'+k)is not None})
                    estimator=MoveNetPoseEstimator(model)
                resources.callback(model.close)
            validator=NativeSkinningValidator(cfg['skinning_validator'])
            if native_upper64_scan:validator.require_upper64_scan()
            dma_options={} if roi_fd_owner is None else dict(opener=roi_fd_owner.take_for_parent)
            if static_mesh:
                from .static_mesh_dma import StaticMeshLinuxSharedPoseDma,StaticMeshAvatarSkinningBackend
                dma=resources.enter_context(StaticMeshLinuxSharedPoseDma(skinning_validator=validator,**dma_options))
            else:
                dma=resources.enter_context(LinuxSharedPoseDma(skinning_validator=validator,**dma_options))
            setup=load_avatar_preview_setup(mesh_path=cfg.get('mesh'),shared_dma=dma,
                skinning_validator=validator,native_upper64_scan=native_upper64_scan)
            resources.callback(setup.close);runtime=setup.runtime;asset=setup.mesh_asset
            if static_mesh:
                runtime.backend=StaticMeshAvatarSkinningBackend(dma,runtime.vertex_requests,
                    upper64_check=runtime.backend.upper64_check)
            else:
                from .shared_skinning_batches import ReusedSkinningFrameTransfer
                runtime.backend.transfer=ReusedSkinningFrameTransfer(dma)
            affine_composer=bone_builder=limb_builder=rig_planner=rig_math=None
            planner_flags=dict(hand_bones='left_hand'in asset.bone_names,
                depth_stability=True,palm_roll_stability=True,head_stability=True,head_depth=True)
            if not geometry_process:
                if cfg.get('preview_affine_library'):
                    from .native_affine import NativeAffineComposer
                    affine_composer=NativeAffineComposer(cfg['preview_affine_library'])
                if cfg.get('preview_bone_affine_library'):
                    from .native_bone_affine import NativeBoneAffineBuilder
                    bone_builder=NativeBoneAffineBuilder(cfg['preview_bone_affine_library'])
                limb_builder=NativeLimbAxisBuilder(bone_builder)if native_limb_axis else None
                rig_math=NativeRigMath(rig_math_path)if rig_math_path else None
                runtime.planner=PoseAvatarPreviewPlanner(**planner_flags,
                    affine_composer=affine_composer,bone_affine_builder=bone_builder)
                rig_planner=PreviewRigidAvatar(asset,runtime.planner.config.bone_links,runtime.bone_topology_indices,
                    affine_composer=affine_composer,bone_affine_builder=bone_builder,limb_axis_builder=limb_builder,math_builder=rig_math,
                    tail_library=rig_tail_path,
                    head_cue_hold=cfg.get('preview_head_cue_hold',False),
                    head_current_axes=cfg.get('preview_head_current_axes',False),
                    finite_arm_reach=cfg.get('preview_finite_arm_reach',False),
                    leg_recovery=cfg.get('preview_leg_recovery',False))
            resources.callback(pause_recorded_demo(cfg.get('recorded_demo')))
            hdmi=bool(cfg.get('hdmi_display',False))
            gpu_video=hdmi and cfg.get('preview_gpu_video',False)
            late_finish=cfg.get('preview_late_finish',False)
            if type(late_finish)is not bool or late_finish and not hdmi:
                raise ValueError('preview_late_finish requires native HDMI and a boolean option')
            video_native_size=cfg.get('preview_video_native_size',False)
            if type(video_native_size)is not bool or video_native_size and not(hdmi and gpu_video):
                raise ValueError('preview_video_native_size requires native HDMI, GPU video and a boolean option')
            swap_before_finish=cfg.get('preview_swap_before_finish',False)
            if type(swap_before_finish)is not bool or swap_before_finish and not(hdmi and late_finish):
                raise ValueError('preview_swap_before_finish requires native HDMI, late finish and a boolean option')
            normal_preparer=None
            if cfg.get('preview_normal_prep_library'):
                from .native_gles_prep import NativeGlesNormalPreparer
                normal_preparer=NativeGlesNormalPreparer(cfg['preview_normal_prep_library'])
            painter=make_painter(asset,hdmi=hdmi,region_readback=cfg.get('preview_region_readback',True),msaa_samples=cfg.get('preview_hdmi_msaa'),gpu_video=gpu_video,late_finish=late_finish,video_native_size=video_native_size,swap_before_finish=swap_before_finish,cache_locations=cfg.get('preview_gl_locations_cache',False),normal_preparer=normal_preparer,native_submit_library=cfg['preview_native_gl_submit_library'],avatar_yaw_degrees=cfg.get('preview_avatar_yaw_degrees',30.));resources.callback(painter.close)
            upload_lease=UploadLeaseRelease(painter);resources.callback(upload_lease.close)
            from .field_preview_speech import PreviewSpeechFeedback
            speech=PreviewSpeechFeedback(speech_volume)
            resources.callback(speech.close)
            overlay=OptionalCjkTextOverlay(default_cjk_font(),numpy_module=np)
            scorer=load_squat_session_processor(cfg['rules'])if cfg.get('tail4_scoring_enabled',False)and not geometry_process else None
            latest=LatestPair(max_skew_ns=50_000_000)if stronger else LatestPair();completed_score=None;rep_count=0
            backdrop=np.full((720,1280,3),(28,18,13),np.uint8)
            avatar_background=(64,92,30)  # BGR: dark forest green.
            backdrop[108:563,633:1250]=avatar_background
            recorded=cfg.get('recorded_input',False)
            static=[('PosePriv · 深蹲训练' if not recorded else 'PosePriv · 深蹲回放',28,15,26),
                ('双视角',35,75,20),('数字人',640,75,20),
                ('正面',49,96,14),('侧面',327,96,14)]
            from .compact_coach import summary_text
            diagnostics=bool(cfg.get('preview_show_diagnostics',False))
            overlay.draw_many(backdrop,[OverlayText(s,x,y,size,(230,235,240))for s,x,y,size in static])
            def speech_label():
                info=speech.stats()
                return '语音：不可用' if info['failure'] else '语音：开启' if info['requested'] else ' '
            previous_speech=speech_label()
            overlay.draw_many(backdrop,[OverlayText(previous_speech,1010,20,16,(170,205,240))])
            if hdmi:
                overlay.draw_many(backdrop,[OverlayText('实时帧率',680,628,18,(230,235,240))])
                cv2.putText(backdrop,'-- FPS',(783,649),cv2.FONT_HERSHEY_SIMPLEX,.58,(80,230,130),1,cv2.LINE_AA)
                if diagnostics:
                    overlay.draw_many(backdrop,[OverlayText('源图年龄',958,628,18,(230,235,240))])
                    cv2.putText(backdrop,'-- ms',(1058,649),cv2.FONT_HERSHEY_SIMPLEX,.58,(80,230,130),1,cv2.LINE_AA)
            if hdmi:
                native_freshness=NativePreviewFreshness()
                status_canvases={}
                for key,message in (('loading','模型准备中 · 等待双视角数据'),('stale','等待双视角新画面')):
                    status_canvas=backdrop.copy();status_canvas[95:720,:]=(28,18,13)
                    overlay.draw_many(status_canvas,[OverlayText(message,360,310,26,(230,235,240))])
                    status_canvases[key]=status_canvas
                painter.show_status(status_canvases['loading'])
            info_timestamp=None;info_frames=0
            previous_summary=None;previous_visibility=None
            body_band=CachedTextBand((635,108,1250,140),avatar_background)
            score_band=CachedTextBand((0,580,1280,620),(28,18,13))
            hint_band=CachedTextBand((0,665,1280,710),(28,18,13))
            previous_hint=None
            speech_band=CachedTextBand((1000,15,1270,48),(28,18,13))
            parallel_decode=cfg.get('preview_parallel_decode',False)or(inference_process and cfg.get('preview_paired_requests',False))
            if type(parallel_decode) is not bool:raise ValueError('preview_parallel_decode must be boolean')
            decoder=None
            if parallel_decode and not inference_process:
                from concurrent.futures import ThreadPoolExecutor
                decoder=ThreadPoolExecutor(max_workers=2,thread_name_prefix='posepriv-decode')
                inference_resources.callback(decoder.shutdown,wait=True,cancel_futures=True)
            def infer_latest():
                return infer_latest_pair(inputs,latest,rotations,estimator,model,decode_flag,decoder,
                    **(dict(pose_backend=backend)if stronger else{}))
            pipeline=cfg.get('preview_inference_pipeline',not hdmi)
            if type(pipeline) is not bool:raise ValueError('preview_inference_pipeline must be boolean')
            if pipeline and not inference_process:
                inference_worker=LatestInferenceWorker(infer_latest,stopped)
                inference_resources.callback(inference_worker.close)
            from .field_preview_geometry import make_display_stabilizer,make_head_stabilizer
            geometry_builder=None if geometry_process else PreviewGeometryBuilder(runtime,asset,rig_planner,scorer,
                stabilizer=make_display_stabilizer(cfg),head_stabilizer=make_head_stabilizer(cfg,asset),config=cfg)
            if geometry_pipeline:
                if geometry_process:
                    from geometry_full33_process import GeometryProcess,Cancelled,make_geometry_spec
                    spec=make_geometry_spec(runtime,asset,cfg,planner_flags)
                    geometry_process_owner=GeometryProcess(spec,stopped=stopped)
                    # Workers return image leases in the inner stack, then the
                    # inference owner joins before this idle CPU owner exits.
                    resources.callback(geometry_process_owner.close)
                def prepare_geometry_item(item):
                    item=map_display_work(item,cfg)
                    if time.monotonic_ns()-min(r[1]for r in item['selected'].values())>500_000_000:return None
                    if geometry_process_owner is not None:
                        try:prepared=geometry_process_owner.prepare(item,runtime.vertex_requests)
                        except Cancelled:
                            if geometry_process_owner.cancel.is_set():return None
                            raise
                    else:prepared=geometry_builder.prepare(item)
                    return prepared
                def prepare_next_geometry():
                    return transform_owned(snapshot_inference_work(inference_worker),
                        prepare_geometry_item,stopped)
                geometry_worker=LeasedLatestWorker(prepare_next_geometry,stopped,
                    name='posepriv-geometry',block_when_ready=True)
                # Close this consumer before its model/arena provider (ExitStack LIFO).
                inference_resources.callback(geometry_worker.close)
                from leased_pipeline import make_skinning_worker
                skinning_worker=make_skinning_worker(geometry_worker,runtime.backend,stopped)
                inference_resources.callback(skinning_worker.close)
                if geometry_process_owner is not None:inference_resources.callback(geometry_process_owner.cancel.set)
            pending=PendingPresentation()
            def displayed_pose_count():
                return pose_updates
            def publish_completed(record,completion=None):
                nonlocal frames,pose_updates,started,last
                now=record['ready_ns']if completion is None else completion['completed_ns']
                value=record['state'];components=value['component_ms']
                if completion is not None:
                    components['gpu_draw_submit']=(record['drawn_ns']-record['overlaid_ns'])/1e6
                    components['gpu_finish_swap_wait']=completion['finish_swap_ns']/1e6
                    components['gpu_draw_present']=(now-record['overlaid_ns'])/1e6
                    components['jpeg']=0
                    if 'swap_return_ns'in completion:
                        components['gpu_swap_submit_host']=completion['swap_host_ns']/1e6
                        components['gpu_finish_wait']=completion['finish_swap_ns']/1e6
                        value['presentation'].update(swap_return_ns=completion['swap_return_ns'],swap_host_ns=completion['swap_host_ns'])
                if started is None:started=record['begin_ns']
                frames+=1
                if value['fresh_pose_pair']:
                    pose_updates+=1
                value.update(frames=frames,fps=displayed_pose_count()*1e9/(now-started),processing_ms=(now-record['begin_ns'])/1e6)
                value['presentation']['gpu_completed_frames']=painter.presented_frames
                value['gpu_confirmed_pose_source_pairs']=pose_updates
                value['pose_pair_fps']=pose_updates*1e9/(now-started)
                value['video_pair_fps']=frames*1e9/(now-started)
                if hdmi:value['presentation']['confirmed_source_age_ms']=max(0,(now-value['source_timestamp_ns'])/1e6)
                if speech.requested:
                    count,score=record['speech_state']
                    speech.confirmed(repetition_count=count,score=score,
                        source_timestamp_ns=value['source_timestamp_ns'],completed_ns=now,stopping=stopped.is_set())
                value['speech_feedback']=speech.stats()
                last=value
                if presentations is not None:
                    sample=dict(completed_ns=now,source_ids=last['source_ids'],source_timestamp_ns=last['source_timestamp_ns'],component_ms=components,
                        preview_static_mesh=static_mesh,skinning_transport=last['skinning_transport'],skinning_ioctl=last['skinning_ioctl'],
                        inference_begin_ns=record['begin_ns'],inference_completed_ns=record['inferred_ns'],inference_fetch_ns=record['fetch_ns'])
                    for clock in ('decode_ready_ns','model_begin_ns'):
                        if clock in record:sample[clock]=record[clock]
                    if stronger:
                        sample.update(pose_backend=backend,physical_source_ids=last['physical_source_ids'],
                            display_view_mapping=last['display_view_mapping'],fresh_pose_pair=last['fresh_pose_pair'],
                            gpu_confirmed_pose_source_pairs=pose_updates,full33_presence=last['full33_presence'],
                            stream_pair_sequence=last['stream_pair_sequence'],stream_sources=last['stream_sources'],
                            capture_epoch=last['capture_epoch'],
                            scoring_profile=last['scoring_profile'],repetition_count=last['repetition_count'],
                            completed_repetition_score=last['completed_repetition_score'])
                    if image_tracking:
                        sample.update(current_image_tracking=dict(last['current_image_tracking']),
                            gpu_confirmed_pose_source_pairs=pose_updates)
                    if 'swap_return_ns'in value['presentation']:
                        sample.update(swap_return_ns=value['presentation']['swap_return_ns'],swap_host_ns=value['presentation']['swap_host_ns'])
                    presentations.append(sample)
                state.publish(last,record['jpeg'])
            def drain_pending():
                return pending.drain(painter.finish_present,publish_completed)
            state.publish(dict(status='ready_for_source',frames=0,quality_only=True))
            while not stopped.is_set():
                if hdmi and native_freshness.waiting(time.monotonic_ns()):
                    drain_pending()
                    speech.suspend()
                    painter.show_status(status_canvases['stale'])
                prepared=skinning_worker.take()if skinning_worker is not None else None
                geometry_taken=time.monotonic_ns()if skinning_pipeline and prepared is not None else None
                if geometry_worker is not None:work=None if prepared is None else prepared['work']
                else:work=inference_worker.take()if inference_worker is not None else infer_latest()
                if work is not None and geometry_worker is None:work=map_display_work(work,cfg)
                if work is None:
                    drain_pending();continue
                with upload_lease.frame(work):
                    selected=work['selected'];poses=work['poses'];images=work['images'];inference_stages=work['stages']
                    if time.monotonic_ns()-min(r[1]for r in selected.values())>500_000_000:
                        if inference_process and geometry_worker is None:inference_worker.release()
                        drain_pending()
                        continue
                    if hdmi and native_freshness.accept(min(r[1]for r in selected.values())):
                        painter.bg_initialized=False;info_timestamp=None;info_frames=displayed_pose_count()
                        for xx,ww,value in (((780,155,'-- FPS'),)+(((1055,180,'-- ms'),) if diagnostics else ())):
                            backdrop[627:658,xx:xx+ww]=(28,18,13)
                            cv2.putText(backdrop,value,(xx+3,649),cv2.FONT_HERSHEY_SIMPLEX,.58,(80,230,130),1,cv2.LINE_AA)
                    if prepared is None:prepared=geometry_builder.prepare(work)
                    begin=work['begin'];inferred=work['inferred'];score_started=prepared['score_started_ns']
                    scored=prepared['scored_ns'];planned=prepared['planned_ns']
                    pair=prepared['pair'];plan=prepared['plan'];rig=prepared['rig']
                    if cfg.get('preview_display_stabilization')is True:poses=prepared['display_poses']
                    observed_core=prepared['observed_core']
                    rep_count=prepared['repetition_count'];completed_score=prepared['completed_score']
                    if skinning_pipeline:
                        outputs=prepared['skinning_outputs']
                        skin_started=prepared['skin_started_ns'];skinned=prepared['skinned_ns']
                    else:
                        skin_started=time.monotonic_ns()
                        outputs=plan.execute(runtime.backend,program_matrices=runtime.backend.program_matrices)
                        skinned=time.monotonic_ns()
                    dirty_regions=[]
                    current_speech=speech_label()
                    if current_speech!=previous_speech:
                        speech_band.draw(backdrop,overlay,OverlayText(current_speech,1010,20,16,(170,205,240)))
                        previous_speech=current_speech;dirty_regions.append((1000,15,270,33))
                    coach=prepared.get('coach',{})
                    body_label='请保持全身入镜' if rig.reason=='neutral' else ''
                    if body_label!=previous_visibility:
                        body_band.draw(backdrop,overlay,OverlayText(body_label or ' ',640,110,18,(220,235,225)))
                        previous_visibility=body_label;dirty_regions.append((635,108,615,32))
                    scoring_enabled=cfg.get('tail4_scoring_enabled',False)
                    current_summary=summary_text(rep_count,completed_score,coach,enabled=scoring_enabled)
                    hint=coach.get('hint','请保持全身入镜') if scoring_enabled else '请保持全身入镜'
                    if not hint.startswith('下次建议：'):hint='修改建议：'+hint
                    if hint!=previous_hint:
                        hint_band.draw(backdrop,overlay,OverlayText(hint,28,671,25,(130,230,180)))
                        previous_hint=hint;dirty_regions.append((0,665,1280,45))
                    if current_summary!=previous_summary:
                        score_band.draw(backdrop,overlay,OverlayText(current_summary,28,585,22,(230,235,240)))
                        previous_summary=current_summary;dirty_regions.append((0,580,1280,40))
                    if hdmi:
                        info_now=time.monotonic_ns()
                        if info_timestamp is None:info_timestamp=info_now;info_frames=displayed_pose_count()
                        elif info_now-info_timestamp>=1_000_000_000:
                            display_fps=(displayed_pose_count()-info_frames)*1e9/(info_now-info_timestamp)
                            age_ms=last.get('presentation',{}).get('confirmed_source_age_ms',
                                max(0,(info_now-min(r[1]for r in selected.values()))/1e6))
                            values=((780,155,f'{display_fps:.1f} FPS'),)
                            if diagnostics:values+=((1055,180,f'{age_ms:.0f} ms'),)
                            for xx,ww,value in values:
                                backdrop[627:658,xx:xx+ww]=(28,18,13)
                                cv2.putText(backdrop,value,(xx+3,649),cv2.FONT_HERSHEY_SIMPLEX,.58,(80,230,130),1,cv2.LINE_AA)
                                dirty_regions.append((xx,627,ww,31))
                            info_timestamp=info_now;info_frames=displayed_pose_count()
                    canvas=backdrop if gpu_video else backdrop.copy();source_views=[]if gpu_video else None
                    for view,x in (('side',49),('front',327)):
                        im=images[view];scale=min(253/im.shape[1],450/im.shape[0]);w,h=max(1,round(im.shape[1]*scale)),max(1,round(im.shape[0]*scale))
                        xx,yy=x+(253-w)//2,112+(450-h)//2
                        if gpu_video:
                            source_views.append((view,im,poses[view].keypoints,(xx,yy,w,h)));continue
                        canvas[yy:yy+h,xx:xx+w]=cv2.resize(im,(w,h))
                        pts=poses[view].keypoints
                        for a,b in SKELETON_EDGES:
                            pa,pb=pts.get(a),pts.get(b)
                            if pa is not None and pb is not None and min(pa.confidence,pb.confidence)>=.5:
                                cv2.line(canvas,(xx+round(pa.x*w),yy+round(pa.y*h)),(xx+round(pb.x*w),yy+round(pb.y*h)),(80,230,130),2)
                        for pt in pts.values():
                            if pt.confidence>=.5:cv2.circle(canvas,(xx+round(pt.x*w),yy+round(pt.y*h)),3,(50,190,255),-1)
                    overlaid=time.monotonic_ns()
                    if late_finish:drain_pending()
                    gpu_commands_started=time.monotonic_ns()if late_finish else overlaid
                    canvas,triangles=painter.draw(canvas,plan,outputs,rig.display_transform,source_views=source_views,dirty_regions=dirty_regions)
                    if inference_process and geometry_worker is None:inference_worker.release()
                    drawn=time.monotonic_ns()
                    encoded=None
                    if not hdmi:
                        ok,encoded=cv2.imencode('.jpg',canvas,[cv2.IMWRITE_JPEG_QUALITY,80])
                        if not ok:raise RuntimeError('Preview JPEG encoding failed')
                    now=time.monotonic_ns()
                    frame_state=dict(status='running',source_ids=[selected[v][0]for v in ('front','side')],
                        source_timestamp_ns=min(r[1]for r in selected.values()),skew_ms=pair.skew_ns/1e6,
                        component_ms=dict(infer_and_score=((inferred-begin)+(scored-score_started))/1e6,
                        inference_queue_wait=(score_started-inferred)/1e6,plan=(planned-scored)/1e6,skin=(skinned-(skin_started if geometry_worker is not None else planned))/1e6,overlay=(overlaid-(geometry_taken if skinning_pipeline else skinned))/1e6,
                        gpu_draw_present=(drawn-overlaid)/1e6,jpeg=(now-drawn)/1e6),renderer=painter.renderer,triangles=triangles,
                        dpu_frames=work['dpu_frames'],hardware_skinning=True,mesh_vertices=asset.vertex_count,
                        preview_static_mesh=static_mesh,skinning_transport='static_mesh_matrix_only'if static_mesh else'bounded_copy_v1',
                        skinning_ioctl='0x40205034'if static_mesh else'0x40205031',
                        native_upper64_scan=native_upper64_scan,
                        rig_reason=rig.reason,inferred_bones=len(rig.inferred_slots),
                        display_stabilization='adaptive_joint_5_30_1'if cfg.get('preview_display_stabilization')is True else None,
                        head_stabilization='final_world_12_2_1'if cfg.get('preview_head_world_stabilization')is True else None,
                        head_cue_hold=cfg.get('preview_head_cue_hold',False),
                        head_current_axes=cfg.get('preview_head_current_axes',False),
                        finite_arm_reach=cfg.get('preview_finite_arm_reach',False),
                        leg_recovery=cfg.get('preview_leg_recovery',False),
                        bone_geometry_backend='native_cpu'if bone_builder is not None or geometry_process and cfg.get('preview_bone_affine_library')else'python',
                        limb_axis_backend='native_cpu_history'if limb_builder is not None or geometry_process and native_limb_axis else'python',
                        rig_math_backend='native_cpu_exact_small_math'if rig_math_path else'python',
                        rig_tail_backend='native_cpu_13_bone'if rig_tail_path else'python',
                        normal_prep_backend='native_cpu_cofactor'if normal_preparer is not None else'numpy',
                        observed_core=observed_core,
                        hdmi_display=hdmi,
                        presentation=dict(screen_size=painter.screen_size,egl_config=painter.egl.config_info,
                            gpu_completed_frames=painter.presented_frames,gpu_video=painter.gpu_video,late_finish=late_finish,
                            gl_locations_cache=cfg.get('preview_gl_locations_cache',False),
                            video_native_size=video_native_size,video_upload_sizes=dict(getattr(painter,'video_upload_sizes',{})),
                            swap_before_finish=swap_before_finish,world_math_native=True,native_gl_submit=True,native_gl_submit_calls=painter.native_gl_submit.calls,skinning_boundary='reused_frame',native_world_filter=True,native_output_boundaries=True,native_initial_plan=True,arm_recovery=cfg.get('preview_arm_recovery',False),
                            confirmation_scope=('swap is issued at submission; completion requires later glFinish. Neither is physical scanout; confirmed source age includes next CPU preparation and is not exposure latency'
                                if swap_before_finish else 'completion follows glFinish and native swap/readback; not physical scanout'),
                            timing_scope=('gpu_draw_present is submission-to-confirmed-presentation wall including next CPU preparation; gpu_draw_submit and gpu_finish_swap_wait are host calls, not pure GPU latency'
                                if late_finish else 'GPU draw, finish and native swap/readback host wall; not physical scanout')),
                        preview_decode_scale=decode_scale,parallel_decode=parallel_decode,inference_stages_ms=inference_stages,
                        inference_pipeline=None if inference_worker is None else inference_worker.stats(),
                        scope=('live DPU + PPS4 + Mali native X11 window; not measured physical scanout FPS or calibrated 3D'
                               if hdmi else 'live DPU + PPS4 + Mali with JPEG readback; not measured physical scanout FPS or calibrated 3D'))
                    frame_state.update(pose_backend=backend,pose_frames=work['pose_frames'],
                        repetition_count=rep_count,completed_repetition_score=None if completed_score is None else dict(
                            valid=completed_score.valid,conformance_percent=completed_score.conformance_percent,
                            confidence_percent=completed_score.confidence_percent,coverage_percent=completed_score.coverage_percent),
                        physical_source_ids=work['physical_source_ids'],display_view_mapping='device_names',
                        full33_presence=work['full33_presence'],tail4_model_stats=work['tail4_model_stats'],
                        stream_sources=work['stream_sources'],stream_pair_sequence=work['stream_pair_sequence'],
                        capture_epoch=work.get('capture_epoch',0),
                        fresh_pose_pair=all(work['poses'][v].keypoints for v in ('front','side')),
                        scoring_profile=work['scoring_profile'],scoring_enabled=cfg.get('tail4_scoring_enabled',False),
                        scoring_qualified=False,avatar_physical_view=cfg.get('tail4_avatar_view','front'),
                        world_stabilization='display_vector_5_30_1'if cfg.get('preview_display_stabilization')is True else None,
                        display_confidence_semantics='min_original_presence_visibility_sigmoid_not_calibrated_probability',
                        scope='KV260 cross-pair streaming ARM+DPU+Tail4 PL+PPS4/Mali; original raw17 score candidate; no host inference or physical scanout claim')
                    if image_tracking:
                        frame_state.update(current_image_tracking=dict(work['current_image_tracking']),
                            display_confidence_semantics='binary_inferred_display_mask_not_CNN_confidence',
                            scope='Current-pixel ARM tracking with actual periodic DPU CNN + PPS4/Mali; CNN, image measurements and GPU pose updates are separate; not physical scanout or calibrated3D')
                    if geometry_worker is not None:
                        frame_state['geometry_pipeline']=dict(geometry_worker.stats(),owner='process'if geometry_process_owner is not None else'thread',private_image_snapshot=False,decoded_image_lease=True)
                        frame_state['component_ms'].update(geometry_output_wait=((geometry_taken-skinned)if skinning_pipeline else(skin_started-planned))/1e6,
                            geometry_image_snapshot=work['geometry_image_snapshot_ms'])
                    if skinning_pipeline:
                        frame_state.update(skinning_pipeline=True,skinning_owner='parent_single_skinning_worker',
                            split_pipeline=True,skinning_worker=skinning_worker.stats())
                        frame_state['component_ms'].update(geometry_plan_to_consume=(geometry_taken-planned)/1e6,
                            geometry_plan_to_skin=(skin_started-planned)/1e6)
                    if geometry_process_owner is not None:frame_state['geometry_process']=geometry_process_owner.stats()
                    record=dict(state=frame_state,begin_ns=begin,inferred_ns=inferred,fetch_ns=work['fetch_started'],
                        overlaid_ns=gpu_commands_started,drawn_ns=drawn,ready_ns=now,jpeg=None if encoded is None else encoded.tobytes(),
                        speech_state=(rep_count,completed_score))
                    for clock in ('decode_ready_ns','model_begin_ns'):
                        if clock in work:record[clock]=work[clock]
                    if late_finish:pending.submit(record)
                    else:publish_completed(record)
            drain_pending()
    except BaseException as exc:
        error=f'{type(exc).__name__}: {exc}'
        state.publish(dict(last,status='failed',error=error,frames=frames))
    finally:
        if geometry_process_owner is not None:
            close=geometry_process_owner.close_result
            if error is None and (close is None or not close.get('graceful')or close.get('error')or close.get('forced_actions')or close.get('exitcode')!=0):
                error='Geometry process did not close cleanly: '+str(close)
        if frames==0 and error is None:error='No live preview frames generated'
        final=dict(last,status='failed'if error else'complete',error=error,frames=frames)
        if speech is not None:final['speech_feedback']=speech.stats()
        if geometry_worker is not None:final['geometry_pipeline']=dict(geometry_worker.stats(),owner='process'if geometry_process_owner is not None else'thread',private_image_snapshot=False,decoded_image_lease=True)
        if geometry_process_owner is not None:final['geometry_process']=geometry_process_owner.stats()
        if skinning_worker is not None:final['skinning_worker']=skinning_worker.stats();final['split_pipeline']=True
        if inference_worker is not None:final['inference_pipeline']=inference_worker.stats()
        if model is not None:final['dpu_frames']=model.stats()['frames']
        elif inference_worker is not None:final['dpu_frames']=inference_worker.stats()['dpu_frames']
        if cfg.get('preview_pose_backend','movenet')!='movenet':
            totals=model.stats()if model is not None else final.get('inference_pipeline',{}).get('final_model_stats')
            if totals is not None:
                final['total_pose_frames']=totals['frames'];final['final_tail4_model_stats']=totals
        if presentations is not None:
            final['presentation_samples']=list(presentations)
            final['presentation_trace']=presentations.metadata()
        state.publish(final)
        try:result.put_nowait(final)
        except queue.Full:pass
        if server is not None:server.shutdown();server.server_close()


class LivePreview:
    """Child owns model/DMA/GLES; capture never waits for its work or HTTP clients."""
    def __init__(self,config,port,front_rotation,side_rotation,*,shared_input=True,speech_volume=None):
        if speech_volume is not None and (type(speech_volume)is not int or not -100<=speech_volume<=100):
            raise ValueError('speech volume must be an integer in -100..100')
        ctx=mp.get_context('spawn');self.inputs={v:SharedJpegSlot(ctx)if shared_input else ctx.Queue(maxsize=1)for v in ('front','side')}
        self.input_transport='shared_latest_jpeg'if shared_input else'queue'
        self.stopped=ctx.Event();self.result=ctx.Queue(maxsize=1);self.dropped=0
        self.process=ctx.Process(target=_run,args=(str(config),port,dict(front=front_rotation,side=side_rotation),self.inputs,self.stopped,self.result,speech_volume))
        self.process.start()

    def submit(self,view,frame_id,timestamp,payload,*,capture_epoch=None):
        q=self.inputs[view]
        record=(frame_id,timestamp,payload)if capture_epoch is None else(frame_id,timestamp,payload,capture_epoch)
        if isinstance(q,SharedJpegSlot):
            self.dropped+=q.publish((frame_id,timestamp,payload),capture_epoch=capture_epoch);return
        try:q.put_nowait(record)
        except queue.Full:
            self.dropped+=1
            try:q.get_nowait()
            except queue.Empty:return
            try:q.put_nowait(record)
            except queue.Full:pass

    def close(self):
        if self.process.is_alive():self.stopped.set()
        deadline=time.monotonic()+15
        # Drain completion diagnostics while the child can flush its queue.
        result=None
        while result is None and time.monotonic()<deadline:
            try:result=self.result.get(timeout=min(.1,max(0,deadline-time.monotonic())))
            except queue.Empty:
                if not self.process.is_alive():break
        self.process.join(timeout=max(0,deadline-time.monotonic()))
        if self.process.is_alive():self.process.terminate();self.process.join(timeout=7)
        if self.process.is_alive():self.process.kill();self.process.join(timeout=3)
        if result is None:
            result={'status':'failed','error':'Preview child did not produce completion diagnostics'}
        result['superseded_preview_inputs']=self.dropped;result['child_exit_code']=self.process.exitcode
        result['input_transport']=getattr(self,'input_transport','queue')
        for q in self.inputs.values():
            if isinstance(q,SharedJpegSlot):q.close()
            else:q.cancel_join_thread();q.close()
        self.result.cancel_join_thread();self.result.close()
        return result
