"""GStreamer RGB565 -> the project's native Mali/EGL window, without mesh imports."""
from contextlib import ExitStack
import argparse
import ctypes as c
import json
import math
from pathlib import Path
import re
import signal
import subprocess
import time

from posepriv_coach.movie_presentation import MoviePresentation
from posepriv_coach.movie_report import LatestMovieReport

WIDTH, HEIGHT = 1280, 720
PIPELINE = ('filesrc name=source ! avidemux ! jpegdec ! videoconvert n-threads=2 ! '
            'video/x-raw,format=RGB16,width=1280,height=720,pixel-aspect-ratio=1/1 ! '
            'appsink name=frames sync=false max-buffers=1 drop=false wait-on-eos=false enable-last-sample=false')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--loop', action='store_true')
    parser.add_argument('--seconds', type=float)
    return parser


class GstFrame:
    def __init__(self, sample, Gst):
        self.sample, self.Gst = sample, Gst
        caps = sample.get_caps().get_structure(0)
        if (caps.get_value('width'), caps.get_value('height'), caps.get_value('format')) != (WIDTH, HEIGHT, 'RGB16'):
            raise ValueError('movie requires 1280x720 RGB16 frames')
        self.buffer = sample.get_buffer()
        self.pts_ns = int(self.buffer.pts)
        if self.pts_ns == Gst.CLOCK_TIME_NONE:
            raise ValueError('movie frame has no PTS')
        self.mapping = None

    def __enter__(self):
        import numpy as np
        ok, mapped = self.buffer.map(self.Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError('cannot map movie frame')
        self.mapping = mapped
        try:
            data = mapped.data
            if len(data) != WIDTH * HEIGHT * 2:
                raise ValueError('movie RGB16 frame has unexpected stride/size')
            return np.frombuffer(data, np.uint16).reshape(HEIGHT, WIDTH)
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *unused):
        if self.mapping is not None:
            self.buffer.unmap(self.mapping)
            self.mapping = None
        self.sample = self.buffer = None


class GstMovieSource:
    def __init__(self, video):
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        self.Gst = Gst
        Gst.init(None)
        self.pipeline = Gst.parse_launch(PIPELINE)
        self.pipeline.get_by_name('source').set_property('location', str(video))
        self.sink = self.pipeline.get_by_name('frames')
        self.bus = self.pipeline.get_bus()
        self.ended = self._bus_eos = self._seen_sample = False

    def _messages(self):
        Gst = self.Gst
        while True:
            message = self.bus.timed_pop_filtered(0, Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if message is None:
                return
            if message.type == Gst.MessageType.ERROR:
                raise RuntimeError(str(message.parse_error()))
            self._bus_eos = True

    def pull(self):
        self._messages()
        sample = self.sink.emit('try-pull-sample', 100 * self.Gst.MSECOND)
        self._messages()
        if sample is None:
            self.ended = self._bus_eos or (self._seen_sample and self.sink.get_property('eos'))
            return None
        self._seen_sample = True
        return GstFrame(sample, self.Gst)

    def rewind(self):
        Gst = self.Gst
        if not self.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0):
            raise RuntimeError('cannot rewind movie')
        self.ended = self._bus_eos = self._seen_sample = False

    def __enter__(self):
        if self.pipeline.set_state(self.Gst.State.PLAYING) == self.Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError('cannot start movie decode')
        return self

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)

    def __exit__(self, *unused):
        self.close()


class XKeyEvent(c.Structure):
    _fields_ = [('type', c.c_int), ('serial', c.c_ulong), ('send_event', c.c_int),
                ('display', c.c_void_p), ('window', c.c_ulong), ('root', c.c_ulong),
                ('subwindow', c.c_ulong), ('time', c.c_ulong), ('x', c.c_int), ('y', c.c_int),
                ('x_root', c.c_int), ('y_root', c.c_int), ('state', c.c_uint),
                ('keycode', c.c_uint), ('same_screen', c.c_int)]


class XEvent(c.Union):
    _fields_ = [('key', XKeyEvent), ('pad', c.c_long * 24)]


