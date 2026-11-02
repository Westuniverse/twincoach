# SPDX-License-Identifier: GPL-2.0-only
"""Exact original Q5 map on ARM, actual ROI1 gather/Q7 through a borrowed fd."""
import ctypes as c
import struct

import cv2
import numpy as np

COMMAND=struct.Struct('<QQQQIIIIIIII')
ROI_IOCTL=0xc0405270
SOURCE_LIMIT=1048576-262144-16


def kernel_ioctl(fd,command,argument):
    import fcntl
    return fcntl.ioctl(fd,command,argument,True)


class NativeRoiCropper:
    """One actor-thread owner; kernel mutex serializes this fd with skinning.

    The caller owns this duplicated file description and closes it after the
    actor exits. No second device open, image RPC, CPU pixel gather or retry.
    Returned immutable Q7 bytes retain the existing PreparedImage ownership.
    """
    def __init__(self,library,fd,timeout_ms=200,*,ioctl=kernel_ioctl):
        if type(fd)is not int or fd<0:raise ValueError('borrowed shared-DMA fd required')
        if type(timeout_ms)is not int or not 1<=timeout_ms<=2000:raise ValueError('ROI timeout1..2000ms required')
        from fast_crop import matrices
        from preview_adapter import FrameRoi
        self.matrices=matrices;self.FrameRoi=FrameRoi
        self.library=c.CDLL(str(library))
        self.coords=self.library.posepriv_crop_coords_q5
        self.coords.argtypes=[c.c_int,c.c_int,c.c_void_p,c.c_void_p,c.c_int]
        self.coords.restype=None
        self.fd=fd;self.timeout_ms=timeout_ms;self.ioctl=ioctl
        self.coordinate_map=np.empty((256,256),np.uint32)
        self.output=np.empty((256,256,3),np.uint8)
        self.command=bytearray(COMMAND.size)
        self.ticket=0;self.submitted_calls=0;self.completed_calls=0

    def crop(self,bgr,rect):
        if (bgr.dtype!=np.uint8 or bgr.ndim!=3 or bgr.shape[2]!=3 or
                not bgr.flags.c_contiguous):raise ValueError('contiguous U8 BGR image required')
        h,w=bgr.shape[:2]
        if not 1<=w<=2048 or not 1<=h<=2048 or bgr.nbytes>SOURCE_LIMIT:
            raise ValueError('ROI1 source exceeds its coherent allocation')
        perspective,_=self.matrices((*rect,w,h))
        inverse=cv2.invert(perspective,flags=cv2.DECOMP_LU)[1]
        self.coords(w,h,inverse.ctypes.data,self.coordinate_map.ctypes.data,0)
        self.ticket=(self.ticket+1)&0xffffffffffffffff
        COMMAND.pack_into(self.command,0,bgr.ctypes.data,self.coordinate_map.ctypes.data,
            self.output.ctypes.data,self.ticket,w,h,bgr.strides[0],bgr.nbytes,self.timeout_ms,0,0,0)
        self.submitted_calls+=1
        if self.ioctl(self.fd,ROI_IOCTL,self.command)!=0:raise OSError('ROI1 ioctl returned nonzero')
        self.completed_calls+=1
        return self.output.tobytes(),self.FrameRoi(tuple(rect),(0.,0.,0.,0.))

    def stats(self):
        return dict(backend='ROI1_exact_Q5_hardware_gather_Q7',borrowed_fd=True,
            submitted_calls=self.submitted_calls,completed_calls=self.completed_calls,
            output_bytes_per_call=196608,map_bytes_per_call=262144,
            CPU_pre_gather=False,extra_image_RPC=False)
