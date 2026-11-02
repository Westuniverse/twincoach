"""Explicit board experiment: PPS4 positions -> Mali full canvas and native replay."""
from pathlib import Path
import argparse,ctypes as c,json,sys,time,math
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
def recording_range(value):
    try:
        start,stop=map(int,value.split(':'))
        if not 0<=start<stop:raise ValueError
    except ValueError:raise argparse.ArgumentTypeError('record-range needs START:STOP with 0 <= START < STOP') from None
    return start,stop

def avatar_scale(value):
    try:scale=float(value)
    except ValueError:raise argparse.ArgumentTypeError('avatar-scale requires a finite value in [0.1,1.5]')from None
    if not math.isfinite(scale)or not .1<=scale<=1.5:
        raise argparse.ArgumentTypeError('avatar-scale requires a finite value in [0.1,1.5]')
    return scale

def build_parser():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record-video',action='store_true',help='offline MJPG export; wall rate includes readback/encoding')
    parser.add_argument('--record-backend',choices=('auto','opencv-mjpeg'),default='auto',help='offline AVI encoder; explicit OpenCV MJPEG uses quality100, different compression from default')
    parser.add_argument('--record-range',type=recording_range,help='record only global display-frame START:STOP; all preceding states still run')
    parser.add_argument('--msaa-samples',type=int,choices=(0,4,16),help='exact native-window sample count; omitted preserves EGL first-config selection')
    parser.add_argument('--late-finish',action='store_true',help='prepare the next current CPU frame before confirming the prior GPU frame; strict no-readback mode')
    parser.add_argument('--no-snapshots',action='store_true',help='omit diagnostic readbacks/PNGs during native generation')
    parser.add_argument('--package-root',type=Path,required=True)
    parser.add_argument('--expected-boot',required=True)
    parser.add_argument('--capture-dir',type=Path)
    parser.add_argument('--playlist',type=Path,help='private full recording list; paths relative to JSON')
    parser.add_argument('--trace',type=Path)
    parser.add_argument('--mesh',type=Path)
    parser.add_argument('--world-avatar-cache',type=Path,help='explicit image-only precomputed palettes; original scoring remains separate, excludes live model inference')
    parser.add_argument('--authored-rig-cache',type=Path,help='opt-in matching full authored mesh/cache: all-weight PPS4 positions and PS normals; world cache and lighting required')
    parser.add_argument('--native-palette-library',type=Path,help='explicit CPU C gather/all-weight blend library; authored-rig-cache only')
    parser.add_argument('--native-normal-library',type=Path,help='explicit CPU C all-weight normal blend; authored-rig-cache only, default NumPy retained')
    parser.add_argument('--authored-alias-rig',type=Path,help='opt-in symbolic exact matrix aliases for authored positions; full normals unchanged')
    parser.add_argument('--coherent-avatar',action='store_true',help='hide mesh if spine/neck/head is missing, keep original observations and scoring')
    parser.add_argument('--core-view-support',action='store_true',help='preview-only current side confidence supports plausible weak front shoulder/hip points')
    parser.add_argument('--native-affine',action='store_true',help='opt-in exact CPU-native Q1.14 affine composition; requires the explicit runtime library')
    parser.add_argument('--relative-arms',action='store_true',help='opt-in display-only 6Hz shoulder-relative smoothing of current complete arms; adds motion lag, original masks/scores retained')
    rig=parser.add_mutually_exclusive_group()
    rig.add_argument('--torso-heading',action='store_true',help='display-only core axes follow current shoulder heading; no independent face yaw')
    rig.add_argument('--rigid-avatar',action='store_true',help='complete fixed-proportion display rig; inferred/reference parts labelled, original pose/scoring retained')
    parser.add_argument('--rig-profile',type=Path,help='private schema1 shared-person display proportions; rigid-avatar only')
    parser.add_argument('--freeze-record-gaps',action='store_true',help='file playlist only: keep the avatar matching unchanged source images during a recorded gap; scoring remains stale')
    parser.add_argument('--avatar-scale',type=avatar_scale,default=.95,help='fixed orthographic model framing; default0.95 preserves existing view, smaller values leave room for raised hands')
    parser.add_argument('--native-video-resize-library',type=Path,help='opt-in CPU fused rotate/resize ABI1 library; original OpenCV path retained when omitted')
    parser.add_argument('--rgba-video',action='store_true',help='upload original video as opaque RGBA; same visible RGB, default RGB retained')
    parser.add_argument('--mesh-continuity',action='store_true',help='display-only 100ms anchored gap compensation')
    parser.add_argument('--limb-continuity',action='store_true',help='replay complete arm suffixes together for at most 100 source ms; mesh-continuity required')
    parser.add_argument('--depth-stability',action='store_true',help='preview-only bounded side-depth continuity')
    parser.add_argument('--stable-feedback',action='store_true',help='readable completed-score presentation; original scores unchanged')
    parser.add_argument('--read-ahead-video',action='store_true',help='bounded OS advice for upcoming MJPEG bytes; same current frame and pixels')
    parser.add_argument('--reuse-video-frames',action='store_true',help='reuse only the most recent identical source RGB for each view')
    parser.add_argument('--reuse-skinning',action='store_true',help='reuse one completed immutable output only when every skinning input is identical')
    parser.add_argument('--prefetch-images',action='store_true',help='prepare only the next recorded image pair while drawing the current frame; parallel-images required')
    parser.add_argument('--image-lookahead',type=int,choices=(1,2),default=1,help='bounded recorded-image pair lookahead; 2 requires prefetch-images, default 1 preserves original scheduling')
    parser.add_argument('--reuse-text-buffer',action='store_true',help='upload the current Chinese text layout only when it changes; score required')
    head_target=parser.add_mutually_exclusive_group()
    head_target.add_argument('--head-depth',action='store_true',help='display-only head uses trustworthy side depth rather than a flat neck plane')
    head_target.add_argument('--head-face-axis',action='store_true',help='display-only head pitch from current side ear/eye direction, separate from head-depth')
    parser.add_argument('--head-stability',action='store_true',help='display-only head tilt limit; may briefly lag rapid head motion')
    parser.add_argument('--palm-roll-stability',action='store_true',help='display-only continuous palm roll; may briefly lag the palm-down target')
    parser.add_argument('--score-process',action='store_true',help='overlap exact current-frame scoring in one owned child; playlist only')
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--frames',type=int);parser.add_argument('--reduced-jpeg',type=int,choices=(1,2,4),default=1);parser.add_argument('--parallel-images',action='store_true');parser.add_argument('--reuse-visibility',action='store_true');parser.add_argument('--opencv-threads',type=int);parser.add_argument('--lighting',action='store_true');parser.add_argument('--window',action='store_true');parser.add_argument('--score',action='store_true');parser.add_argument('--repeat',type=int,default=1);parser.add_argument('--replay-fps',type=float);parser.add_argument('--finish-every',type=int,choices=(1,2),default=1)
    return parser


# Parse a direct CLI before loading board-only EGL/GLES libraries.
_CLI_ARGS=build_parser().parse_args() if __name__=='__main__' else None

import numpy as np
import cv2
from egl_context import EglSession,api,gl,info,glerror,P,I,U,F,clear,clearcolor,finish,read

