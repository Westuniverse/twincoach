"""Private drop-in raw-head decoder with native heat/projection leaf (GPL-2.0-only)."""
import ctypes
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import numpy as np
from preview_adapter import (DecodedFull33, _HEAT_SIGMOID_Q2, _sigmoid, identity_for)
from posepriv_coach.contracts import CameraView, Keypoint, PoseFrame
from posepriv_coach.world_avatar_preview import WORLD_JOINTS, WorldDisplayObservation
from posepriv_coach.avatar_image_source import AvatarImageSource
from posepriv_coach.preview_head_orientation import CurrentHeadOrientation

_HEAD_ERRORS={1:'current world face point unavailable',2:'degenerate head orientation',
              3:'head rotation must be orthogonal/proper'}


def _records(values):
    # ndarray.tolist converts all numeric scalars in C, avoiding 340 Python
    # float calls/array scalar objects while retaining independent immutable data.
    return tuple(dict(x=x,y=y,z=z,visibility=v,presence=p)
                 for x,y,z,v,p in values.tolist())


@dataclass(frozen=True)
class NativeDecodedFull33(DecodedFull33):
    _native_head: object
    _head_error: str

    def head_orientation(self):
        if self._native_head is None:
            raise ValueError(self._head_error)
        return self._native_head


def _validated_world(source,world,image):
    """Assemble the existing type after its checks ran once in the C leaf."""
    value=object.__new__(WorldDisplayObservation)
    object.__setattr__(value,'source',source)
    object.__setattr__(value,'world_landmarks',tuple(MappingProxyType(p)for p in world))
    object.__setattr__(value,'normalized_landmarks',tuple(MappingProxyType(p)for p in image))
    return value


def _validated_head(source,matrices):
    """C has run the same basis/rotation checks; metadata comes from source."""
    value=object.__new__(CurrentHeadOrientation)
    object.__setattr__(value,'frame_id',source.frame_id)
    object.__setattr__(value,'timestamp_ns',source.timestamp_ns)
    object.__setattr__(value,'source','world_face_plane')
    object.__setattr__(value,'rotation',tuple(matrices[:9]))
    object.__setattr__(value,'source_body_rotation',tuple(matrices[9:]))
    return value


class NativeDecoder:
    """One serial decoder owner; persistent outputs, immutable result records.

    ctypes releases the GIL during the native leaf. Input arrays are zero-copy
    frombuffer views of the original S16/S8 bytes; no float heatmap expansion.
    Head/world contracts run once at the native boundary with original guards.
    """
    def __init__(self, library=None):
        self.library=ctypes.CDLL(str(Path(library or Path(__file__).with_name('libtail4_decode.so')).resolve()))
        pointer=ctypes.c_void_p
        self._s16=self.library.tail4_decode_s16
        self._s16.argtypes=[pointer]*12;self._s16.restype=ctypes.c_int
        self._f32=self.library.tail4_decode_f32
        self._f32.argtypes=[pointer,pointer,ctypes.c_float]+[pointer]*6;self._f32.restype=ctypes.c_int
        self._validate=self.library.tail4_validate_world_and_head
        self._validate.argtypes=[pointer]*3;self._validate.restype=ctypes.c_int
        self.heat_lut=np.ascontiguousarray(_HEAT_SIGMOID_Q2,dtype=np.float32)
        values=np.arange(-32768,32768,dtype=np.float32)
        self.q5_lut=_sigmoid(values/np.float32(32))
        self.q8_lut=_sigmoid(values/np.float32(256))
        self.image=np.empty((35,5),np.float32)
        self.world=np.empty((33,5),np.float32)
        self.presence=np.empty(1,np.float32)
        self.head=np.empty(18,np.float64)

    def _result(self,source,present,score):
        if not present:
            pose=PoseFrame(CameraView(source.view),source.frame_id,source.timestamp_ns,{},source.width,source.height)
            return NativeDecodedFull33(source,pose,WorldDisplayObservation(source,(),()),(),score,
                                       None,'thirty-three world landmarks required')
        status=self._validate(self.image.ctypes.data,self.world.ctypes.data,self.head.ctypes.data)
        if status<0:
            raise ValueError('finite model coordinates and separate confidences required')
        image_records=_records(self.image[:33]);world_records=_records(self.world)
        pose=PoseFrame(CameraView(source.view),source.frame_id,source.timestamp_ns,
            {name:Keypoint(image_records[i]['x'],image_records[i]['y'],
                min(image_records[i]['visibility'],image_records[i]['presence']))
             for i,name in WORLD_JOINTS.items()},source.width,source.height)
        orientation=_validated_head(source,self.head.tolist())if status==0 else None
        return NativeDecodedFull33(source,pose,_validated_world(source,world_records,image_records),
            _records(self.image[33:35]),score,orientation,_HEAD_ERRORS.get(status,''))

    def decode(self,source,roi,heads,heat_q2):
        if not isinstance(source,AvatarImageSource):
            raise ValueError('one image source required')
        if heads.identity != identity_for(source):
            raise ValueError('tail source/frame/ns/geometry mismatch')
        if (len(heads.landmarks195_q5),len(heads.world117_q14),len(heads.presence1_q8),len(heat_q2)) != (390,234,2,159744):
            raise ValueError('tail/head useful byte contract changed')
        raw=np.frombuffer(heads.landmarks195_q5,dtype='<i2')
        world=np.frombuffer(heads.world117_q14,dtype='<i2')
        presence=np.frombuffer(heads.presence1_q8,dtype='<i2')
        heat=np.frombuffer(heat_q2,dtype=np.int8)
        rect,padding=np.asarray(roi.rect,np.float32),np.asarray(roi.padding,np.float32)
        present=self._s16(raw.ctypes.data,world.ctypes.data,presence.ctypes.data,heat.ctypes.data,
            self.heat_lut.ctypes.data,self.q5_lut.ctypes.data,self.q8_lut.ctypes.data,
            rect.ctypes.data,padding.ctypes.data,self.image.ctypes.data,self.world.ctypes.data,self.presence.ctypes.data)
        return self._result(source,present,float(self.presence[0]))

    __call__=decode

    def decode_values(self,source,roi,raw,world,presence_probability,heat_q2):
        """Cached-float reference entry; production uses original raw S16 only."""
        if not isinstance(source,AvatarImageSource):
            raise ValueError('one image source required')
        raw=np.ascontiguousarray(raw,np.float32);world=np.ascontiguousarray(world,np.float32)
        if raw.size!=195 or world.size!=117 or len(heat_q2)!=159744:
            raise ValueError('cached float/head size changed')
        heat=np.frombuffer(heat_q2,dtype=np.int8)
        rect,padding=np.asarray(roi.rect,np.float32),np.asarray(roi.padding,np.float32)
        present=self._f32(raw.ctypes.data,world.ctypes.data,float(presence_probability),heat.ctypes.data,
            self.heat_lut.ctypes.data,rect.ctypes.data,padding.ctypes.data,self.image.ctypes.data,self.world.ctypes.data)
        return self._result(source,present,float(presence_probability))


_default=None


def decode(source,roi,heads,heat_q2):
    global _default
    if _default is None:
        _default=NativeDecoder()
    return _default.decode(source,roi,heads,heat_q2)
