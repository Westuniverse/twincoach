# SPDX-License-Identifier: GPL-2.0-only
"""Private factory; original context/init/finish owns the same current GL thread."""
from posepriv_coach.field_preview_gles import make_painter as python_painter
from native_gl import NativeGlSubmit


def make_painter(asset,*,native_submit_library=None,_native_callbacks=None,**options):
    if native_submit_library is not None and not(options.get('hdmi')and options.get('gpu_video')):
        raise ValueError('Native GLES submit candidate requires HDMI GPU video')
    painter=python_painter(asset,**options)
    if native_submit_library is None:return painter
    try:
        import benchmark as g
        native=NativeGlSubmit(native_submit_library,painter,g,
            normal_preparer=options.get('normal_preparer'),callbacks=_native_callbacks)
    except BaseException:painter.close();raise
    original_close=painter.close
    def close():
        try:original_close()
        finally:native.close()
    painter.draw=native.draw;painter.close=close;painter.native_gl_submit=native
    return painter
