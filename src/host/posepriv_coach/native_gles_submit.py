# SPDX-License-Identifier: GPL-2.0-only
"""Single CDLL GLES submission; Python retains original immutable frame buffers."""
import ctypes as c
from time import monotonic_ns
import numpy as np
import cv2
from .field_preview_gles import (AVATAR_VIEWPORT,fit_hdmi_box,
    prepare_video_upload,overlay_vertices,prepare_normal_matrices)
from .preview_body_reference import compose_display_projection,display_light_direction

U=c.c_uint32;I=c.c_int;P=c.c_void_p;F=c.c_float;S=c.c_ssize_t


class Attribute(c.Structure):
    _fields_=[('buffer',U),('location',I),('size',I),('type',U)]
class Config(c.Structure):
    _fields_=[(k,U)for k in ('mesh','quad','overlay','qv','pv','iv','overlay_vbo','bg')]+\
        [(k,I)for k in ('quad_pos','quad_uv','quad_image','overlay_pos','overlay_color','overlay_controls',
                       'mesh_pos','mesh_bones','mesh_light','mesh_transform')]+\
        [('attributes',Attribute*4),('full_viewport',I*4),('avatar_viewport',I*4),('background_viewport',I*4),
         ('line_width',F),('point_size',F)]
class Upload(c.Structure):
    _fields_=[('texture',U),('x',I),('y',I),('width',I),('height',I),('pixels',P)]
class Video(c.Structure):
    _fields_=[('upload',Upload),('viewport',I*4),('lines',P),('dots',P),('line_count',I),('dot_count',I)]
class Frame(c.Structure):
    _fields_=[('backgrounds',c.POINTER(Upload)),('background_count',U),('videos',c.POINTER(Video)),('video_count',U),
        ('skin',P),('skin_bytes',S),('indices',P),('index_bytes',S),('index_count',I),('index_update',I),
        ('bones',P),('light',P),('transform',P),('reserved_upload_before_draw',I),('flush',I)]

API_SIGNATURES=(
 ('BindTexture',(U,U)),('Viewport',(I,I,I,I)),('UseProgram',(U,)),('BindBuffer',(U,U)),
 ('EnableVertexAttribArray',(U,)),('VertexAttribPointer',(U,I,U,c.c_uint8,I,P)),
 ('Uniform1i',(I,I)),('DrawArrays',(U,I,I)),('BufferData',(U,S,P,U)),
 ('Uniform3f',(I,F,F,F)),('LineWidth',(F,)),('Disable',(U,)),('Enable',(U,)),
 ('ClearColor',(F,F,F,F)),('Clear',(U,)),('TexSubImage2D',(U,I,I,I,I,I,U,U,P)),
 ('BufferSubData',(U,S,S,P)),('UniformMatrix3fv',(I,I,c.c_uint8,P)),
 ('UniformMatrix4fv',(I,I,c.c_uint8,P)),('DrawElements',(U,I,U,P)),('Flush',()))
class CallbackApi(c.Structure):
    _fields_=[(name,P)for name,_ in API_SIGNATURES]


