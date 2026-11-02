"""On-board Mali painter for the live 16-slot preview; imports are board-only."""
from pathlib import Path
import ctypes as c
from functools import lru_cache
import math
import sys
from time import monotonic_ns


AVATAR_VIEWPORT = (633, 161, 615, 448)


def prepare_normal_matrices(writes,rotation,*,normal_preparer=None,numpy_module=None):
    """Same float32 normal bases; optional batching retains target NumPy matmul."""
    if numpy_module is None:import numpy as numpy_module
    np=numpy_module
    # Materialize exactly once, including when the native domain is unsupported.
    rows=tuple(write.matrix.values for write in writes)
    cof=NotImplemented if normal_preparer is None else normal_preparer(rows)
    if cof is NotImplemented:
        bases=np.array(rows,np.float32).reshape(16,3,4)[:,:,:3]/16384
        a,b,d=bases[:,:,0],bases[:,:,1],bases[:,:,2]
        cof=np.stack((np.cross(b,d),np.cross(d,a),np.cross(a,b)),axis=2);det=(a*cof[:,:,0]).sum(axis=1)
        usable=np.abs(det)>1e-8;cof[usable]/=det[usable,None,None];cof[~usable]=np.eye(3,dtype=np.float32)
    return np.ascontiguousarray((rotation@cof).transpose(0,2,1))


def canvas_region(canvas, box):
    """Map a bottom-left GLES rectangle to a top-left CPU image view."""
    x,y,width,height=box
    top=canvas.shape[0]-y-height
    return canvas[top:top+height,x:x+width]


def compose_readback(canvas, bgr, box):
    result=canvas.copy()
    canvas_region(result,box)[...]=bgr
    return result


def fit_hdmi_box(box, screen_size):
    """Fit the logical 16:9 UI into a real screen, mapping shared edges together."""
    width,height=screen_size;scale=min(width/1280,height/720)
    full_width,full_height=round(1280*scale),round(720*scale)
    left,bottom=(width-full_width)//2,(height-full_height)//2
    x,y,w,h=box
    x0,y0=round(x*scale),round(y*scale)
    return left+x0,bottom+y0,round((x+w)*scale)-x0,round((y+h)*scale)-y0


def prepare_video_upload(image, rect, screen_size, *, native_size=False, cv2_module=None):
    """Optional display-only resize; borrowed source pixels are never modified."""
    if not native_size:return image
    x,y,width,height=rect
    _,_,width,height=fit_hdmi_box((x,720-y-height,width,height),screen_size)
    if min(width,height)<=0:raise ValueError('Native video viewport must have positive dimensions')
    if image.shape[:2]==(height,width):return image
    if cv2_module is None:import cv2 as cv2_module
    return cv2_module.resize(image,(width,height),interpolation=cv2_module.INTER_LINEAR)


def current_hdmi_size():
    import re,subprocess
    result=subprocess.run(['xrandr','--current'],check=True,capture_output=True,text=True)
    match=re.search(r'current (\d+) x (\d+)',result.stdout)
    if match is None:raise RuntimeError('Cannot read current HDMI screen size')
    return tuple(map(int,match.groups()))


def overlay_vertices(points, rect):
    from .skeleton_display import SKELETON_EDGES
    x,y,width,height=rect
    def vertex(point,color):
        px=x+round(point.x*width);py=y+round(point.y*height)
        return (px/640-1,1-py/360,*color)
    lines=[];dots=[]
    for a,b in SKELETON_EDGES:
        pa,pb=points.get(a),points.get(b)
        if pa is not None and pb is not None and min(pa.confidence,pb.confidence)>=.5:
            lines.extend((vertex(pa,(130/255,230/255,80/255)),vertex(pb,(130/255,230/255,80/255))))
    for point in points.values():
        if point.confidence>=.5:dots.append(vertex(point,(1,190/255,50/255)))
    return lines,dots


