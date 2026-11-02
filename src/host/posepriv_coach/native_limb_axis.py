"""Optional exact history-branch limb transport in the existing bone extension.

The Python rig remains the oracle for first use and unsupported/legacy inputs.
Only the rig writes history; this callable returns independent array snapshots.
"""
from .native_bone_affine import NativeBoneAffineBuilder
from .skeleton_pose import BoneSegment3D


def native_limb_axis_enabled(config):
    enabled=config.get('preview_native_limb_axis',False)
    if type(enabled)is not bool:raise ValueError('preview_native_limb_axis must be boolean')
    if enabled and not config.get('preview_bone_affine_library'):
        raise ValueError('native limb axis requires preview_bone_affine_library')
    return enabled


class NativeLimbAxisBuilder:
    def __init__(self,bone_builder):
        if not isinstance(bone_builder,NativeBoneAffineBuilder):
            raise TypeError('native limb axis requires a NativeBoneAffineBuilder')
        function=getattr(bone_builder._module,'limb_axis',None)
        if not callable(function):raise ValueError('selected bone library has no native limb_axis method')
        import numpy as np
        self.np=np;self._function=function;self.bone_builder=bone_builder

    def __call__(self,slot,segment,rotation,dt,binds,history):
        np=self.np
        # Do not change the oracle's exception ordering for unsupported objects.
        if (type(slot)is not int or type(binds)not in(tuple,list)or not 0<=slot<len(binds)or
                type(history)is not dict or type(segment)is not BoneSegment3D or segment.valid is not True or
                type(dt)is not float or type(rotation)is not np.ndarray):return NotImplemented
        previous=history.get(slot);bind=binds[slot]
        if (type(bind)is not np.ndarray or type(previous)is not tuple or len(previous)!=2 or
                any(type(value)is not np.ndarray for value in previous)):return NotImplemented
        result=self._function(segment.start,segment.end,rotation,bind,previous,dt)
        if result is NotImplemented:return NotImplemented
        axis,(local,direction)=result
        return tuple(np.array(axis)),(np.array(local),np.array(direction))