class EglMovieQuad:
    def __init__(self):
        import numpy as np
        from egl_context import EglSession, api, gl, U, I, P, F, info, glerror
        self.np, self.api, self.gl = np, api, gl
        self.session = EglSession(WIDTH, HEIGHT, window=True, msaa_samples=0)
        self._closed = False
        try:
            def bind(name, args, result=None):
                return api(gl, name, args, result)
            self.renderer = info(0x1F01).decode()
            self.config = self.session.config_info
            self.window_id = self.session.window
            shader = bind('glCreateShader', [U], U)
            source = bind('glShaderSource', [U, I, c.POINTER(c.c_char_p), c.POINTER(I)])
            compile_shader = bind('glCompileShader', [U])
            shader_status = bind('glGetShaderiv', [U, U, c.POINTER(I)])
            attach = bind('glAttachShader', [U, U])
            delete_shader = bind('glDeleteShader', [U])
            program = bind('glCreateProgram', [], U)()
            for kind, text in ((0x8B31, b'attribute vec2 pos; attribute vec2 uv; varying mediump vec2 tex; void main(){gl_Position=vec4(pos,0.,1.);tex=uv;}'),
                               (0x8B30, b'precision mediump float; varying mediump vec2 tex; uniform sampler2D image; void main(){gl_FragColor=texture2D(image,tex);}')):
                handle = shader(kind)
                text_pointer = c.c_char_p(text)
                source(handle, 1, c.byref(text_pointer), None)
                compile_shader(handle)
                ok = I(); shader_status(handle, 0x8B81, c.byref(ok))
                if not ok.value:
                    raise RuntimeError('movie shader compilation failed')
                attach(program, handle); delete_shader(handle)
            bind('glLinkProgram', [U])(program)
            ok = I(); bind('glGetProgramiv', [U, U, c.POINTER(I)])(program, 0x8B82, c.byref(ok))
            if not ok.value:
                raise RuntimeError('movie shader link failed')
            bind('glUseProgram', [U])(program)
            vertex_buffer = U(); bind('glGenBuffers', [I, c.POINTER(U)])(1, c.byref(vertex_buffer))
            bind('glBindBuffer', [U, U])(0x8892, vertex_buffer.value)
            quad = np.array([[-1,1,0,0],[-1,-1,0,1],[1,1,1,0],[1,-1,1,1]], np.float32)
            bind('glBufferData', [U, c.c_ssize_t, P, U])(0x8892, quad.nbytes, quad.ctypes.data, 0x88E4)
            for name, offset in ((b'pos', 0), (b'uv', 8)):
                location = bind('glGetAttribLocation', [U, c.c_char_p], I)(program, name)
                bind('glEnableVertexAttribArray', [U])(location)
                bind('glVertexAttribPointer', [U, I, U, U, I, P])(location, 2, 0x1406, 0, 16, offset)
            texture = U(); bind('glGenTextures', [I, c.POINTER(U)])(1, c.byref(texture))
            bind('glActiveTexture', [U])(0x84C0)
            self.bind_texture = bind('glBindTexture', [U, U]); self.texture = texture.value
            self.bind_texture(0x0DE1, self.texture)
            for name, value in ((0x2801,0x2600),(0x2800,0x2600),(0x2802,0x812F),(0x2803,0x812F)):
                bind('glTexParameteri', [U, U, I])(0x0DE1, name, value)
            bind('glPixelStorei', [U, I])(0x0CF5, 1)
            bind('glTexImage2D', [U,I,I,I,I,I,U,U,P])(0x0DE1,0,0x1907,WIDTH,HEIGHT,0,0x1907,0x8363,None)
            bind('glUniform1i', [I,I])(bind('glGetUniformLocation', [U,c.c_char_p], I)(program,b'image'),0)
            bind('glViewport', [I,I,I,I])(0,0,WIDTH,HEIGHT)
            for state in (0x0B71,0x0BD0,0x0BE2):bind('glDisable',[U])(state)
            self.upload = bind('glTexSubImage2D', [U,I,I,I,I,I,U,U,P])
            self.draw_quad = bind('glDrawArrays', [U,I,I])
            self.finish = bind('glFinish', [])
            self.error = glerror
            self._check_error()
            xlib = self.session.xlib
            self.pending = api(xlib, 'XPending', [P], I)
            self.next_event = api(xlib, 'XNextEvent', [P,c.POINTER(XEvent)], I)
            keycode = api(xlib, 'XKeysymToKeycode', [P,c.c_ulong], c.c_ubyte)
            self.quit_keys = {keycode(self.session.xdisplay,k) for k in (ord('q'),0xff1b)}
            api(xlib,'XStoreName',[P,c.c_ulong,c.c_char_p],I)(self.session.xdisplay,self.session.window,b'PosePriv Native HDMI Movie')
            api(xlib,'XSelectInput',[P,c.c_ulong,c.c_long],I)(self.session.xdisplay,self.session.window,1)
            api(xlib,'XSetInputFocus',[P,c.c_ulong,I,c.c_ulong],I)(self.session.xdisplay,self.session.window,2,0)
            self.session.xflush(self.session.xdisplay)
        except BaseException:
            self.close()
            raise

    def _check_error(self):
        code = self.error()
        if code:
            raise RuntimeError(f'movie GL error {hex(code)}')

    def poll_quit(self):
        while self.pending(self.session.xdisplay):
            event = XEvent(); self.next_event(self.session.xdisplay,c.byref(event))
            if event.key.type == 2 and event.key.keycode in self.quit_keys:
                return True
        return False

    def draw(self, pixels):
        if self._closed:
            raise RuntimeError('movie renderer is closed')
        if pixels.dtype != self.np.uint16 or pixels.shape != (HEIGHT, WIDTH) or not pixels.flags.c_contiguous:
            raise ValueError('movie renderer requires contiguous 1280x720 RGB565')
        self.bind_texture(0x0DE1,self.texture)
        self.upload(0x0DE1,0,0,0,WIDTH,HEIGHT,0x1907,0x8363,pixels.ctypes.data)
        self.draw_quad(5,0,4)
        self.finish()
        self.session.swap()
        self._check_error()

    def close(self):
        if not self._closed:
            self._closed = True
            # The context owns the single program/buffer/texture for its lifetime.
            self.session.close()

    def __enter__(self):return self
    def __exit__(self,*unused):self.close()


