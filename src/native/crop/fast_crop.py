# SPDX-License-Identifier: GPL-2.0-only
"""Independent candidate leaves; frozen crop_rgb remains the numerical reference."""
import ctypes
import math
from pathlib import Path
import sys
import cv2
import numpy as np

HERE=Path(__file__).resolve().parent
sys.path[:0]=[str(HERE.parent/'frontend'),
              str(HERE.parent/'adapter')]
from live_frontend import Q7_RGB_LUT
from preview_adapter import FrameRoi


def matrices(rect):
    # Same float32 arithmetic, boxPoints and 0..256 destination as crop_rgb.
    cx,cy,rw,rh,angle,width,height=rect
    cx,cy,rw,rh,angle=map(np.float32,(cx,cy,rw,rh,angle))
    pw,ph=np.float32(rw*width),np.float32(rh*height)
    degrees=np.float32(float(np.float32(angle*np.float32(180)))/math.pi)
    source=cv2.boxPoints(((float(cx*width),float(cy*height)),(float(pw),float(ph)),float(degrees)))
    destination=np.array([[0,256],[0,0],[256,0],[256,256]],np.float32)
    perspective=cv2.getPerspectiveTransform(source,destination)
    affine=cv2.getAffineTransform(source[:3],destination[:3])
    return perspective,affine


def affine_bgr_q7(bgr,rect):
    h,w=bgr.shape[:2];_,affine=matrices((*rect,w,h))
    crop=cv2.warpAffine(bgr,affine,(256,256),flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_REPLICATE)
    rgb=cv2.cvtColor(crop,cv2.COLOR_BGR2RGB)
    return cv2.LUT(rgb,Q7_RGB_LUT).tobytes(),FrameRoi(tuple(rect),(0.,0.,0.,0.))


class NativeCropper:
    def __init__(self,library):
        self.library=ctypes.CDLL(str(library))
        self.call=self.library.posepriv_crop_bgr_q7
        self.call.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_size_t,
                            ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_int]
        self.call.restype=None
        self.output=np.empty((256,256,3),np.uint8)
        self.rgb=np.empty_like(self.output)

    def crop(self,bgr,rect,*,return_rgb=False,mode=0):
        if bgr.dtype!=np.uint8 or bgr.ndim!=3 or bgr.shape[2]!=3 or bgr.strides[2]!=1 or bgr.strides[1]!=3:
            raise ValueError('interleaved U8 BGR input required')
        h,w=bgr.shape[:2];perspective,_=matrices((*rect,w,h))
        inverse=cv2.invert(perspective,flags=cv2.DECOMP_LU)[1]
        self.call(bgr.ctypes.data,w,h,bgr.strides[0],inverse.ctypes.data,
                  self.output.ctypes.data,self.rgb.ctypes.data if return_rgb else None,mode)
        roi=FrameRoi(tuple(rect),(0.,0.,0.,0.))
        if return_rgb:return self.output.tobytes(),roi,self.rgb.copy()
        return self.output.tobytes(),roi
