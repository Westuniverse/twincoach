"""All-influence inverse-transpose normals for an explicit authored display rig.

This is PS work, not an FPGA operator. The GPU normalizes the blended vector.
It is the usual weighted bone-normal approximation, not a surface derivative.
"""
from __future__ import annotations

from .avatar_skinning import AffineMatrixQ1_14
from .avatar_skinning_palettes import WeightedSkinningVertex


class WeightedNormalPlan:
    def __init__(self, vertices, rest_normals, *, bone_count):
        import numpy as np
        vertices = tuple(vertices)
        if (type(bone_count) is not int or bone_count < 1 or not vertices or
                any(not isinstance(v, WeightedSkinningVertex) for v in vertices) or
                any(i >= bone_count for v in vertices for i in v.bone_indices)):
            raise ValueError('weighted vertices and corresponding positive bone count required')
        normals = np.array(rest_normals, dtype=np.float64, copy=True)
        if normals.shape != (len(vertices), 3) or not np.isfinite(normals).all():
            raise ValueError('one finite rest normal per original vertex required')
        source, bones, weights = [], [], []
        for index, vertex in enumerate(vertices):
            for bone, weight in zip(vertex.bone_indices, vertex.weights_q0_15):
                if weight:
                    source.append(index);bones.append(bone);weights.append(weight / 32768.)
        self.bone_count = bone_count
        self.vertex_count = len(vertices)
        self._source = np.array(source, dtype=np.intp)
        self._bones = np.array(bones, dtype=np.intp)
        self._weights = np.array(weights, dtype=np.float64)
        self._normals = normals[self._source]
        self._rest_normals = normals
        self._used = np.unique(self._bones)

    def apply(self, matrices, *, native_ops=None):
        """Reject singular active bones before the caller performs any frame I/O."""
        import numpy as np
        matrices = tuple(matrices)
        if len(matrices) != self.bone_count or any(not isinstance(m, AffineMatrixQ1_14) for m in matrices):
            raise ValueError('one Q1.14 common-bind matrix per global bone required')
        bases = np.array([m.values for m in matrices], dtype=np.float64).reshape(-1, 3, 4)[:, :, :3] / 16384.
        x, y, z = bases[:, :, 0], bases[:, :, 1], bases[:, :, 2]
        a, b, d = np.cross(y, z), np.cross(z, x), np.cross(x, y)
        determinant = x[:, 0]*a[:, 0] + x[:, 1]*a[:, 1] + x[:, 2]*a[:, 2]
        if np.any(determinant[self._used] <= 1.e-8):
            raise ValueError('active normal matrices must be nonsingular and orientation preserving')
        # Only referenced matrices participate: unused bones do not invent normals.
        bases = np.stack((a, b, d), axis=2)
        bases[self._used] /= determinant[self._used, None, None]
        if native_ops is not None:
            return native_ops.apply(self, bases)
        inverse_transpose = bases[self._bones]
        n = self._normals
        transformed = (inverse_transpose[:, :, 0]*n[:, 0, None] +
                       inverse_transpose[:, :, 1]*n[:, 1, None] +
                       inverse_transpose[:, :, 2]*n[:, 2, None]) * self._weights[:, None]
        result = np.zeros((self.vertex_count, 3), dtype=np.float64)
        np.add.at(result, self._source, transformed)
        result = np.ascontiguousarray(result, dtype=np.float32)
        result.flags.writeable = False
        return result


class NativeWeightedNormals:
    """Explicit CPU C ABI1, statically bound to one all-influence normal plan."""
    def __init__(self, plan, library):
        import ctypes as c
        from pathlib import Path
        import numpy as np
        if not isinstance(plan, WeightedNormalPlan):raise TypeError('weighted normal plan required')
        self.plan = plan
        self.library = c.CDLL(str(Path(library).resolve(strict=True)))
        abi = self.library.posepriv_weighted_normals_abi
        abi.argtypes = [];abi.restype = c.c_uint32
        if abi() != 1:raise RuntimeError('weighted normal ABI differs')
        self._apply = self.library.posepriv_weighted_normals_v1
        self._apply.argtypes = [c.c_void_p,c.c_uint32,c.c_void_p,c.c_uint32,
            c.c_void_p,c.c_void_p,c.c_void_p,c.c_void_p,c.c_uint32]
        self._apply.restype = c.c_int
        self._offsets = np.ascontiguousarray(np.r_[0,np.cumsum(
            np.bincount(plan._source,minlength=plan.vertex_count))],dtype=np.uint32)
        self._bones = np.ascontiguousarray(plan._bones,dtype=np.uint32)
        self._weights = np.ascontiguousarray(plan._weights,dtype=np.float64)
        self._normals = np.ascontiguousarray(plan._rest_normals,dtype=np.float64)

    def apply(self, plan, inverse_transpose):
        import numpy as np
        if plan is not self.plan:raise ValueError('native normal metadata belongs to another plan')
        bases = np.ascontiguousarray(inverse_transpose,dtype=np.float64)
        if bases.shape != (plan.bone_count,3,3) or not np.isfinite(bases).all():
            raise ValueError('one finite inverse-transpose matrix per global bone required')
        result = np.empty((plan.vertex_count,3),dtype=np.float32)
        if not self._apply(result.ctypes.data,plan.vertex_count,bases.ctypes.data,plan.bone_count,
                self._normals.ctypes.data,self._offsets.ctypes.data,self._bones.ctypes.data,
                self._weights.ctypes.data,len(self._bones)):
            raise ValueError('native weighted normal blend failed')
        result.flags.writeable=False
        return result
