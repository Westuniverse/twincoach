# SPDX-License-Identifier: GPL-2.0-only
"""Thread-local persistent libjpeg handles, independently owned output arrays."""
import ctypes as c
from threading import local
import numpy as np


class JpegDecodeError(ValueError):pass
class UnsupportedJpeg(JpegDecodeError):pass


class _Handle:
    def __init__(self,lib):
        self.lib=lib;self.pointer=lib.posepriv_jpeg_create()
        if not self.pointer:raise RuntimeError('Cannot create matching libjpeg decompressor')
        self.dimensions=(c.c_uint*2)()
    def close(self):
        if self.pointer:self.lib.posepriv_jpeg_destroy(self.pointer);self.pointer=None
    def __del__(self):self.close()


class ThreadLocalJpegDecoder:
    def __init__(self,library):
        self.library=c.CDLL(str(library))
        lib=self.library;lib.posepriv_jpeg_create.argtypes=[];lib.posepriv_jpeg_create.restype=c.c_void_p
        lib.posepriv_jpeg_destroy.argtypes=[c.c_void_p];lib.posepriv_jpeg_destroy.restype=None
        lib.posepriv_jpeg_header.argtypes=[c.c_void_p,c.c_void_p,c.c_size_t,c.c_int,c.c_int,c.POINTER(c.c_uint)]
        lib.posepriv_jpeg_header.restype=c.c_int
        lib.posepriv_jpeg_finish.argtypes=[c.c_void_p,c.c_void_p,c.c_size_t];lib.posepriv_jpeg_finish.restype=c.c_int
        lib.posepriv_jpeg_error.argtypes=[c.c_void_p];lib.posepriv_jpeg_error.restype=c.c_char_p
        lib.posepriv_jpeg_abi.restype=c.c_int
        if lib.posepriv_jpeg_abi()!=1:raise ValueError('Unsupported JPEG leaf ABI')
        self.local=local()

    def decode(self,jpeg,*,scale=2,rotation=0):
        handle=getattr(self.local,'handle',None)
        if handle is None:handle=self.local.handle=_Handle(self.library)
        encoded=np.frombuffer(jpeg,dtype=np.uint8)
        result=self.library.posepriv_jpeg_header(handle.pointer,encoded.ctypes.data,
            encoded.size,scale,rotation,handle.dimensions)
        if result:
            message=self.library.posepriv_jpeg_error(handle.pointer).decode('utf-8','replace')
            raise(UnsupportedJpeg if result==-2 else JpegDecodeError)(message)
        height,width=handle.dimensions
        # The returned buffer is never owned/reused by the persistent decoder.
        # It can be held by image leases/three downstream stages indefinitely.
        image=np.empty((height,width,3),dtype=np.uint8)
        if self.library.posepriv_jpeg_finish(handle.pointer,image.ctypes.data,image.nbytes):
            raise JpegDecodeError(self.library.posepriv_jpeg_error(handle.pointer).decode('utf-8','replace'))
        return image

    def close(self):
        handle=getattr(self.local,'handle',None)
        if handle is not None:handle.close();del self.local.handle