def make_painter(asset, *, hdmi=False, region_readback=True, msaa_samples=None, gpu_video=False,late_finish=False,video_native_size=False,swap_before_finish=False,cache_locations=False,normal_preparer=None,native_submit_library=None,avatar_yaw_degrees=30.):
    if type(avatar_yaw_degrees)not in (int,float)or not math.isfinite(avatar_yaw_degrees):
        raise ValueError('Avatar display yaw must be a finite angle in degrees')
    if native_submit_library is not None and not(hdmi and gpu_video):
        raise ValueError('Native GLES submit requires HDMI GPU video')
    if normal_preparer is not None and not callable(normal_preparer):
        raise TypeError('normal preparer must be callable')
    if type(cache_locations)is not bool:
        raise ValueError('Location cache requires a boolean option')
    if type(late_finish)is not bool or late_finish and not hdmi:
        raise ValueError('Late finish requires native HDMI and a boolean option')
    if type(video_native_size)is not bool or video_native_size and not(hdmi and gpu_video):
        raise ValueError('Native video size requires native HDMI, GPU video and a boolean option')
    if type(swap_before_finish)is not bool or swap_before_finish and not(hdmi and late_finish):
        raise ValueError('Swap before finish requires native HDMI, late finish and a boolean option')
    # Reuse the already validated GLES ABI/shader helpers, not the replay loop.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'board_tests/gles_avatar'))
    import benchmark as g
    import numpy as np
    import cv2
    from .preview_body_reference import BASE_LIGHT
    class Painter:
        def __init__(self):
            self.screen_size=current_hdmi_size()if hdmi else (1280,720)
            self.egl = g.EglSession(*self.screen_size,window=hdmi,msaa_samples=msaa_samples)
            self.presented_frames=0;self.gpu_video=gpu_video;self.bg_initialized=False;self.video_textures={}
            self.late_finish=late_finish;self._pending_present=False
            self.video_native_size=video_native_size;self.video_upload_sizes={}
            self.swap_before_finish=swap_before_finish;self._pending_swap=None
            if b'Mali' not in g.info(0x1F01):
                self.egl.close()
                raise RuntimeError('Live preview requires the actual Mali renderer')
            self.renderer = g.info(0x1F01).decode()
            # Linked locations belong to this context; uniform values still upload each draw.
            self._attribute=lru_cache(maxsize=32)(g.attribute)if cache_locations else g.attribute
            self._uniform=lru_cache(maxsize=32)(g.uniform)if cache_locations else g.uniform
            self.faces = np.asarray(asset.triangles, np.uint16)
            self.joints = np.asarray([v.matrix_indices for v in asset.vertices], np.uint8)
            self.weights = np.asarray([v.weights_q0_15 for v in asset.vertices], np.int32)
            self.zero_weights = self.weights==0
            self.last_validity = None
            self.index_count = self.faces.size
            colors = np.zeros((asset.vertex_count, 3), np.float64)
            counts = np.zeros(asset.vertex_count, np.int32)
            face_colors = np.asarray(asset.triangle_colors_rgb, np.float64)
            for lane in range(3):
                np.add.at(colors, self.faces[:, lane], face_colors)
                np.add.at(counts, self.faces[:, lane], 1)
            colors = np.ascontiguousarray(colors / np.maximum(counts[:, None], 1) / 255, np.float32)
            rest = np.array([(v.position.x, v.position.y, v.position.z) for v in asset.vertices], np.float64) / 16384
            area = np.cross(rest[self.faces[:,1]]-rest[self.faces[:,0]], rest[self.faces[:,2]]-rest[self.faces[:,0]])
            normals = np.zeros_like(rest)
            for lane in range(3): np.add.at(normals, self.faces[:,lane], area)
            normals = np.ascontiguousarray(normals / np.maximum(np.linalg.norm(normals,axis=1)[:,None],1e-12), np.float32)
            self.mesh = g.build_program('''attribute vec3 pos; attribute vec3 color; attribute vec3 normal;
                attribute vec4 joint; attribute vec4 weight; uniform mat4 transform; uniform mat3 bones[16];
                uniform vec3 light_direction; varying lowp vec3 col;
                void main(){gl_Position=transform*vec4(pos/16384.0,1.0);
                vec3 n=bones[int(joint.x)]*normal*weight.x;
                if(weight.y>0.0)n+=bones[int(joint.y)]*normal*weight.y;
                if(weight.z>0.0)n+=bones[int(joint.z)]*normal*weight.z;
                if(weight.w>0.0)n+=bones[int(joint.w)]*normal*weight.w;
                n=n/max(length(n),0.000001);
                col=color*pow(0.55+0.45*min(1.0,abs(dot(n,light_direction))),0.45454545);}''',
                'precision mediump float; varying lowp vec3 col; void main(){gl_FragColor=vec4(col,1.0);}')
            self.quad = g.build_program('attribute vec2 pos; attribute vec2 uv; varying vec2 tc; void main(){gl_Position=vec4(pos,0,1);tc=uv;}',
                'precision mediump float; varying vec2 tc; uniform sampler2D image; void main(){gl_FragColor=texture2D(image,tc);}')
            self.qv = g.buffer(np.array([[-1,1,0,0],[-1,-1,0,1],[1,1,1,0],[1,-1,1,1]],np.float32))
            if gpu_video:
                self.overlay_program=g.build_program(
                    'attribute vec2 pos; attribute vec3 color; uniform mediump vec3 controls; varying lowp vec3 col; void main(){gl_Position=vec4(pos,0,1);gl_PointSize=controls.x;col=color;}',
                    'precision mediump float; varying lowp vec3 col; uniform mediump vec3 controls; void main(){if(controls.y>0.5&&dot(gl_PointCoord-vec2(0.5),gl_PointCoord-vec2(0.5))>0.25)discard;gl_FragColor=vec4(col,1);}')
                self.overlay_vbo=g.buffer(np.zeros((64,5),np.float32),0x88E8)
                self.line_width=g.fn('glLineWidth',[g.F])
            self.pv = g.buffer(np.zeros((asset.vertex_count,8),np.int16),0x88E8)
            self.iv = g.buffer(self.faces,0x88E8)
            self.attrs = [(b'color',g.buffer(colors),3,0x1406),(b'normal',g.buffer(normals),3,0x1406),
                (b'joint',g.buffer(self.joints),4,0x1401),
                (b'weight',g.buffer(np.ascontiguousarray(self.weights/32768,np.float32)),4,0x1406)]
            self.avatar_yaw_degrees=float(avatar_yaw_degrees)
            yaw,pitch=np.deg2rad([avatar_yaw_degrees,-8])
            rotation=np.array([[np.cos(yaw),0,-np.sin(yaw)],[-np.sin(pitch)*np.sin(yaw),np.cos(pitch),-np.sin(pitch)*np.cos(yaw)],
                [np.cos(pitch)*np.sin(yaw),np.sin(pitch),np.cos(pitch)*np.cos(yaw)]],np.float32)
            self.rotation=rotation
            projection=np.eye(4,dtype=np.float32);projection[:3,:3]=np.diag([.82*448/615,.82,.4])@rotation
            self.projection=np.ascontiguousarray(projection.T)
            self.light=BASE_LIGHT
            g.pixel_store(0x0CF5,1)
            self.readback_box=AVATAR_VIEWPORT if region_readback and not hdmi and not gpu_video else (0,0,1280,720)
            _,_,width,height=self.readback_box
            self.bg=g.texture(np.zeros((height,width,4),np.uint8))
        def draw_quad(self, texture, box):
            g.bind_tex(0x0DE1,texture)
            g.viewport(*(fit_hdmi_box(box,self.screen_size)if hdmi else box))
            g.use(self.quad);g.bind_buffer(0x8892,self.qv)
            for name,offset in ((b'pos',0),(b'uv',8)):
                loc=self._attribute(self.quad,name);g.enable_attr(loc);g.vertex(loc,2,0x1406,0,16,offset)
            g.uniform_int(self._uniform(self.quad,b'image'),0);g.draw(5,0,4)
        def draw_points(self, points, rect):
            lines,dots=overlay_vertices(points,rect)
            g.viewport(*(fit_hdmi_box((0,0,1280,720),self.screen_size)if hdmi else (0,0,1280,720)))
            g.use(self.overlay_program);g.bind_buffer(0x8892,self.overlay_vbo)
            scale=min(self.screen_size[0]/1280,self.screen_size[1]/720)
            self.line_width(2*scale)
            for mode,vertices in ((1,lines),(0,dots)):
                if not vertices:continue
                data=np.ascontiguousarray(vertices,np.float32)
                g.buffer_data(0x8892,data.nbytes,data.ctypes.data,0x88E8)
                for name,size,offset in ((b'pos',2,0),(b'color',3,8)):
                    loc=self._attribute(self.overlay_program,name);g.enable_attr(loc);g.vertex(loc,size,0x1406,0,20,offset)
                g.uniform3(self._uniform(self.overlay_program,b'controls'),6*scale,float(mode==0),0)
                g.draw(mode,0,len(vertices))
        def show_status(self, canvas):
            """Native-only loading/stale UI; never count it as a fresh presentation."""
            if not hdmi:raise ValueError('Preview status screens require native HDMI')
            if self._pending_present:raise RuntimeError('Complete the pending frame before showing status')
            g.disable(0x0B71);g.clearcolor(0,0,0,1);g.clear(0x4000|0x0100)
            rgba=cv2.cvtColor(canvas,cv2.COLOR_BGR2RGBA)
            g.bind_tex(0x0DE1,self.bg)
            g.tex_sub(0x0DE1,0,0,0,1280,720,0x1908,0x1401,rgba.ctypes.data)
            self.draw_quad(self.bg,(0,0,1280,720));g.finish();self.egl.swap()
            self.bg_initialized=False
            if g.glerror():raise RuntimeError('Live GLES HDMI status presentation failed')
        def finish_present(self):
            """Confirm one submitted frame before its GL buffers can be reused."""
            if not self._pending_present:return None
            self._pending_present=False;started=monotonic_ns()
            swap_timing=self._pending_swap;self._pending_swap=None
            g.finish()
            if not self.swap_before_finish:self.egl.swap()
            if g.glerror():raise RuntimeError('Live GLES HDMI presentation failed')
            completed=monotonic_ns();self.presented_frames+=1
            completion=dict(completed_ns=completed,finish_swap_ns=completed-started)
            if swap_timing is not None:completion.update(swap_timing)
            return completion
        def draw(self, canvas, plan, outputs, display_transform, *, source_views=None, dirty_regions=()):
            from .preview_body_reference import compose_display_projection,display_light_direction
            if self._pending_present:raise RuntimeError('Complete the pending frame before reusing GL buffers')
            g.disable(0x0B71);g.clearcolor(0,0,0,1);g.clear(0x4000|0x0100)
            x,y,width,height=self.readback_box
            g.bind_tex(0x0DE1,self.bg)
            direct=self.gpu_video and source_views is not None
            if direct:
                regions=dirty_regions if self.bg_initialized else ((0,0,1280,720),)
                for xx,yy,w,h in regions:
                    rgba=cv2.cvtColor(canvas[yy:yy+h,xx:xx+w],cv2.COLOR_BGR2RGBA)
                    g.tex_sub(0x0DE1,0,xx,yy,w,h,0x1908,0x1401,rgba.ctypes.data)
                self.bg_initialized=True
            else:
                rgb=cv2.cvtColor(canvas_region(canvas,self.readback_box),cv2.COLOR_BGR2RGBA)
                g.tex_sub(0x0DE1,0,0,0,width,height,0x1908,0x1401,rgb.ctypes.data)
            self.draw_quad(self.bg,self.readback_box)
            if direct:
                for view,image,points,rect in source_views:
                    source_shape=image.shape[:2]
                    upload=prepare_video_upload(image,rect,self.screen_size,native_size=self.video_native_size,cv2_module=cv2)
                    rgba=cv2.cvtColor(upload,cv2.COLOR_BGR2RGBA)
                    shape=upload.shape[:2];entry=self.video_textures.get(view)
                    if entry is None:
                        texture=g.texture(np.zeros_like(rgba));self.video_textures[view]=(source_shape,shape,texture)
                    else:
                        previous_source,previous_shape,texture=entry
                        if previous_source!=source_shape:raise ValueError('Preview source dimensions changed within a session')
                        if previous_shape!=shape:raise ValueError('Preview upload dimensions changed within a session')
                    self.video_upload_sizes[view]=(shape[1],shape[0])
                    g.bind_tex(0x0DE1,texture)
                    g.tex_sub(0x0DE1,0,0,0,upload.shape[1],upload.shape[0],0x1908,0x1401,rgba.ctypes.data)
                    xx,yy,w,h=rect;self.draw_quad(texture,(xx,720-yy-h,w,h))
                    self.draw_points(points,rect)
            validity=tuple(plan.matrix_valid)
            if validity!=self.last_validity:
                valid=(np.asarray(validity)[self.joints]|self.zero_weights).all(axis=1)
                indices=np.ascontiguousarray(self.faces[valid[self.faces].all(axis=1)])
                g.bind_buffer(0x8893,self.iv);g.buffer_data(0x8893,indices.nbytes,indices.ctypes.data,0x88E8)
                self.index_count=indices.size
                self.last_validity=validity
            g.enable(0x0B71);g.viewport(*(fit_hdmi_box(AVATAR_VIEWPORT,self.screen_size)if hdmi else AVATAR_VIEWPORT));g.use(self.mesh)
            g.bind_buffer(0x8892,self.pv);g.buffer_sub(0x8892,0,len(outputs.padded_data),c.cast(c.c_char_p(outputs.padded_data),g.P))
            loc=self._attribute(self.mesh,b'pos');g.enable_attr(loc);g.vertex(loc,3,0x1402,0,16,None)
            for name,vbo,size,kind in self.attrs:
                loc=self._attribute(self.mesh,name);g.bind_buffer(0x8892,vbo);g.enable_attr(loc);g.vertex(loc,size,kind,0,0,None)
            matrices=prepare_normal_matrices(plan.matrix_writes,self.rotation,normal_preparer=normal_preparer,numpy_module=np)
            g.matrix3(self._uniform(self.mesh,b'bones[0]'),16,0,matrices.ctypes.data)
            light=display_light_direction(display_transform,self.light)
            projection=np.ascontiguousarray(np.array(compose_display_projection(tuple(float(v)for v in self.projection.T.flat),display_transform),np.float32).reshape(4,4).T)
            g.uniform3(self._uniform(self.mesh,b'light_direction'),*light)
            g.matrix(self._uniform(self.mesh,b'transform'),1,0,projection.ctypes.data)
            g.bind_buffer(0x8893,self.iv)
            g.draw_elements(4,self.index_count,0x1403,None)
            if hdmi:
                self._pending_present=True
                if self.late_finish:
                    if self.swap_before_finish:
                        swap_started=monotonic_ns();self.egl.swap();swap_returned=monotonic_ns()
                        self._pending_swap=dict(swap_return_ns=swap_returned,swap_host_ns=swap_returned-swap_started)
                    else:g.flush()
                    if g.glerror():raise RuntimeError('Live GLES HDMI submission failed')
                else:self.finish_present()
                return canvas,int(self.index_count//3)
            g.finish()
            pixels=np.empty((height,width,4),np.uint8);g.read(x,y,width,height,0x1908,0x1401,pixels.ctypes.data)
            if g.glerror():raise RuntimeError('Live GLES draw/readback failed')
            return compose_readback(canvas,cv2.cvtColor(np.flipud(pixels),cv2.COLOR_RGBA2BGR),self.readback_box),int(self.index_count//3)
        def close(self):
            try:self.egl.close()
            finally:
                if cache_locations:
                    self._attribute.cache_clear();self._uniform.cache_clear()
    painter=Painter()
    if native_submit_library is None:return painter
    try:
        from .native_gles_submit import NativeGlSubmit
        native=NativeGlSubmit(native_submit_library,painter,g,normal_preparer=normal_preparer)
    except BaseException:painter.close();raise
    original_close=painter.close
    def close():
        try:original_close()
        finally:native.close()
    painter.draw=native.draw;painter.close=close;painter.native_gl_submit=native
    return painter