class NativeGlSubmit:
    def __init__(self,library,painter,g,*,normal_preparer=None,callbacks=None):
        self.p=painter;self.g=g;self.normal_preparer=normal_preparer;self.callbacks=callbacks
        self.library=c.CDLL(str(library));lib=self.library
        lib.posepriv_gl_abi.restype=U
        if lib.posepriv_gl_abi()!=1:raise ValueError('unsupported native GLES submit ABI')
        lib.posepriv_gl_create.argtypes=[c.c_char_p];lib.posepriv_gl_create.restype=P
        lib.posepriv_gl_create_callbacks.argtypes=[c.POINTER(CallbackApi)];lib.posepriv_gl_create_callbacks.restype=P
        lib.posepriv_gl_error.restype=c.c_char_p
        self.context=(lib.posepriv_gl_create(b'libGLESv2.so.2')if callbacks is None else
                      lib.posepriv_gl_create_callbacks(c.byref(callbacks)))
        if not self.context:raise RuntimeError('native GLES binding failed: '+str(lib.posepriv_gl_error()))
        lib.posepriv_gl_destroy.argtypes=[P];lib.posepriv_gl_destroy.restype=None
        self.call=lib.posepriv_gl_submit;self.call.argtypes=[P,c.POINTER(Config),c.POINTER(Frame)];self.call.restype=None
        p=painter;cfg=Config()
        for key in ('mesh','quad','qv','pv','iv','bg'):setattr(cfg,key,getattr(p,key))
        cfg.overlay=p.overlay_program;cfg.overlay_vbo=p.overlay_vbo
        for key,program,name in (
            ('quad_pos',p.quad,b'pos'),('quad_uv',p.quad,b'uv'),('overlay_pos',p.overlay_program,b'pos'),
            ('overlay_color',p.overlay_program,b'color'),('mesh_pos',p.mesh,b'pos')):
            setattr(cfg,key,p._attribute(program,name))
        for key,program,name in (
            ('quad_image',p.quad,b'image'),('overlay_controls',p.overlay_program,b'controls'),
            ('mesh_bones',p.mesh,b'bones[0]'),('mesh_light',p.mesh,b'light_direction'),('mesh_transform',p.mesh,b'transform')):
            setattr(cfg,key,p._uniform(program,name))
        for index,(name,buffer,size,kind)in enumerate(p.attrs):cfg.attributes[index]=Attribute(buffer,p._attribute(p.mesh,name),size,kind)
        cfg.full_viewport[:]=fit_hdmi_box((0,0,1280,720),p.screen_size)
        cfg.avatar_viewport[:]=fit_hdmi_box(AVATAR_VIEWPORT,p.screen_size)
        cfg.background_viewport[:]=fit_hdmi_box(p.readback_box,p.screen_size)
        scale=min(p.screen_size[0]/1280,p.screen_size[1]/720);cfg.line_width=2*scale;cfg.point_size=6*scale
        self.config=cfg;self.calls=0

    def draw(self,canvas,plan,outputs,display_transform,*,source_views=None,dirty_regions=()):
        p=self.p;g=self.g
        if p._pending_present:raise RuntimeError('Complete the pending frame before reusing GL buffers')
        owners=[canvas,plan,outputs,display_transform,source_views];backgrounds=[];videos=[]
        direct=source_views is not None
        regions=(dirty_regions if p.bg_initialized else((0,0,1280,720),))if direct else((0,0,1280,720),)
        for x,y,w,h in regions:
            rgba=cv2.cvtColor(canvas[y:y+h,x:x+w],cv2.COLOR_BGR2RGBA);owners.append(rgba)
            backgrounds.append(Upload(p.bg,x,y,w,h,rgba.ctypes.data))
        if direct:p.bg_initialized=True
        for view,image,points,rect in source_views or():
            source_shape=image.shape[:2]
            upload=prepare_video_upload(image,rect,p.screen_size,native_size=p.video_native_size,cv2_module=cv2)
            rgba=cv2.cvtColor(upload,cv2.COLOR_BGR2RGBA);owners.extend((image,upload,rgba))
            shape=upload.shape[:2];entry=p.video_textures.get(view)
            if entry is None:
                texture=g.texture(np.zeros_like(rgba));p.video_textures[view]=(source_shape,shape,texture)
            else:
                previous_source,previous_shape,texture=entry
                if previous_source!=source_shape:raise ValueError('Preview source dimensions changed within a session')
                if previous_shape!=shape:raise ValueError('Preview upload dimensions changed within a session')
            p.video_upload_sizes[view]=(shape[1],shape[0])
            lines,dots=overlay_vertices(points,rect)
            line_data=np.ascontiguousarray(lines,np.float32);dot_data=np.ascontiguousarray(dots,np.float32)
            owners.extend((line_data,dot_data))
            x,y,w,h=rect;video=Video()
            video.upload=Upload(texture,0,0,shape[1],shape[0],rgba.ctypes.data)
            video.viewport[:]=fit_hdmi_box((x,720-y-h,w,h),p.screen_size)
            video.lines=line_data.ctypes.data;video.dots=dot_data.ctypes.data
            video.line_count=len(lines);video.dot_count=len(dots);videos.append(video)
        frame=Frame();bg_array=(Upload*len(backgrounds))(*backgrounds);video_array=(Video*len(videos))(*videos)
        owners.extend((bg_array,video_array))
        frame.backgrounds=bg_array;frame.background_count=len(backgrounds);frame.videos=video_array;frame.video_count=len(videos)
        validity=tuple(plan.matrix_valid)
        if validity!=p.last_validity:
            valid=(np.asarray(validity)[p.joints]|p.zero_weights).all(axis=1)
            indices=np.ascontiguousarray(p.faces[valid[p.faces].all(axis=1)])
            owners.append(indices);frame.indices=indices.ctypes.data;frame.index_bytes=indices.nbytes;frame.index_update=1
            p.index_count=indices.size;p.last_validity=validity
        frame.index_count=p.index_count
        skin=c.c_char_p(outputs.padded_data);owners.extend((outputs.padded_data,skin))
        frame.skin=c.cast(skin,P);frame.skin_bytes=len(outputs.padded_data)
        bones=prepare_normal_matrices(plan.matrix_writes,p.rotation,normal_preparer=self.normal_preparer,numpy_module=np)
        light=np.ascontiguousarray(display_light_direction(display_transform,p.light),np.float32)
        transform=np.ascontiguousarray(np.array(compose_display_projection(tuple(float(v)for v in p.projection.T.flat),display_transform),np.float32).reshape(4,4).T)
        owners.extend((bones,light,transform));frame.bones=bones.ctypes.data;frame.light=light.ctypes.data;frame.transform=transform.ctypes.data
        frame.reserved_upload_before_draw=0  # ABI1 reserved slot; original interleaved draw order only.
        frame.flush=p.late_finish and not p.swap_before_finish
        self.call(self.context,c.byref(self.config),c.byref(frame));self.calls+=1
        p._pending_present=True
        if p.late_finish:
            if p.swap_before_finish:
                started=monotonic_ns();p.egl.swap();returned=monotonic_ns()
                p._pending_swap=dict(swap_return_ns=returned,swap_host_ns=returned-started)
            if g.glerror():raise RuntimeError('Live GLES HDMI submission failed')
        else:p.finish_present()
        return canvas,int(p.index_count//3)

    def close(self):
        if self.context:self.library.posepriv_gl_destroy(self.context);self.context=None
