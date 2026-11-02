# SPDX-License-Identifier: GPL-2.0-only
"""One cold ctypes binding, one batched native leaf per nonempty observation."""
import ctypes
from pathlib import Path
from types import MappingProxyType
import numpy as np
from posepriv_coach.world_avatar_preview import WorldDisplayObservation
from torso_input_translate_head import TwoViewTorsoHeadTranslationInput

VIEWS=('front','side')
NAMES=((5,'left_shoulder'),(6,'right_shoulder'),(7,'left_elbow'),(8,'right_elbow'),
       (9,'left_wrist'),(10,'right_wrist'),(11,'left_hip'),(12,'right_hip'))
REASONS={1:'q_below_0p5',2:'outside_source',3:'degenerate direction',4:'torso_direction_conflict',
 5:'opposite_torso_axes',6:'torso_scale_conflict',7:'current_geometry_weight',
 8:'degenerate_original_arm',9:'undistortion residual',10:'Singular matrix'}

class NativeV2Math:
    def __init__(self,driver,library=None):
        self.driver=driver
        self.lib=ctypes.CDLL(str(library or Path(__file__).with_name('libposepriv_v2_math.so')))
        if self.lib.posepriv_v2_math_abi()!=1:raise ValueError('native v2 ABI differs')
        ptr=ctypes.POINTER(ctypes.c_double)
        self.call=self.lib.posepriv_v2_math_apply
        self.call.argtypes=[ptr,ptr,ptr,ptr,ctypes.POINTER(ctypes.c_uint64),ptr,ctypes.POINTER(ctypes.c_int)]
        self.call.restype=ctypes.c_int
        self.params=np.zeros(64,dtype=np.float64)
        for v,view in enumerate(VIEWS):
            k=driver.K[view]
            self.params[v*11:v*11+11]=[k.width,k.height,k.fx,k.fy,k.cx,k.cy,*(k.distortion or (0.,0.,0.,0.,0.))]
            self.params[22+v*9:31+v*9]=driver.R[view].flat
            self.params[40+v*9:49+v*9]=driver.relative[view].flat
        self.params[58]=driver.torso_length
        self.params[59:63]=[driver.length[s,p]for s in('left','right')for p in('upper_arm','lower_arm')]
        self.params[63]=VIEWS.index(driver.primary_view)
        self.raw=np.zeros((2,17,3));self.world=np.empty((33,3));self.out=np.empty((33,3))
        self.meta=np.empty(28);self.codes=np.empty(7,dtype=np.int32);self.mask=ctypes.c_uint64()
        self.args=[a.ctypes.data_as(ptr)for a in(self.params,self.raw,self.world,self.out)]
        self.args +=[ctypes.byref(self.mask),self.meta.ctypes.data_as(ptr),self.codes.ctypes.data_as(ctypes.POINTER(ctypes.c_int))]

    def apply(self,primary,work):
        if not primary.world_landmarks or any(
            (work['world_observations'][v].source.view,work['world_observations'][v].source.width,
             work['world_observations'][v].source.height,work['world_observations'][v].source.rotation_degrees)!=g
            for v,g in self.driver.expected_geometry.items()):
            return TwoViewTorsoHeadTranslationInput.apply(self.driver,primary,work)
        self.raw[:,:,2]=-1.
        for v,view in enumerate(VIEWS):
            points=work['poses'][view].keypoints
            for i,name in NAMES:
                point=points.get(name)
                if point is not None:self.raw[v,i]=point.x,point.y,point.confidence
        for i,p in enumerate(primary.world_landmarks):self.world[i]=p['x'],p['y'],p['z']
        self.call(*self.args)
        c,m=self.codes,self.meta;mask=self.mask.value
        meta=dict(active=bool(mask),scope='display_only_current_two_view_before_existing_filter',arms={})
        torso=dict(active=bool(c[0]))
        if c[0]:torso.update(angle_deg=float(m[0]),rotation=m[1:10].reshape(3,3).tolist(),pivot=m[10:13].tolist(),head_translation=m[13:16].tolist())
        elif c[1]>0:torso['reason']=REASONS[int(c[1])]
        if c[2]>0:meta['reason']=REASONS[int(c[2])]
        else:
            for n,name in enumerate(('left_upper_arm','left_lower_arm','right_upper_arm','right_lower_arm')):
                info=dict(weight=float(m[16+n]),reason=REASONS[int(c[3+n])])
                if c[3+n]==7:info.update(pixel_energy_before=float(m[20+n]),pixel_energy_fit=float(m[24+n]))
                meta['arms'][name]=info
        meta['torso_current_geometry']=torso
        if not mask:return primary,meta
        modified=[i for i in range(33)if mask&(1<<i)]
        meta['modified_world_indices']=modified;points=list(primary.world_landmarks)
        for i in modified:points[i]=MappingProxyType(dict(points[i],x=float(self.out[i,0]),y=float(self.out[i,1]),z=float(self.out[i,2])))
        result=object.__new__(WorldDisplayObservation)
        object.__setattr__(result,'source',primary.source);object.__setattr__(result,'world_landmarks',tuple(points))
        object.__setattr__(result,'normalized_landmarks',primary.normalized_landmarks)
        return result,meta