PROJECT_ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(PROJECT_ROOT/'src'),str(PROJECT_ROOT/'scripts')]
from posepriv_coach.avatar_setup import load_avatar_preview_setup
from posepriv_coach.native_skinning_validator import NativeSkinningValidator
from posepriv_coach.native_affine import NativeAffineComposer
from posepriv_coach.linux_shared_pose_dma import LinuxSharedPoseDma
from replay_sources import pose_chapter,load_playlist,chapter_report,ReplayScoring
from video_frames import IndexedPreviewImages
from skinning_reuse import LastSkinningOutput
from posepriv_coach.preview_visibility import PreviewCoreVisibility,CORE_MESSAGE_ZH,CORE_DETAIL_ZH
from posepriv_coach.generation_timing import analyze_generation_timing
from posepriv_coach.preview_body_reference import IDENTITY_4,BASE_LIGHT,compose_display_projection,display_light_direction
from posepriv_coach.recorded_avatar_freeze import RECORDED_HOLD_MESSAGE_ZH
from posepriv_coach.preview_core_support import CORE_SUPPORT_STATUS_ZH
from posepriv_coach.text_overlay import OptionalCjkTextOverlay,OverlayText,default_cjk_font
from verify_shared_pose_dma import load_board_manifest,pps4_config_matches
from world_cache import WorldAvatarCache,WORLD_DISPLAY_STATUS_ZH,WORLD_SCORE_SOURCE_ZH

def fn(name,args,result=None):return api(gl,name,args,result)
flush=fn('glFlush',[])
shader=fn('glCreateShader',[U],U);source=fn('glShaderSource',[U,I,c.POINTER(c.c_char_p),c.POINTER(I)])
compile_shader=fn('glCompileShader',[U]);shader_iv=fn('glGetShaderiv',[U,U,c.POINTER(I)])
shader_log=fn('glGetShaderInfoLog',[U,I,c.POINTER(I),P]);delete_shader=fn('glDeleteShader',[U])
program=fn('glCreateProgram',[],U);attach=fn('glAttachShader',[U,U]);link=fn('glLinkProgram',[U])
program_iv=fn('glGetProgramiv',[U,U,c.POINTER(I)]);program_log=fn('glGetProgramInfoLog',[U,I,c.POINTER(I),P])
use=fn('glUseProgram',[U]);attribute=fn('glGetAttribLocation',[U,c.c_char_p],I)
uniform=fn('glGetUniformLocation',[U,c.c_char_p],I);matrix=fn('glUniformMatrix4fv',[I,I,U,P])
matrix3=fn('glUniformMatrix3fv',[I,I,U,P]);uniform_int=fn('glUniform1i',[I,I]);viewport=fn('glViewport',[I,I,I,I])
uniform3=fn('glUniform3f',[I,F,F,F])
gen_buffer=fn('glGenBuffers',[I,c.POINTER(U)]);bind_buffer=fn('glBindBuffer',[U,U])
buffer_data=fn('glBufferData',[U,c.c_ssize_t,P,U]);buffer_sub=fn('glBufferSubData',[U,c.c_ssize_t,c.c_ssize_t,P])
vertex=fn('glVertexAttribPointer',[U,I,U,U,I,P]);enable_attr=fn('glEnableVertexAttribArray',[U])
draw_elements=fn('glDrawElements',[U,I,U,P]);draw=fn('glDrawArrays',[U,I,I]);enable=fn('glEnable',[U]);disable=fn('glDisable',[U])
gen_tex=fn('glGenTextures',[I,c.POINTER(U)]);bind_tex=fn('glBindTexture',[U,U])
tex_param=fn('glTexParameteri',[U,U,I]);pixel_store=fn('glPixelStorei',[U,I])
tex_image=fn('glTexImage2D',[U,I,I,I,I,I,U,U,P]);tex_sub=fn('glTexSubImage2D',[U,I,I,I,I,I,U,U,P])

def build_program(vs,fs):
    shaders=[];p=program()
    for kind,text in ((0x8B31,vs),(0x8B30,fs)):
        s=shader(kind);data=c.c_char_p(text.encode());source(s,1,c.byref(data),None);compile_shader(s)
        ok=I();shader_iv(s,0x8B81,c.byref(ok))
        if not ok.value:
            log=c.create_string_buffer(4096);shader_log(s,4096,None,log);raise RuntimeError(log.value)
        attach(p,s);shaders.append(s)
    link(p);ok=I();program_iv(p,0x8B82,c.byref(ok))
    if not ok.value:
        log=c.create_string_buffer(4096);program_log(p,4096,None,log);raise RuntimeError(log.value)
    for s in shaders:delete_shader(s)
    return p

def buffer(array,usage=0x88E4):
    out=U();gen_buffer(1,c.byref(out));bind_buffer(0x8892,out);buffer_data(0x8892,array.nbytes,array.ctypes.data,usage)
    return out.value

def texture(rgb):
    out=U();gen_tex(1,c.byref(out));bind_tex(0x0DE1,out)
    for name,value in ((0x2801,0x2601),(0x2800,0x2601),(0x2802,0x812F),(0x2803,0x812F)):tex_param(0x0DE1,name,value)
    fmt=0x1908 if rgb.shape[2]==4 else 0x1907
    tex_image(0x0DE1,0,fmt,rgb.shape[1],rgb.shape[0],0,fmt,0x1401,rgb.ctypes.data)
    return out.value

