# SPDX-License-Identifier: MIT
"""Private independent BODY17 ABI2; one owner, strict storage, no fallback."""
import ctypes
import weakref
from pathlib import Path
from threading import get_ident
from typing import NamedTuple
from posepriv_coach.native_object_tracking import NativePyramidLKFlow

class Body17Error(RuntimeError):pass

class CurrentTrack(NamedTuple):
    points:object
    accepted:object
    reason:object
    fb_error:object
    displacement:object
    forward_patch:object
    backward_patch:object
    forward_status:object
    backward_status:object
    backward_points:object


class Body17:
    _prove_allocation=NativePyramidLKFlow._prove_allocation
    _gray=NativePyramidLKFlow._gray
    def __init__(self,path,width,height):
        if type(width)is not int or type(height)is not int or not(1<=width<=4096 and 1<=height<=2160):raise ValueError('Exact bounded dimensions required')
        import cv2,numpy as np
        self._numpy=np;self.width=width;self.height=height;self._owner=get_ident();self._failed=False;self._closed=False;self._stage='empty';self._handle=None;self._forward_result=None
        self._bytes_address=ctypes.pythonapi.PyBytes_AsString;self._bytes_address.argtypes=[ctypes.py_object];self._bytes_address.restype=ctypes.c_void_p
        self.library_path=Path(path).resolve(strict=True);self.lib=ctypes.CDLL(str(self.library_path))
        abi=self.lib.posepriv_body17_tracking_abi;abi.argtypes=[];abi.restype=ctypes.c_uint32
        if abi()!=2:raise Body17Error('Independent body17 ABI2 required')
        for role in ('build','runtime'):
            f=getattr(self.lib,'posepriv_body17_tracking_'+role+'_version');f.argtypes=[];f.restype=ctypes.c_char_p
            if f()!=cv2.__version__.encode('ascii'):raise Body17Error('C++ build/runtime and Python OpenCV versions differ')
        P=ctypes.c_void_p;S=ctypes.c_size_t
        signatures=dict(create=[ctypes.c_int,ctypes.c_int,ctypes.POINTER(P)],seed=[P,P,S,S],forward=[P,P,S,S,P,S,P,P,P],backward=[P,P,S,P,P,P],commit=[P],reset=[P],timing=[P,P,S],destroy=[P])
        for name,args in signatures.items():
            f=getattr(self.lib,'posepriv_body17_tracking_'+name+'_v1');f.argtypes=args;f.restype=None if name=='destroy'else ctypes.c_int;setattr(self,'_c_'+name,f)
        f=self.lib.posepriv_body17_tracking_track_current_v2
        f.argtypes=[P,P,S,S,P,S,ctypes.c_double,ctypes.c_double,P,P,P,P,P,P,P,P,P,P]
        f.restype=ctypes.c_int;self._c_track_current=f
        handle=P();code=self._c_create(width,height,ctypes.byref(handle))
        if code or not handle.value:
            if handle.value:self._c_destroy(handle.value)
            raise Body17Error('Native creation failed '+str(code))
        self._handle=handle.value
        self._finalizer=weakref.finalize(self,self._c_destroy,self._handle)

    def _usable(self):
        if get_ident()!=self._owner:raise Body17Error('Creating owner thread required')
        if self._closed or self._failed:raise Body17Error('Body17 closed/failed; no retry or fallback')
    def _invoke(self,fn,*args):
        try:
            code=fn(self._handle,*args)
            if code:raise Body17Error('Native stage/input/execution failure '+str(code))
        except BaseException:
            self._failed=True
            try:self._c_reset(self._handle)
            except BaseException:pass
            raise
    def _points(self,points):
        np=self._numpy
        if type(points)is not np.ndarray or points.dtype!=np.dtype(np.float32)or points.ndim!=3 or points.shape[1:]!=(1,2)or not 1<=len(points)<=17:raise ValueError('Exact native float32 Nx1x2,1..17 required')
        self._prove_allocation(points);p=points.copy(order='C')
        if not np.isfinite(p).all()or (p[:,:,0]<0).any()or (p[:,:,0]>=self.width).any()or (p[:,:,1]<0).any()or (p[:,:,1]>=self.height).any():raise ValueError('Finite strictly in-image seed required')
        return p
    def _outputs(self,n,fn,*args):
        np=self._numpy;p=np.zeros((n,1,2),np.float32);s=np.zeros((n,1),np.uint8);e=np.zeros((n,1),np.float32)
        self._invoke(fn,*args,p.ctypes.data,s.ctypes.data,e.ctypes.data)
        if ((s!=0)&(s!=1)).any():self._failed=True;raise Body17Error('Malformed native status')
        valid=s[:,0]==1
        # Never inspect failed point/error slots; zeros there are placeholders.
        if not np.isfinite(p[valid]).all()or not np.isfinite(e[valid]).all()or (e[valid]<0).any():self._failed=True;raise Body17Error('Malformed successful native output')
        return p,s,e
    def seed(self,gray):
        self._usable()
        if self._stage not in ('empty','seeded'):raise Body17Error('seed requires empty/committed stage')
        image=self._gray(gray);self._invoke(self._c_seed,image.ctypes.data,image.strides[0],image.nbytes);self._stage='seeded';self._forward_result=None
    def track_current(self,gray,points,source_scale=(1.,1.)):
        """Build current pyramid, bidirectional LK, qualify and commit in one call.

        source_scale is source pixels per tracking pixel (half-size gray: 2,2).
        Points stay in native gray pixels. Reason0 accepts; 1/2 forward status/
        outside, 3/4 backward status/outside, 5 patch, 6 displacement, 7 FB.
        Output is trusted to the matched ABI2; no Python point scans/copies.
        """
        self._usable()
        if self._stage!='seeded':raise Body17Error('track_current requires seeded stage')
        np=self._numpy
        if type(gray)is not np.ndarray or gray.dtype!=np.dtype(np.uint8)or gray.shape!=(self.height,self.width)or not gray.flags.c_contiguous:raise ValueError('Exact contiguous uint8 HxW gray required')
        if type(points)is not np.ndarray or points.dtype!=np.dtype(np.float32)or points.ndim!=3 or points.shape[1:]!=(1,2)or not 1<=len(points)<=17 or not points.flags.c_contiguous:raise ValueError('Exact contiguous native float32 Nx1x2,1..17 required')
        self._prove_allocation(gray);self._prove_allocation(points)
        sx,sy=source_scale
        n=len(points)
        result=CurrentTrack(np.empty((n,1,2),np.float32),np.empty(n,np.uint8),np.empty(n,np.uint8),np.empty(n,np.float64),np.empty(n,np.float64),np.empty(n,np.float32),np.empty(n,np.float32),np.empty(n,np.uint8),np.empty(n,np.uint8),np.empty((n,1,2),np.float32))
        self._invoke(self._c_track_current,gray.ctypes.data,gray.strides[0],gray.nbytes,points.ctypes.data,n,sx,sy,*(x.ctypes.data for x in result))
        self._stage='seeded';self._forward_result=None
        return result
    def forward(self,gray,points):
        self._usable()
        if self._stage!='seeded':raise Body17Error('forward requires seeded stage')
        image=self._gray(gray);p=self._points(points)
        result=self._outputs(len(p),self._c_forward,image.ctypes.data,image.strides[0],image.nbytes,p.ctypes.data,len(p))
        self._forward_result=tuple(x.copy()for x in result);self._stage='forward';return result
    def backward(self,indices):
        self._usable();np=self._numpy
        if self._stage!='forward':raise Body17Error('backward requires forward stage')
        if type(indices)is not np.ndarray or indices.dtype!=np.dtype(np.uint32)or indices.ndim!=1 or not 1<=len(indices)<=17:raise ValueError('Exact uint32 subset1..17 required')
        self._prove_allocation(indices);ids=indices.copy(order='C');p,s,e=self._forward_result
        if len(np.unique(ids))!=len(ids)or (ids>=len(p)).any():raise ValueError('Unique original compact forward indices required')
        for i in ids:
            if s[i,0]!=1:raise ValueError('Backward cannot read failed forward point')
            x,y=p[i,0]
            if not(0<=x<self.width and 0<=y<self.height):raise ValueError('Backward subset requires in-image successful forward point')
        result=self._outputs(len(ids),self._c_backward,ids.ctypes.data,len(ids));self._stage='backward';return result
    def commit(self):
        self._usable()
        if self._stage not in ('forward','backward'):raise Body17Error('commit requires current transaction')
        if self._stage=='forward':
            p,s,_=self._forward_result
            for i in range(len(p)):
                if s[i,0]==1 and 0<=p[i,0,0]<self.width and 0<=p[i,0,1]<self.height:raise Body17Error('Successful subset must run backward before commit')
        self._invoke(self._c_commit);self._stage='seeded';self._forward_result=None
    def reset(self):
        self._usable();self._invoke(self._c_reset);self._stage='empty';self._forward_result=None
    def timing(self):
        self._usable();values=(ctypes.c_uint64*9)();self._invoke(self._c_timing,ctypes.addressof(values),9)
        return dict(zip(('abi','sequence','operation','result','entered','completed','whole_wall_ns','pyramid_wall_ns','LK_wall_ns'),map(int,values)))
    def close(self):
        if get_ident()!=self._owner:raise Body17Error('Creating owner thread required')
        if not self._closed:
            self._closed=True;handle,self._handle=self._handle,None
            finalizer=getattr(self,'_finalizer',None)
            if finalizer is not None:finalizer.detach()
            if handle is not None:self._c_destroy(handle)
    def __enter__(self):self._usable();return self
    def __exit__(self,*_):self.close()
