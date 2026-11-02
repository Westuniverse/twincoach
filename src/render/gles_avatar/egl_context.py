import ctypes as c
import json,time
P=c.c_void_p;I=c.c_int;U=c.c_uint;F=c.c_float
lib=c.CDLL('/usr/lib/aarch64-linux-gnu/libEGL.so.1')
gl=c.CDLL('/usr/lib/aarch64-linux-gnu/libGLESv2.so.2')
def api(l,name,args,result):
 f=getattr(l,name);f.argtypes=args;f.restype=result;return f
get=api(lib,'eglGetDisplay',[P],P);init=api(lib,'eglInitialize',[P,c.POINTER(I),c.POINTER(I)],U)
error=api(lib,'eglGetError',[],I);query=api(lib,'eglQueryString',[P,I],c.c_char_p)
choose=api(lib,'eglChooseConfig',[P,c.POINTER(I),c.POINTER(P),I,c.POINTER(I)],U)
bind=api(lib,'eglBindAPI',[U],U)
surface=api(lib,'eglCreatePbufferSurface',[P,P,c.POINTER(I)],P)
context=api(lib,'eglCreateContext',[P,P,P,c.POINTER(I)],P)
current=api(lib,'eglMakeCurrent',[P,P,P,P],U)
destroy_context=api(lib,'eglDestroyContext',[P,P],U)
destroy_surface=api(lib,'eglDestroySurface',[P,P],U)
terminate=api(lib,'eglTerminate',[P],U)
info=api(gl,'glGetString',[U],c.c_char_p)
clearcolor=api(gl,'glClearColor',[F,F,F,F],None);clear=api(gl,'glClear',[U],None)
finish=api(gl,'glFinish',[],None);read=api(gl,'glReadPixels',[I,I,I,I,U,U,P],None)
glerror=api(gl,'glGetError',[],U)
def checked(value,name):
 if not value:raise RuntimeError(name+' EGL error '+hex(error()))
 return value

class XAttributes(c.Structure):
 _fields_=[('background_pixmap',c.c_ulong),('background_pixel',c.c_ulong),('border_pixmap',c.c_ulong),('border_pixel',c.c_ulong),('bit_gravity',I),('win_gravity',I),('backing_store',I),('backing_planes',c.c_ulong),('backing_pixel',c.c_ulong),('save_under',I),('event_mask',c.c_long),('do_not_propagate_mask',c.c_long),('override_redirect',I),('colormap',c.c_ulong),('cursor',c.c_ulong)]
class XVisualInfo(c.Structure):
 _fields_=[('visual',P),('visualid',c.c_ulong),('screen',I),('depth',I),('visual_class',I),('red_mask',c.c_ulong),('green_mask',c.c_ulong),('blue_mask',c.c_ulong),('colormap_size',I),('bits_per_rgb',I)]