def main():
    parser=build_parser()
    args=_CLI_ARGS if _CLI_ARGS is not None else parser.parse_args()
    if args.record_range is not None and not args.record_video:
        parser.error('record-range requires record-video')
    if args.record_backend!='auto' and not args.record_video:
        parser.error('nondefault record-backend requires record-video')
    if args.msaa_samples is not None and not args.window:
        parser.error('msaa-samples requires window')
    if args.late_finish and (args.finish_every!=1 or not args.no_snapshots or args.record_video):
        parser.error("late-finish requires finish-every=1, no-snapshots and no recording")
    if args.prefetch_images and not args.parallel_images:
        parser.error('prefetch-images requires parallel-images')
    if args.image_lookahead==2 and not args.prefetch_images:
        parser.error('image-lookahead=2 requires prefetch-images')
    if args.reuse_text_buffer and not args.score:
        parser.error('reuse-text-buffer requires score')
    if args.limb_continuity and not args.mesh_continuity:
        parser.error('limb-continuity requires mesh-continuity')
    if args.rig_profile is not None and not args.rigid_avatar:parser.error('rig-profile requires rigid-avatar')
    if args.rigid_avatar and (args.rig_profile is None or not args.score):parser.error('rigid-avatar requires rig-profile and score/text layer')
    if args.freeze_record_gaps and (not args.rigid_avatar or not args.playlist):parser.error('freeze-record-gaps requires playlist and rigid-avatar')
    if args.world_avatar_cache:
        if not args.playlist:parser.error('world-avatar-cache requires playlist')
        unused=('rigid_avatar','torso_heading','relative_arms','mesh_continuity','limb_continuity',
                'depth_stability','core_view_support','head_depth','head_face_axis','head_stability',
                'palm_roll_stability','native_affine','freeze_record_gaps')
        if any(getattr(args,name)for name in unused):
            parser.error('world-avatar-cache cannot combine with pose geometry frontend options')
    if args.authored_rig_cache and (not args.world_avatar_cache or not args.lighting or not args.native_palette_library):
        parser.error('authored-rig-cache requires world-avatar-cache, lighting and native-palette-library')
    if args.native_palette_library and not args.authored_rig_cache:
        parser.error('native-palette-library requires authored-rig-cache')
    if args.native_normal_library and not args.authored_rig_cache:
        parser.error('native-normal-library requires authored-rig-cache')
    if args.authored_alias_rig and not args.authored_rig_cache:
        parser.error('authored-alias-rig requires authored-rig-cache')
    proportions=None
    if args.rigid_avatar:
        from posepriv_coach.preview_rigid_avatar import load_rig_proportions
        proportions=load_rig_proportions(args.rig_profile)
    root=args.package_root.resolve(strict=True)
    if args.repeat<=0 or (args.frames is not None and args.frames<=0) or (args.repeat>1 and not args.score):parser.error('positive frames/repeat; repeated chapters require score reset')
    if args.playlist:
        if args.capture_dir or args.trace or args.frames is not None or not args.score or not args.replay_fps:
            parser.error('playlist requires score/replay-fps and excludes capture-dir/trace/frames')
    elif args.capture_dir is None:parser.error('capture-dir required without playlist')
    if args.replay_fps is not None and (not math.isfinite(args.replay_fps) or args.replay_fps<=0):parser.error('positive finite replay fps required')
    if args.coherent_avatar and not args.score:parser.error('coherent-avatar requires the score/text layer')
    if args.stable_feedback and not args.score:parser.error('stable-feedback requires score')
    if args.score_process and (not args.score or not args.playlist):parser.error('score-process requires score and full playlist')
    if args.opencv_threads is not None:cv2.setNumThreads(args.opencv_threads)
    if args.record_video and not args.replay_fps:parser.error('record-video requires timestamp replay fps')
    args.output_dir.mkdir(parents=True)
    manifest=load_board_manifest(args.expected_boot,root/'runtime/unified_sd_candidate.json')
    assert pps4_config_matches(manifest['shared_pose_dma'])
    validator=NativeSkinningValidator(root/'runtime/libposepriv_skinning_validator.so')
    affine_composer=NativeAffineComposer(root/'runtime/libposepriv_affine.so') if args.native_affine else None
    chapters=load_playlist(args.playlist,fps=args.replay_fps) if args.playlist else (
        pose_chapter(args.trace or root/'data/private/complete-poses.jsonl',args.capture_dir,
                     title='录像姿态输入',fps=args.replay_fps,frames=args.frames or 120),)
    chapter_sequence=chapters*args.repeat
    work=[(chapter_id,local_id,sample) for chapter_id,chapter in enumerate(chapter_sequence)
          for local_id,sample in enumerate(chapter.samples)]
    if args.record_range is not None and args.record_range[1]>len(work):
        parser.error('record-range exceeds generated display frames')
    writer=None;recorded_frames=[];record_backend=None;record_quality=None;record_release_ns=0
    if args.record_video:
        backend=cv2.CAP_OPENCV_MJPEG if args.record_backend=='opencv-mjpeg' else cv2.CAP_ANY
        writer=cv2.VideoWriter(str(args.output_dir/'comparison.avi'),backend,cv2.VideoWriter_fourcc(*'MJPG'),args.replay_fps,(1280,720))
        if not writer.isOpened():raise RuntimeError('cannot create comparison AVI')
        record_backend=writer.getBackendName()
        if args.record_backend=='opencv-mjpeg':
            if (record_backend!='CV_MJPEG' or not writer.set(cv2.VIDEOWRITER_PROP_QUALITY,100)
                    or writer.get(cv2.VIDEOWRITER_PROP_QUALITY)!=100):
                writer.release();raise RuntimeError('cannot select OpenCV MJPEG quality100')
            record_quality=100
    backdrop=np.full((720,1280,3),(13,18,28),np.uint8)
    for x,width in ((24,583),(625,631)):
        cv2.rectangle(backdrop,(x,64),(x+width,566),(15,24,35),-1)
        cv2.rectangle(backdrop,(x,64),(x+width,566),(75,110,135),1)
    overlay=OptionalCjkTextOverlay(default_cjk_font(),numpy_module=np)
    overlay.draw_many(backdrop,[OverlayText('PosePriv 运动教练',28,14,28,(235,235,240)),
        OverlayText('原录像输入 · 同帧点位',960,20,19,(200,220,230)),
        OverlayText('真实视频输入',38,70,23,(230,235,240)),
        OverlayText('动作跟随数字人',640,70,23,(230,235,240))])
    if not args.score:
        overlay.draw_many(backdrop,[OverlayText('硬件绘制测试 · 未重新计算评分',28,600,22,(200,220,230))])
    backdrop=cv2.cvtColor(backdrop,cv2.COLOR_BGR2RGB)
    if args.score:cv2.rectangle(backdrop,(0,578),(1279,719),(28,18,13),-1)
    with ExitStack() as resources,LinuxSharedPoseDma(skinning_validator=validator) as dma,EglSession(1280,720,window=args.window,msaa_samples=args.msaa_samples) as egl:
        if b'Mali' not in info(0x1F01):raise RuntimeError('this board experiment requires actual Mali renderer')
        setup=load_avatar_preview_setup(mesh_path=args.mesh or root/'data/private/hoodie-glasses-v4.json',shared_dma=dma,skinning_validator=validator)
        asset=setup.mesh_asset;runtime=setup.runtime
        cache_started=time.perf_counter_ns()
        world_cache=WorldAvatarCache(args.world_avatar_cache,chapters,asset,runtime.vertex_requests)if args.world_avatar_cache else None
        image_work=([(ci,li,world_cache.samples[ci%len(chapters)][li])for ci,li,_ in work]
                    if world_cache is not None else work)
        cache_preparation_ns=time.perf_counter_ns()-cache_started if world_cache is not None else 0
        relative_arms=None
        if args.relative_arms:
            from posepriv_coach.preview_relative_arms import PreviewRelativeArms
            relative_arms=PreviewRelativeArms(runtime.planner.config.bone_links,
                runtime.bone_topology_indices,asset.bone_bind_lengths_q1_14,
                asset.inverse_bind_matrices_q1_14,affine_composer=affine_composer,
                palm_roll_stability=args.palm_roll_stability)
        torso_heading=rigid_avatar=None
        if args.torso_heading:
            from posepriv_coach.preview_torso_heading import PreviewTorsoHeading
            torso_heading=PreviewTorsoHeading(runtime.planner.config.bone_links,runtime.bone_topology_indices,
                asset.bone_bind_lengths_q1_14,asset.inverse_bind_matrices_q1_14,affine_composer=affine_composer)
        if args.rigid_avatar:
            from posepriv_coach.preview_rigid_avatar import PreviewRigidAvatar
            rigid_avatar=PreviewRigidAvatar(asset,runtime.planner.config.bone_links,runtime.bone_topology_indices,
                proportions=proportions,affine_composer=affine_composer)
        recorded_freeze=None
        if args.freeze_record_gaps:
            from posepriv_coach.recorded_avatar_freeze import RecordedAvatarFreeze
            recorded_freeze=RecordedAvatarFreeze()
        head_slot=asset.bone_names.index('head') if 'head' in asset.bone_names else None
        core_guard=PreviewCoreVisibility(asset.bone_names) if args.coherent_avatar else None
        chapter_streams={id(chapter):{v:resources.enter_context((chapter.capture_dir/f'{v}.mjpeg').open('rb')) for v in chapter.views} for chapter in chapters}
        continuity=None;limb_continuity=None
        if args.mesh_continuity:
            from posepriv_coach.preview_continuity import PreviewMeshContinuity
            continuity=PreviewMeshContinuity([runtime.planner.config.bone_links[i] for i in runtime.bone_topology_indices])
            if args.limb_continuity:
                from posepriv_coach.preview_limb_continuity import PreviewLimbContinuity
                limb_continuity=PreviewLimbContinuity(continuity.slot_links)
        faces=np.asarray(asset.triangles,dtype=np.int32)
        face_colors=np.asarray(asset.triangle_colors_rgb,dtype=np.float64)
        color_sum=np.zeros((asset.vertex_count,3),np.float64);color_counts=np.zeros(asset.vertex_count,np.int32)
        for lane in range(3):
            np.add.at(color_sum,faces[:,lane],face_colors);np.add.at(color_counts,faces[:,lane],1)
        colors=np.ascontiguousarray(color_sum/np.maximum(color_counts[:,None],1)/255.,dtype=np.float32)
        joints=np.asarray([v.matrix_indices for v in asset.vertices],np.int32)
        weights=np.asarray([v.weights_q0_15 for v in asset.vertices],np.int32)
        vertex_source='attribute vec3 pos; attribute vec3 color; uniform mat4 transform; varying lowp vec3 col; void main(){gl_Position=transform*vec4(pos/16384.0,1.0);col=color*0.9;}'
        if args.lighting:
            vertex_source='''attribute vec3 pos; attribute vec3 color; attribute vec3 normal;
              attribute vec4 joint; attribute vec4 weight; uniform mat4 transform;
              uniform mat3 bones[16]; uniform vec3 light_direction; varying lowp vec3 col;
              void main(){
                gl_Position=transform*vec4(pos/16384.0,1.0);
                vec3 n=bones[int(joint.x)]*normal*weight.x;
                if(weight.y>0.0)n+=bones[int(joint.y)]*normal*weight.y;
                if(weight.z>0.0)n+=bones[int(joint.z)]*normal*weight.z;
                if(weight.w>0.0)n+=bones[int(joint.w)]*normal*weight.w;
                n=n/max(length(n),0.000001);
                float d=min(1.0,abs(dot(n,light_direction)));
                col=color*pow(0.55+0.45*d,0.45454545);
              }'''
            if args.authored_rig_cache:
                vertex_source='''attribute vec3 pos; attribute vec3 color; attribute vec3 normal;
                  uniform mat4 transform; uniform mat3 normal_view;
                  uniform vec3 light_direction; varying lowp vec3 col;
                  void main(){
                    gl_Position=transform*vec4(pos/16384.0,1.0);
                    vec3 n=normal_view*normal;n=n/max(length(n),0.000001);
                    float d=min(1.0,abs(dot(n,light_direction)));
                    col=color*pow(0.55+0.45*d,0.45454545);
                  }'''
        mesh_program=build_program(vertex_source,
            'precision mediump float; varying lowp vec3 col; void main(){gl_FragColor=vec4(col,1.0);}')
        quad_program=build_program('attribute vec2 pos; attribute vec2 uv; varying mediump vec2 tex; void main(){gl_Position=vec4(pos,0.0,1.0);tex=uv;}',
            'precision mediump float; uniform sampler2D image; varying mediump vec2 tex; void main(){gl_FragColor=texture2D(image,tex);}')
        quad=np.array([[-1,1,0,0],[-1,-1,0,1],[1,1,1,0],[1,-1,1,1]],np.float32)
        qv=buffer(quad);cv=buffer(colors);position_buffers=[buffer(np.zeros((asset.vertex_count,8),np.int16),0x88E8) for _ in range(args.finish_every)];index_buffers=[buffer(faces.astype(np.uint16),0x88E8) for _ in range(args.finish_every)];uploaded_masks=[None]*args.finish_every
        authored=None;authored_preparation_ns=0
        if args.lighting:
            rest=np.array([(v.position.x,v.position.y,v.position.z) for v in asset.vertices],np.float64)/16384.
            area=np.cross(rest[faces[:,1]]-rest[faces[:,0]],rest[faces[:,2]]-rest[faces[:,0]])
            normals=np.zeros_like(rest)
            for lane in range(3):np.add.at(normals,faces[:,lane],area)
            normals=np.ascontiguousarray(normals/np.maximum(np.linalg.norm(normals,axis=1)[:,None],1.e-12),np.float32)
            mn=attribute(mesh_program,b'normal')
            if args.authored_rig_cache:
                from authored_replay import AuthoredReplay
                authored_started=time.perf_counter_ns()
                authored=AuthoredReplay(args.authored_rig_cache,world_cache,asset,normals,native_library=args.native_palette_library,native_normal_library=args.native_normal_library,alias_rig=args.authored_alias_rig)
                authored_preparation_ns=time.perf_counter_ns()-authored_started
                normal_buffers=[buffer(np.zeros_like(normals),0x88E8)for _ in range(args.finish_every)]
            else:
                nv=buffer(normals);jv=buffer(np.ascontiguousarray(joints,np.uint8));wv=buffer(np.ascontiguousarray(weights/32768.,np.float32))
                mj,mw=(attribute(mesh_program,name) for name in (b'joint',b'weight'))
        pixel_store(0x0CF5,1)
        bg=texture(backdrop);video_channels=4 if args.rgba_video else 3
        video_format=0x1908 if args.rgba_video else 0x1907
        video_buffers=[[texture(np.zeros((450,253,video_channels),np.uint8)) for _ in range(2)] for _ in range(args.finish_every)]
        qa,qu=attribute(quad_program,b'pos'),attribute(quad_program,b'uv')
        ma,mc=attribute(mesh_program,b'pos'),attribute(mesh_program,b'color')
        def draw_texture(tex,rect):
            x,y,w,h=rect;viewport(x,720-y-h,w,h);use(quad_program);bind_tex(0x0DE1,tex)
            bind_buffer(0x8892,qv);enable_attr(qa);vertex(qa,2,0x1406,0,16,None)
            enable_attr(qu);vertex(qu,2,0x1406,0,16,8);uniform_int(uniform(quad_program,b'image'),0);draw(5,0,4)
        yaw,pitch=math.radians(30),math.radians(-8)
        rotation=np.array([[math.cos(yaw),0,-math.sin(yaw),0],
            [-math.sin(pitch)*math.sin(yaw),math.cos(pitch),-math.sin(pitch)*math.cos(yaw),0],
            [math.cos(pitch)*math.sin(yaw),math.sin(pitch),math.cos(pitch)*math.cos(yaw),0],[0,0,0,1]],np.float32)
        transform=np.ascontiguousarray((np.diag([args.avatar_scale*448/615,args.avatar_scale,.4,1]).astype(np.float32)@rotation).T)
        if authored is not None:normal_view=np.ascontiguousarray(rotation[:3,:3].T)
        base_projection=tuple(float(v)for v in transform.T.flat)
        cached_display=None;cached_projection=None;cached_light=None
        light_location=uniform(mesh_program,b'light_direction')if args.lighting else None
        image_reader=IndexedPreviewImages(chapter_streams,reduced_jpeg=args.reduced_jpeg,reuse=args.reuse_video_frames,read_ahead=args.read_ahead_video,rgba=args.rgba_video,native_resize_library=args.native_video_resize_library)
        resources.callback(image_reader.close)
        prepare_image=image_reader.get
        if args.score:
            from gles_text import GpuText
            from posepriv_coach.display import completed_score_text
            from posepriv_coach.pose_avatar_preview import PoseAvatarPreviewPlanner
            text_files=[root/'src/posepriv_coach'/name for name in ('display.py','feedback_zh.py','session.py')]
            if args.coherent_avatar:text_files.append(root/'src/posepriv_coach/preview_visibility.py')
            if args.stable_feedback:text_files.append(root/'src/posepriv_coach/preview_presentation.py')
            if args.core_view_support:text_files.append(root/'src/posepriv_coach/preview_core_support.py')
            text_files+=list((root/'config/exercises').glob('*.json'))
            characters='0123456789'+''.join(path.read_text() for path in text_files)+'录像姿态输入实际硬件生成暂无 FPS动作阶段已完成次目标帧每秒上次得分采集原片未分析与评分正面文件缺失' + ''.join(chapter.title for chapter in chapters)
            if args.rigid_avatar:characters+='当前身体追踪不足数字人显示参考姿态局部追踪不稳'+RECORDED_HOLD_MESSAGE_ZH
            if world_cache is not None:characters+=WORLD_DISPLAY_STATUS_ZH+WORLD_SCORE_SOURCE_ZH+RECORDED_HOLD_MESSAGE_ZH+'参考姿态'
            text_renderer=GpuText(default_cjk_font(),characters,build_program,reuse_buffer=args.reuse_text_buffer)
            score_changes=[]
            scorer=ReplayScoring(root/'config/exercises/squat_depth_angle_front_order_v1.json')
            score_process=None
            if args.score_process:
                from score_process import ScoreProcess
                score_process=resources.enter_context(ScoreProcess(args.playlist,root/'config/exercises/squat_depth_angle_front_order_v1.json',fps=args.replay_fps))
            if args.stable_feedback:
                from posepriv_coach.preview_presentation import PreviewScorePresenter
                presenter=PreviewScorePresenter()
            def present_score(state,frame,chapter_id,sample):
                presentation_state=None
                if args.stable_feedback:
                    presentation_state=presenter.update(state,timestamp_ns=sample.display_timestamp_ns,
                        observation_timestamp_ns=sample.observation_ready_ns,chapter_id=chapter_id,analyzed=sample.pair is not None)
                if sample.pair is not None:
                    score_key=(state.completed_repetition_count,completed_score_text(state))
                    if not score_changes or score_changes[-1]['chapter']!=chapter_id or tuple(score_changes[-1]['value'])!=score_key:
                        score_changes.append(dict(frame=frame,chapter=chapter_id,value=score_key))
                return presentation_state
        pool=ThreadPoolExecutor(max_workers=2)
        resources.callback(pool.shutdown)
        skinning_cache=LastSkinningOutput() if args.reuse_skinning else None
        lookahead=None
        if args.image_lookahead==2:
            from image_lookahead import ImageLookahead
            lookahead=ImageLookahead(prepare_image,image_work,chapter_sequence)
            resources.callback(lookahead.close)
        pending_images=None
        completion_events=[]
        timings=[];saved=[];pending_snapshots=[];previous_mask=None;visible_indices=None;visibility_reuses=0;playback_started=time.perf_counter_ns();active_chapter=-1
        for n,(chapter_id,local_id,sample) in enumerate(work):
            chapter=chapter_sequence[chapter_id];pair=sample.pair
            image_sample=image_work[n][2]
            if chapter_id!=active_chapter:
                from posepriv_coach.pose_avatar_preview import PoseAvatarPreviewPlanner
                runtime.planner=PoseAvatarPreviewPlanner(hand_bones=True,depth_stability=args.depth_stability,palm_roll_stability=args.palm_roll_stability,head_stability=args.head_stability,head_depth=args.head_depth,core_view_support=args.core_view_support,head_face_axis=args.head_face_axis,affine_composer=affine_composer)
                if continuity is not None:continuity.reset()
                if limb_continuity is not None:limb_continuity.reset()
                if skinning_cache is not None:skinning_cache.reset()
                if authored is not None:authored.reset()
                if relative_arms is not None:relative_arms.reset()
                if torso_heading is not None:torso_heading.reset()
                if rigid_avatar is not None:rigid_avatar.reset()
                if recorded_freeze is not None:recorded_freeze.reset()
                previous_mask=None;uploaded_masks[:]=[None]*args.finish_every
                active_chapter=chapter_id
            if args.replay_fps:
                remaining=(playback_started+round(n*1e9/args.replay_fps)-time.perf_counter_ns())/1e9
                if remaining>0:time.sleep(remaining)
            start=time.perf_counter_ns()
            image_futures=lookahead.begin(n) if lookahead is not None else pending_images
            pending_images=None
            if args.parallel_images and image_futures is None:
                image_futures=[pool.submit(prepare_image,v,chapter,image_sample) for v in ('front','side')]
            if args.score:
                if args.score_process:
                    score_process.submit(chapter_id,chapter_id%len(chapters),local_id)
                else:
                    state=scorer.update(chapter_id,chapter,sample)
                    presentation_state=present_score(state,n,chapter_id,sample)
            slot=n%args.finish_every;pv=position_buffers[slot];iv=index_buffers[slot];videos=video_buffers[slot]
            score_ready=time.perf_counter_ns()
            held_slots=();observed_valid=();render_valid=();triangle_count=0;visibility_changed=False;missing_core=()
            head_matrix=();head_target_error=None
            head_face_reason=None;head_face_sides=();head_face_age=None;head_face_pitch=None
            core_supported=();core_support_reason=None;core_support_age=None
            limb_held=();limb_suppressed=();limb_ages=();limb_reasons=()
            relative_applied=();relative_rejected=();relative_depth_resets=()
            torso_reason=None;torso_yaw=None;torso_target=None;torso_age=None
            rig_reason=None;rig_inferred=();rig_scale=None;rig_head=None;rig_errors=()
            rig_display_transform=None;rig_display_scale=None
            rig_source_reason=None;rig_recorded_hold_age=None
            skinning_reused=False
            authored_normal_ns=authored_position_ns=0
            plan=None;world_source=None
            planned=skinned=time.perf_counter_ns()
            if world_cache is not None:
                rig=world_cache.frame(chapter_id%len(chapters),local_id)
                if rig is not None:
                    plan=rig.render_plan;world_source=plan.source
                    rig_reason=rig_source_reason=rig.reason;rig_inferred=rig.inferred_slots
                    rig_scale=rig.body_scale;rig_head=rig.head_mode
                    rig_display_transform=rig.display_transform;rig_display_scale=rig.display_body_scale
            elif pair is not None:
                plan=runtime.planner.build(pair,vertex_requests=runtime.vertex_requests,
                    bone_bind_lengths_q1_14=asset.bone_bind_lengths_q1_14,inverse_bind_matrices_q1_14=asset.inverse_bind_matrices_q1_14,
                    bone_topology_indices=runtime.bone_topology_indices,display_timestamp_ns=sample.display_timestamp_ns)
                support=runtime.planner.last_core_support
                if support is not None:core_supported=support.names;core_support_reason=support.reason;core_support_age=support.source_age_ns
                face=runtime.planner.last_head_face_axis
                if face is not None:head_face_reason=face.reason;head_face_sides=face.sides;head_face_age=face.source_age_ns;head_face_pitch=face.pitch_rad
                held_slots=()
                observed_valid=plan.matrix_valid
                observed_plan=plan
                if continuity is not None:
                    presentation=continuity.apply(plan)
                    plan=presentation.render_plan;held_slots=presentation.held_slots
                    if limb_continuity is not None:
                        limb=limb_continuity.apply(presentation,display_timestamp_ns=sample.display_timestamp_ns)
                        plan=limb.render_plan;held_slots=limb.held_slots
                        limb_held=limb.chain_held_slots;limb_suppressed=limb.suppressed_slots
                        limb_ages=limb.hold_ages_ns;limb_reasons=limb.reasons
                if relative_arms is not None:
                    relative=relative_arms.apply(observed_plan,plan,axes=runtime.planner.bone_axes)
                    plan=relative.render_plan
                    relative_applied=relative.applied_sides;relative_rejected=relative.rejected_sides
                    relative_depth_resets=relative.depth_reset_sides
                if torso_heading is not None:
                    torso=torso_heading.apply(observed_plan,plan,display_timestamp_ns=sample.display_timestamp_ns)
                    plan=torso.render_plan;torso_reason=torso.reason;torso_yaw=torso.yaw_radians
                    torso_target=torso.target_yaw_radians;torso_age=torso.heading_age_ns
                if rigid_avatar is not None:
                    rig=rigid_avatar.apply(observed_plan,plan,display_timestamp_ns=sample.display_timestamp_ns)
                    rig_source_reason=rig.reason
                    if recorded_freeze is not None:
                        selected=recorded_freeze.apply(rig,source_ids={v:r.frame_id for v,r in sample.rows.items()},display_timestamp_ns=sample.display_timestamp_ns)
                        rig=selected.preview;rig_recorded_hold_age=selected.frozen_source_age_ns
                    plan=rig.render_plan;rig_reason=rig.reason;rig_inferred=rig.inferred_slots
                    rig_scale=rig.body_scale;rig_head=rig.head_mode;rig_errors=rig.endpoint_errors
                    rig_display_transform=getattr(rig,'display_transform',IDENTITY_4)
                    rig_display_scale=getattr(rig,'display_body_scale',rig.body_scale)
            if plan is not None:
                planned=time.perf_counter_ns()
                if authored is not None:
                    outputs,authored_normals,skinning_reused,authored_normal_ns,authored_position_ns=authored.execute(
                        chapter_id%len(chapters),local_id,transfer=dma.exchange_skinning_packet,validator=validator,reuse=args.reuse_skinning)
                elif skinning_cache is None:
                    outputs=plan.execute(runtime.backend,program_matrices=runtime.backend.program_matrices)
                else:
                    outputs,skinning_reused=skinning_cache.execute(plan,runtime.backend)
                skinned=time.perf_counter_ns()
                visibility_changed=not args.reuse_visibility or plan.matrix_valid!=previous_mask
                if visibility_changed:
                    valid=(np.asarray(plan.matrix_valid)[joints]|(weights==0)).all(axis=1)
                    selected=valid[faces].all(axis=1)
                    visible_indices=np.ascontiguousarray(faces[selected],dtype=np.uint16)
                    visible_indices.flags.writeable=False
                    previous_mask=plan.matrix_valid
                else:visibility_reuses+=1
                if args.lighting and authored is None:
                    bases=np.array([w.matrix.values for w in plan.matrix_writes],np.float32).reshape(16,3,4)[:,:,:3]/16384.
                    c0,c1,c2=bases[:,:,0],bases[:,:,1],bases[:,:,2]
                    co0,co1,co2=np.cross(c1,c2),np.cross(c2,c0),np.cross(c0,c1)
                    det=(c0*co0).sum(axis=1)
                    cofactor=np.stack((co0,co1,co2),axis=2)
                    usable=np.abs(det)>1.e-8
                    cofactor[usable]/=det[usable,None,None];cofactor[~usable]=np.eye(3,dtype=np.float32)
                    normal_matrices=np.ascontiguousarray((rotation[:3,:3]@cofactor).transpose(0,2,1))
                render_valid=plan.matrix_valid;triangle_count=visible_indices.size//3
                if head_slot is not None and render_valid[head_slot]:head_matrix=plan.matrix_writes[head_slot].matrix.values
                if world_cache is None and runtime.planner.head_tracker is not None:head_target_error=runtime.planner.head_tracker.last_target_error_rad
                if core_guard is not None:missing_core=core_guard.missing(render_valid)
            mesh_ready=time.perf_counter_ns();images=[];image_timings=[]
            values=[f.result() for f in image_futures] if image_futures is not None else [prepare_image(v,chapter,image_sample) for v in ('front','side')]
            images=[value[0] for value in values];image_timings=[value[1] for value in values]
            images_ready=time.perf_counter_ns()
            if lookahead is not None:
                lookahead.consumed()
            elif args.prefetch_images and n+1<len(work):
                next_chapter_id,_,next_sample=image_work[n+1]
                next_chapter=chapter_sequence[next_chapter_id]
                pending_images=[pool.submit(prepare_image,v,next_chapter,next_sample) for v in ('front','side')]
            late_score_start=time.perf_counter_ns()
            score_wait_ns=score_worker_ns=score_roundtrip_ns=score_process_rss_kib=0
            if args.score_process:
                state=score_process.receive()
                presentation_state=present_score(state,n,chapter_id,sample)
                score_wait_ns=score_process.last_receive_wait_ns
                score_worker_ns=score_process.last_worker_ns
                score_roundtrip_ns=score_process.last_roundtrip_ns
                score_process_rss_kib=score_process.last_max_rss_kib
            display_transform=rig_display_transform or IDENTITY_4
            if display_transform==IDENTITY_4:draw_projection=transform;draw_light=BASE_LIGHT
            else:
                if display_transform!=cached_display:
                    cached_projection=np.ascontiguousarray(np.array(compose_display_projection(base_projection,display_transform),np.float32).reshape(4,4).T)
                    cached_light=display_light_direction(display_transform);cached_display=display_transform
                draw_projection=cached_projection;draw_light=cached_light
            prepared=time.perf_counter_ns()
            late_score_ns=prepared-late_score_start if args.score_process else 0
            previous_gpu_finish_wait_ns=0
            if args.late_finish and n>0:
                previous_finish_start=time.perf_counter_ns();finish();drained_at=time.perf_counter_ns()
                previous_gpu_finish_wait_ns=drained_at-previous_finish_start
                egl.swap();confirmed=time.perf_counter_ns()
                completion_events.append(dict(frame_index=n-1,completed_count=n,drained_ns=drained_at,finished_ns=confirmed))
            gpu_commands_started=time.perf_counter_ns()
            disable(0x0B71);clearcolor(0,0,0,1);clear(0x4000|0x0100);draw_texture(bg,(0,0,1280,720))
            for tex,image,x in zip(videos,images,(49,327)):
                bind_tex(0x0DE1,tex);tex_sub(0x0DE1,0,0,0,253,450,video_format,0x1401,image.ctypes.data)
                draw_texture(tex,(x,112,253,450))
            if plan is not None and not missing_core:
                enable(0x0B71);viewport(633,161,615,448);use(mesh_program)
                bind_buffer(0x8892,pv);buffer_sub(0x8892,0,len(outputs.padded_data),c.cast(c.c_char_p(outputs.padded_data),P))
                enable_attr(ma);vertex(ma,3,0x1402,0,16,None)
                bind_buffer(0x8892,cv)
                enable_attr(mc);vertex(mc,3,0x1406,0,0,None)
                if args.lighting:
                    if authored is not None:
                        bind_buffer(0x8892,normal_buffers[slot]);buffer_sub(0x8892,0,authored_normals.nbytes,authored_normals.ctypes.data)
                        enable_attr(mn);vertex(mn,3,0x1406,0,0,None)
                        matrix3(uniform(mesh_program,b'normal_view'),1,0,normal_view.ctypes.data)
                    else:
                        for location,vbo,size,kind in ((mn,nv,3,0x1406),(mj,jv,4,0x1401),(mw,wv,4,0x1406)):
                            bind_buffer(0x8892,vbo);enable_attr(location);vertex(location,size,kind,0,0,None)
                        matrix3(uniform(mesh_program,b'bones[0]'),16,0,normal_matrices.ctypes.data)
                    uniform3(light_location,*draw_light)
                matrix(uniform(mesh_program,b'transform'),1,0,draw_projection.ctypes.data)
                bind_buffer(0x8893,iv)
                if uploaded_masks[slot]!=plan.matrix_valid:
                    buffer_data(0x8893,visible_indices.nbytes,visible_indices.ctypes.data,0x88E8);uploaded_masks[slot]=plan.matrix_valid
                draw_elements(4,visible_indices.size,0x1403,None)
            text_ns=0
            if args.score:
                if args.stable_feedback:
                    lines=[(line,28,y,color) for line,y,color in zip(presentation_state.lines_zh,
                        (580,623,666),((230,235,240),(180,220,210),(150,185,205)))]
                    lines.append((chapter.title,640,112,(200,220,230)))
                    if core_supported:lines[-1]=(CORE_SUPPORT_STATUS_ZH,640,112,(180,205,235))
                    if pair is None and plan is None:
                        lines.append(('未分析与评分',810,300,(150,185,205)))
                        if 'front' not in chapter.views:lines.append(('正面文件缺失',100,300,(150,185,205)))
                elif pair is not None:
                    lines=[
                        (f'阶段：{state.phase_zh}    已完成 {state.completed_repetition_count} 次    上次得分：'+completed_score_text(state),28,580,(230,235,240)),
                        (state.process_hint_zh or state.status_zh,28,623,(180,220,210)),
                        (chapter.title+' · 实际硬件生成 · 目标 15 FPS',28,666,(150,185,205))]
                else:
                    lines=[(chapter.title,28,580,(230,235,240)),
                           ('采集原片 · 未分析与评分',28,623,(180,220,210)),
                           ('正面文件缺失' if 'front' not in chapter.views else '原录像输入',28,666,(150,185,205))]
                    if plan is None:lines.append(('未分析与评分',810,300,(150,185,205)))
                if world_cache is not None and plan is not None:
                    if pair is not None:lines.append((WORLD_SCORE_SOURCE_ZH,650,139,(180,205,225)))
                    lines.append((RECORDED_HOLD_MESSAGE_ZH if rig_reason=='recorded_hold'else WORLD_DISPLAY_STATUS_ZH,650,540,(180,205,225)))
                elif pair is not None and missing_core:
                    lines.extend(((CORE_MESSAGE_ZH,775,290,(190,215,225)),(CORE_DETAIL_ZH,800,332,(150,185,205))))
                elif rig_reason in ('neutral','held_body'):
                    lines.append(('参考姿态',650,540,(190,215,225)))
                elif rig_reason=='recorded_hold':
                    lines.append((RECORDED_HOLD_MESSAGE_ZH,650,540,(190,215,225)))
                elif rig_inferred:
                    lines.append(('局部参考',650,540,(180,205,225)))
                text_started=time.perf_counter_ns()
                text_renderer.draw_lines(lines)
                text_ns=time.perf_counter_ns()-text_started
            gpu_submitted=time.perf_counter_ns()
            snapshot=not args.no_snapshots and n in (0,len(work)//2,len(work)-1)
            record_frame=args.record_video and (args.record_range is None or args.record_range[0]<=n<args.record_range[1])
            capture_pixels=record_frame or snapshot
            drained=(n==len(work)-1 if args.late_finish else (n+1)%args.finish_every==0 or n==len(work)-1 or capture_pixels)
            if drained:
                finish()
                if args.late_finish:drained_at=time.perf_counter_ns()
            pixels=None
            if capture_pixels:
                pixels=np.empty((720,1280,4),np.uint8)
                read(0,0,1280,720,0x1908,0x1401,pixels.ctypes.data)
            if args.late_finish:
                if drained:
                    egl.swap()
                    completion_events.append(dict(frame_index=n,completed_count=n+1,drained_ns=drained_at,finished_ns=time.perf_counter_ns()))
                else:flush()
            else:egl.swap()
            end=time.perf_counter_ns();assert glerror()==0
            timings.append(dict(gpu_commands_started_ns=gpu_commands_started,gpu_submitted_ns=gpu_submitted,previous_gpu_finish_wait_ns=previous_gpu_finish_wait_ns,finished_ns=end,started_ns=start,gpu_drained=drained,completed_count=n+1 if drained else None,frame_id=image_sample.rows['front'].frame_id if 'front' in image_sample.rows else None,source_timestamp_ns=sample.timestamp_ns,
                chapter=chapter_id,chapter_kind=chapter.kind,local_frame=local_id,source_ids={v:r.frame_id for v,r in image_sample.rows.items()},
                score_source_ids=({v:getattr(pair,v).frame_id for v in ('front','side')}if pair is not None else None),
                world_avatar_source=(dict(view=world_source.view,frame_id=world_source.frame_id,timestamp_ns=world_source.timestamp_ns)if world_source is not None else None),
                display_timestamp_ns=sample.display_timestamp_ns,
                observation_ready_ns=sample.observation_ready_ns,
                score_ns=score_ready-start,score_wait_ns=score_wait_ns,score_worker_ns=score_worker_ns,score_roundtrip_ns=score_roundtrip_ns,late_score_ns=late_score_ns,score_process_rss_kib=score_process_rss_kib,plan_ns=planned-score_ready,skin_ns=skinned-planned,prepare_and_decode_ns=prepared-skinned,
                gpu_upload_draw_finish_ns=end-prepared,total_ns=end-start,triangles=triangle_count if not missing_core else 0,planned_triangles=triangle_count,
                avatar_visible=plan is not None and not missing_core and triangle_count>0,missing_core_bones=missing_core,visibility_changed=visibility_changed,mesh_rows_ns=mesh_ready-skinned,image_timings=image_timings,
                observed_matrix_valid=observed_valid,render_matrix_valid=render_valid,held_slots=held_slots,
                head_matrix_q1_14=head_matrix,head_target_error_rad=head_target_error,
                head_face_axis_reason=head_face_reason,head_face_axis_sides=head_face_sides,head_face_axis_age_ns=head_face_age,head_face_axis_pitch_rad=head_face_pitch,
                core_supported_joints=core_supported,core_support_reason=core_support_reason,core_support_age_ns=core_support_age,
                limb_held_slots=limb_held,limb_suppressed_slots=limb_suppressed,limb_hold_ages_ns=limb_ages,limb_reasons=limb_reasons,
                relative_arm_applied_sides=relative_applied,relative_arm_rejected_sides=relative_rejected,relative_arm_depth_reset_sides=relative_depth_resets,
                torso_heading_reason=torso_reason,torso_heading_yaw=torso_yaw,torso_heading_target=torso_target,torso_heading_age_ns=torso_age,
                rig_reason=rig_reason,rig_inferred_slots=rig_inferred,rig_body_scale=rig_scale,rig_head_mode=rig_head,rig_endpoint_errors=rig_errors,
                rig_display_transform=rig_display_transform,rig_display_body_scale=rig_display_scale,
                rig_source_reason=rig_source_reason,rig_recorded_hold_age_ns=rig_recorded_hold_age,
                skinning_reused=skinning_reused,image_wait_ns=images_ready-mesh_ready,text_ns=text_ns,
                video_convert_ns=0,video_encode_ns=0))
            if authored is not None:
                timings[-1].update(authored_normal_ns=authored_normal_ns,authored_position_ns=authored_position_ns,
                    authored_matrix_frame=local_id,authored_global_bones=len(authored.names))
            if pixels is not None:
                convert_started=time.perf_counter_ns()
                bgr=cv2.flip(pixels[:,:,2::-1],0)
                timings[-1]['video_convert_ns']=time.perf_counter_ns()-convert_started
                if snapshot:pending_snapshots.append((n,bgr))
                if record_frame:
                    encode_started=time.perf_counter_ns();writer.write(bgr)
                    timings[-1]['video_encode_ns']=time.perf_counter_ns()-encode_started
                    recorded_frames.append(n)
        if writer is not None:
            release_started=time.perf_counter_ns();writer.release()
            record_release_ns=time.perf_counter_ns()-release_started
        pool.shutdown();setup.close()
        snapshot_encode_start=time.perf_counter_ns()
        for n,bgr in pending_snapshots:
            if not cv2.imwrite(str(args.output_dir/f'frame-{n}.png'),bgr):raise OSError('cannot write diagnostic PNG')
            saved.append(n)
        snapshot_encode_ns=time.perf_counter_ns()-snapshot_encode_start
        warm=timings[8:];drains=[t for t in warm if t['gpu_drained']];result=dict(scope='actual PPS4 skinning + Mali complete 1280x720 generation; includes JPEG decode, points, GPU vertex lighting and optional real scoring/text/window swap; excludes new CNN inference; no pre-rendered avatar frames. '+('Offline recording: completion/wall rates include interframe MJPG encoding.' if args.record_video else 'No video encoder in this run.'),
            gl_renderer=info(0x1F01).decode(),frames=len(timings),warmup_frames=8,reduced_jpeg=args.reduced_jpeg,parallel_images=args.parallel_images,indexed_q14_upload=True,reuse_visibility=args.reuse_visibility,visibility_reuses=visibility_reuses,opencv_threads=cv2.getNumThreads(),lighting=args.lighting,window=args.window,score=args.score,repeat=args.repeat,record_video=args.record_video,replay_fps=args.replay_fps,finish_every=args.finish_every,chapters=[chapter_report(chapter) for chapter in chapter_sequence],raw_pairs_each_chapter=len(chapters[0].raw_pairs) if len(chapters)==1 else None,rendered_frames_each_chapter=len(chapters[0].samples) if len(chapters)==1 else None,score_changes=score_changes if args.score else None,missing_glyphs=sorted(text_renderer.missing) if args.score else None,
            mesh_continuity=args.mesh_continuity,coherent_avatar=args.coherent_avatar,
            depth_stability=args.depth_stability,stable_feedback=args.stable_feedback,palm_roll_stability=args.palm_roll_stability,head_stability=args.head_stability,head_depth=args.head_depth,head_face_axis=args.head_face_axis,
            native_affine=args.native_affine,
            relative_arms=args.relative_arms,
            rgba_video=args.rgba_video,
            native_video_resize_library=(str(args.native_video_resize_library) if args.native_video_resize_library else None),
            score_process=args.score_process,score_timing_scope=('score_ns is parent dispatch; score_worker_ns overlaps parent geometry/images; score_wait_ns/late_score_ns are inside prepare_and_decode_ns' if args.score_process else 'score_ns is synchronous original scoring and presentation before geometry'),
            read_ahead_video=args.read_ahead_video,read_ahead_advice_requests=(image_reader.read_ahead.requests if image_reader.read_ahead is not None else 0),
            reuse_video_frames=args.reuse_video_frames,reused_image_frames=sum(image.get('cache_hit',False) for t in timings for image in t['image_timings']),
            snapshots_enabled=not args.no_snapshots,snapshot_encoding='after_generation_loop',post_render_snapshot_encoding_ns=snapshot_encode_ns,
            completion_rate_includes_recording_encoding=args.record_video,work_rate_excludes_interframe_encoding_and_pacing=True,
            gpu_completed_fps=((drains[-1]['completed_count']-drains[0]['completed_count'])*1e9/(drains[-1]['finished_ns']-drains[0]['finished_ns']) if len(drains)>1 else None),generated_fps=len(warm)*1e9/sum(t['total_ns'] for t in warm) if warm else None,wall_finished_fps=(len(warm)-1)*1e9/(warm[-1]['finished_ns']-warm[0]['finished_ns']) if len(warm)>1 else None,timings=timings,saved_frames=saved)
        result['late_finish']=args.late_finish
        result['world_avatar_cache']=args.world_avatar_cache is not None
        if authored is not None:
            result['authored_rig']=dict(bones=len(authored.names),vertices=authored.plan.vertex_count,
                local_packets=len(authored.plan.palettes.batches),preparation_ns=authored_preparation_ns,
                normal_backend='cpu_c'if authored.native_normals is not None else'numpy',
                position_matrix_count=authored.plan.palettes.bone_count,wire_vertices=authored.plan.palettes.vertex_count,
                matrix_aliases=authored.aliases is not None,
                normal_scope='PS all-weight inverse-transpose blend; Mali normalizes and lights; not exact LBS surface derivative',
                source_state_D_and_driver_bytes_exact=True)
        result['world_cache_preparation_ns']=cache_preparation_ns
        if world_cache is not None:result['scope']+=' Image-only poses are precomputed; cache preparation is separate; original scoring uses its own paired records, not these inferred points.'
        result['core_view_support']=args.core_view_support
        result['record_range']=args.record_range
        result['record_backend_requested']=args.record_backend
        result['record_backend_actual']=record_backend
        result['record_quality']=record_quality
        result['record_release_ns']=record_release_ns
        result['recorded_frame_indices']=recorded_frames
        result['encoded_video_frames']=len(recorded_frames)
        result['egl_config']=egl.config_info
        result['msaa_samples_requested']=args.msaa_samples
        result['gl_samples_actual']=egl.samples_actual
        result['limb_continuity']=args.limb_continuity
        result['reuse_skinning']=args.reuse_skinning
        result['skinning_reused_frames']=sum(t['skinning_reused'] for t in timings)
        result['prefetch_images']=args.prefetch_images
        result['image_lookahead']=args.image_lookahead
        result['relative_arm_palm_roll_stability']=args.relative_arms and args.palm_roll_stability
        result['torso_heading']=args.torso_heading
        result['rigid_avatar']=args.rigid_avatar
        result['freeze_record_gaps']=args.freeze_record_gaps
        result['avatar_scale']=args.avatar_scale
        result['rig_proportions']=proportions
        result['reuse_text_buffer']=args.reuse_text_buffer
        result['text_buffer_uploads']=text_renderer.buffer_uploads if args.score else 0
        result['image_timing_scope']='worker stages may precede current loop with prefetch; image_wait_ns measures current result collection; do not sum overlapping stages'
        if args.late_finish:result['host_loop_semantics']='current CPU/PL preparation and GL submission; prior frame drain/swap before current GL commands; completion events include matching swap return, not scanout'
        if args.late_finish:
            result['gpu_completion_event_schema']='posepriv.gles.completed_frame.v1'
            result['gpu_completion_events']=completion_events
        if len(timings)>8:
            certified=analyze_generation_timing(result)
            result['gpu_completed_fps']=certified['gpu_completion']['fps']
            for chapter in certified['chapter_completion']['runs']:
                result['chapters'][chapter['chapter']]['gpu_completed_fps']=chapter['gpu_completed_fps']
        else:
            for chapter in result['chapters']:chapter['gpu_completed_fps']=None
        pose_rates=[row['gpu_completed_fps'] for row in result['chapters'] if row['kind']=='pose_trace' and row['gpu_completed_fps'] is not None]
        result['minimum_pose_chapter_gpu_completed_fps']=min(pose_rates) if pose_rates else None
        result['throughput_scope']='overall rate mixes raw and pose chapters; evaluate individual pose chapter completion rates and frame pacing against the requested target'
        (args.output_dir/'report.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({k:v for k,v in result.items() if k not in ('timings','gpu_completion_events')},indent=2))

if __name__=='__main__':main()