def run_movie(source, renderer, presentation, *, loop, seconds, stop, publish,
              clock_ns=time.monotonic_ns, sleep=time.sleep):
    started = clock_ns(); last_sample = started; last_report = started
    deadline = started + round(seconds * 1e9) if seconds is not None else None
    quit_requested = False
    def quitting():
        nonlocal quit_requested
        if not quit_requested:
            quit_requested = stop() or renderer.poll_quit() or (deadline is not None and clock_ns() >= deadline)
        return quit_requested
    while not quitting():
        frame = source.pull()
        now = clock_ns()
        if frame is None:
            if source.ended:
                presentation.end_loop()
                publish()
                if not loop:break
                source.rewind(); last_sample = clock_ns()
            elif now - last_sample > 10_000_000_000:
                raise TimeoutError('no movie frame for ten seconds')
            continue
        last_sample = now
        target = presentation.deadline(frame.pts_ns, now)
        while not quitting():
            remaining = target - clock_ns()
            if remaining <= 0:break
            sleep(min(remaining / 1e9,.05))
        if quitting():break
        with frame as pixels:
            renderer.draw(pixels)
            presentation.confirmed(clock_ns())
        if clock_ns() - last_report >= 5_000_000_000:
            publish(); last_report = clock_ns()


def main(argv=None):
    parser = build_parser(); args = parser.parse_args(argv)
    if args.seconds is not None and (not math.isfinite(args.seconds) or args.seconds <= 0):
        parser.error('--seconds must be positive and finite')
    video = args.video.resolve(strict=True)
    if not video.is_file() or args.report.resolve() == video:
        parser.error('video must be a file and report must be a different path')
    screen = subprocess.run(['xrandr','--current'],check=True,capture_output=True,text=True).stdout
    if re.search(r'current 1280 x 720[, ]',screen) is None:
        raise RuntimeError('native movie playback requires a 1280x720 desktop; use run_egl_hdmi_demo.sh')
    args.report.parent.mkdir(parents=True,exist_ok=True)
    presentation = MoviePresentation(); requested_stop = False; failure = None; renderer = None
    launched = time.monotonic_ns()
    def stop_signal(*unused):
        nonlocal requested_stop
        requested_stop = True
    handlers = {sig:signal.signal(sig,stop_signal) for sig in (signal.SIGINT,signal.SIGTERM)}
    reporter = LatestMovieReport(args.report)
    def publish():
        result = dict(schema='posepriv.native_movie_replay.v1',
            scope='offline AVI playback; per-frame GPU finish and swap-return observations, not generation/inference/physical scanout',
            video=str(video),display_size=[WIDTH,HEIGHT],elapsed_seconds=(time.monotonic_ns()-launched)/1e9,
            error=failure,renderer=renderer.renderer if renderer else None,
            egl_config=renderer.config if renderer else None,native_window_id=renderer.window_id if renderer else None,appsink_queue_limit=1,drop_frames=False,
            **presentation.report())
        reporter.submit(result)
    try:
        with ExitStack() as resources:
            source = resources.enter_context(GstMovieSource(video))
            renderer = resources.enter_context(EglMovieQuad())
            print(json.dumps(dict(fullscreen=True,display_size=[WIDTH,HEIGHT],renderer=renderer.renderer)),flush=True)
            publish()
            run_movie(source,renderer,presentation,loop=args.loop,seconds=args.seconds,
                      stop=lambda:requested_stop,publish=publish)
    except BaseException as exc:
        failure = str(exc)
        raise
    finally:
        for sig,handler in handlers.items():signal.signal(sig,handler)
        try:publish()
        finally:reporter.close()
    print('POSEPRIV_NATIVE_MOVIE PASS (offline file replay)',flush=True)
