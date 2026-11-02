"""Prototype single-font glyph atlas; drawing does not rasterize fonts per frame."""
import ctypes as c
import math
import numpy as np
from PIL import Image,ImageDraw,ImageFont
from egl_context import api,gl,P,I,U

def fn(name,args,result=None):return api(gl,name,args,result)

class GpuText:
    def __init__(self,font_path,characters,build_program,*,reuse_buffer=False):
        chars=sorted(set(ch for ch in characters+'0123456789□\u3000' if ch.isprintable() or ch=='\u3000'))
        self.font=ImageFont.truetype(str(font_path),22)
        width=1024;height=math.ceil(len(chars)/32)*36
        atlas=Image.new('L',(width,height));painter=ImageDraw.Draw(atlas)
        self.glyphs={};self.missing=set();self.key=None;self.lines=None
        self.reuse_buffer=reuse_buffer;self.buffer_uploads=0
        self.geometry=None;self.ranges=();self.pending_upload=False
        for index,char in enumerate(chars):
            x,y=index%32*32,index//32*36
            painter.text((x+2,y),char,font=self.font,fill=255)
            self.glyphs[char]=(x/width,y/height,(x+32)/width,(y+36)/height,self.font.getlength(char))
        data=np.asarray(atlas)
        self.program=build_program('attribute vec2 pos; attribute vec2 uv; varying mediump vec2 tex; void main(){gl_Position=vec4(pos,0.0,1.0);tex=uv;}',
            'precision mediump float; uniform sampler2D image; uniform vec3 color; varying mediump vec2 tex; void main(){gl_FragColor=vec4(color,texture2D(image,tex).r);}')
        self.bind_tex=fn('glBindTexture',[U,U]);self.use=fn('glUseProgram',[U])
        self.bind_buffer=fn('glBindBuffer',[U,U]);self.data=fn('glBufferData',[U,c.c_ssize_t,P,U])
        self.vertex=fn('glVertexAttribPointer',[U,I,U,U,I,P]);self.enable_attr=fn('glEnableVertexAttribArray',[U])
        self.draw=fn('glDrawArrays',[U,I,I]);self.uniform_int=fn('glUniform1i',[I,I])
        self.color=fn('glUniform3f',[I,c.c_float,c.c_float,c.c_float]);self.viewport=fn('glViewport',[I,I,I,I])
        self.enable=fn('glEnable',[U]);self.disable=fn('glDisable',[U]);self.blend=fn('glBlendFunc',[U,U])
        self.location=fn('glGetUniformLocation',[U,c.c_char_p],I)
        attr=fn('glGetAttribLocation',[U,c.c_char_p],I)
        self.pos,self.uv=(attr(self.program,k) for k in (b'pos',b'uv'))
        create=fn('glGenTextures',[I,c.POINTER(U)]);texture=U();create(1,c.byref(texture));self.texture=texture.value
        self.bind_tex(0x0DE1,self.texture)
        parameter=fn('glTexParameteri',[U,U,I])
        for key,value in ((0x2800,0x2601),(0x2801,0x2601),(0x2802,0x812F),(0x2803,0x812F)):parameter(0x0DE1,key,value)
        fn('glTexImage2D',[U,I,I,I,I,I,U,U,P])(0x0DE1,0,0x1909,width,height,0,0x1909,0x1401,data.ctypes.data)
        buffer=U();fn('glGenBuffers',[I,c.POINTER(U)])(1,c.byref(buffer));self.buffer=buffer.value

    def draw_lines(self,lines):
        # Cache only the current strings; no growing text or pose history.
        key=tuple(lines)
        if key!=self.key:
            self.lines=[]
            for text,x,y,color in lines:
                vertices=[]
                for ch in text:
                    if ch not in self.glyphs:self.missing.add(ch)
                    u,v,du,dv,advance=self.glyphs.get(ch,self.glyphs['□'])
                    left,right=x/640-1,(x+32)/640-1
                    top,bottom=1-y/360,1-(y+36)/360
                    vertices.extend(((left,top,u,v),(left,bottom,u,dv),(right,top,du,v),
                                     (right,top,du,v),(left,bottom,u,dv),(right,bottom,du,dv)))
                    x+=advance
                self.lines.append((np.asarray(vertices,dtype=np.float32).reshape(-1,4),color))
            self.key=key
            if self.reuse_buffer:
                self.geometry=np.concatenate([values for values,_ in self.lines]) if self.lines else np.empty((0,4),np.float32)
                ranges=[];first=0
                for values,color in self.lines:
                    if len(values):ranges.append((first,len(values),color))
                    first+=len(values)
                self.ranges=tuple(ranges);self.pending_upload=True
        self.disable(0x0B71);self.enable(0x0BE2);self.blend(0x0302,0x0303)
        self.viewport(0,0,1280,720);self.use(self.program);self.bind_tex(0x0DE1,self.texture)
        self.uniform_int(self.location(self.program,b'image'),0)
        self.bind_buffer(0x8892,self.buffer)
        self.enable_attr(self.pos);self.enable_attr(self.uv)
        if self.reuse_buffer:
            if self.pending_upload:
                if self.geometry.nbytes:
                    self.data(0x8892,self.geometry.nbytes,self.geometry.ctypes.data,0x88E8)
                    self.buffer_uploads+=1
                self.pending_upload=False
            self.vertex(self.pos,2,0x1406,0,16,None);self.vertex(self.uv,2,0x1406,0,16,8)
            for first,count,color in self.ranges:
                self.color(self.location(self.program,b'color'),*(channel/255 for channel in color))
                self.draw(4,first,count)
        else:
            for values,color in self.lines:
                if not len(values):continue
                self.data(0x8892,values.nbytes,values.ctypes.data,0x88E8);self.buffer_uploads+=1
                self.vertex(self.pos,2,0x1406,0,16,None);self.vertex(self.uv,2,0x1406,0,16,8)
                self.color(self.location(self.program,b'color'),*(channel/255 for channel in color))
                self.draw(4,0,len(values))
        self.disable(0x0BE2)