class EglSession:
 def __init__(self,width,height,window=False,*,msaa_samples=None):
  if msaa_samples is not None and (not window or type(msaa_samples) is not int or msaa_samples not in (0,4,16)):
   raise ValueError('explicit MSAA requires a window and samples 0, 4 or 16')
  self.surf=self.ctx=self.xdisplay=self.window=self.colormap=None
  self._closed=False
  self.window_mode=window
  if window:
   self.xlib=c.CDLL('/usr/lib/aarch64-linux-gnu/libX11.so.6')
   self.xopen=api(self.xlib,'XOpenDisplay',[c.c_char_p],P)
   self.xclose=api(self.xlib,'XCloseDisplay',[P],I)
   self.xroot=api(self.xlib,'XDefaultRootWindow',[P],c.c_ulong)
   self.xscreen=api(self.xlib,'XDefaultScreen',[P],I)
   self.xvisual=api(self.xlib,'XDefaultVisual',[P,I],P)
   self.xvisual_id=api(self.xlib,'XVisualIDFromVisual',[P],c.c_ulong)
   self.xcreate=api(self.xlib,'XCreateSimpleWindow',[P,c.c_ulong,I,I,U,U,U,c.c_ulong,c.c_ulong],c.c_ulong)
   self.xattrs=api(self.xlib,'XChangeWindowAttributes',[P,c.c_ulong,c.c_ulong,c.POINTER(XAttributes)],I)
   self.xmap=api(self.xlib,'XMapRaised',[P,c.c_ulong],I)
   self.xflush=api(self.xlib,'XFlush',[P],I)
   self.xdestroy=api(self.xlib,'XDestroyWindow',[P,c.c_ulong],I)
   self.xdisplay=self.xopen(None)
   if not self.xdisplay:raise RuntimeError('cannot open display')
  self.dpy=get(self.xdisplay);major,minor=I(),I()
  try:
   checked(init(self.dpy,c.byref(major),c.byref(minor)),'initialize')
   checked(bind(0x30A0),'bind ES')
   attrs=(I*15)(0x3033,4 if window else 1,0x3040,4,0x3024,8,0x3023,8,0x3022,8,0x3021,0,0x3025,16,0x3038)
   configs=(P*64)();count=I();checked(choose(self.dpy,attrs,configs,64,c.byref(count)),'choose')
   if not count.value:raise RuntimeError('no matching config')
   config=configs[0]
   config_attr=api(lib,'eglGetConfigAttrib',[P,P,I,c.POINTER(I)],U)
   def attribute(candidate,key):
    value=I();checked(config_attr(self.dpy,candidate,key,c.byref(value)),'config attribute')
    return value.value
   if msaa_samples is not None:
    config=next((candidate for candidate in configs[:count.value]
                 if attribute(candidate,0x3031)==msaa_samples
                 and attribute(candidate,0x3032)==int(msaa_samples>0)),None)
    if config is None:raise RuntimeError('no exact MSAA config for '+str(msaa_samples))
   self.config_info={name:attribute(config,key) for name,key in (
    ('config_id',0x3028),('red_bits',0x3024),('green_bits',0x3023),('blue_bits',0x3022),
    ('alpha_bits',0x3021),('depth_bits',0x3025),('samples',0x3031),('sample_buffers',0x3032),
    ('native_visual_id',0x302E))}
   if window:
    native=I(self.config_info['native_visual_id'])
    get_visual=api(self.xlib,'XGetVisualInfo',[P,c.c_long,c.POINTER(XVisualInfo),c.POINTER(I)],c.POINTER(XVisualInfo))
    free=api(self.xlib,'XFree',[P],I)
    create_colormap=api(self.xlib,'XCreateColormap',[P,c.c_ulong,P,I],c.c_ulong)
    self.free_colormap=api(self.xlib,'XFreeColormap',[P,c.c_ulong],I)
    create_window=api(self.xlib,'XCreateWindow',[P,c.c_ulong,I,I,U,U,U,I,U,P,c.c_ulong,c.POINTER(XAttributes)],c.c_ulong)
    template=XVisualInfo();template.visualid=native.value;matches=I()
    visuals=get_visual(self.xdisplay,1,c.byref(template),c.byref(matches))
    if not visuals or not matches.value:raise RuntimeError('native EGL visual not found in X server')
    try:
     v=visuals[0];parent=self.xroot(self.xdisplay)
     self.colormap=create_colormap(self.xdisplay,parent,v.visual,0)
     attr=XAttributes();attr.override_redirect=1;attr.colormap=self.colormap
     self.window=create_window(self.xdisplay,parent,0,0,width,height,0,v.depth,1,v.visual,(1<<1)|(1<<3)|(1<<9)|(1<<13),c.byref(attr))
    finally:free(c.cast(visuals,P))
    if not self.window:raise RuntimeError('cannot create native-visual window')
    self.xmap(self.xdisplay,self.window);self.xflush(self.xdisplay)
    window_surface=api(lib,'eglCreateWindowSurface',[P,P,c.c_ulong,c.POINTER(I)],P)
    self.surf=checked(window_surface(self.dpy,config,self.window,(I*1)(0x3038)),'window surface')
   else:self.surf=checked(surface(self.dpy,config,(I*5)(0x3057,width,0x3056,height,0x3038)),'pbuffer')
   self.ctx=checked(context(self.dpy,config,None,(I*3)(0x3098,2,0x3038)),'context')
   checked(current(self.dpy,self.surf,self.surf,self.ctx),'current')
   get_integer=api(gl,'glGetIntegerv',[U,c.POINTER(I)],None)
   actual=I();get_integer(0x80A9,c.byref(actual));self.samples_actual=actual.value
   if msaa_samples is not None and self.samples_actual!=msaa_samples:
    raise RuntimeError('actual GL samples do not match requested MSAA')
   self.swap_fn=api(lib,'eglSwapBuffers',[P,P],U)
   if window:
    interval=api(lib,'eglSwapInterval',[P,I],U);checked(interval(self.dpy,0),'swap interval')
  except BaseException:
   self.close();raise
 def swap(self):
  if self._closed:raise RuntimeError('EGL session already closed')
  if self.window_mode:checked(self.swap_fn(self.dpy,self.surf),'swap')
 def close(self):
  if self._closed:return
  self._closed=True
  if self.dpy:current(self.dpy,None,None,None)
  if self.ctx:destroy_context(self.dpy,self.ctx);self.ctx=None
  if self.surf:destroy_surface(self.dpy,self.surf);self.surf=None
  if self.dpy:terminate(self.dpy)
  if self.window:self.xdestroy(self.xdisplay,self.window);self.window=None
  if self.colormap:self.free_colormap(self.xdisplay,self.colormap);self.colormap=None
  if self.xdisplay:self.xclose(self.xdisplay);self.xdisplay=None
 def __enter__(self):return self
 def __exit__(self,*unused):self.close()
