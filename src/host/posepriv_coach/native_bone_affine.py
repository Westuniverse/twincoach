"""Opt-in strict CPU bone builder; GPL-2.0-only project logic, existing CPython API (PSF).

Reference source: skeleton_pose.py; C implementation and numerical/source notes
live in board_tests/bone_affine.c. No new dependency, model, data or FPGA IP.
All unsupported values retain the Python oracle, without numeric coercion.
"""
import ctypes
import importlib.util
from pathlib import Path
import sys

from .avatar_skinning import AffineMatrixQ1_14
from .skeleton_pose import BoneSegment3D,bone_affine_matrix_q1_14


class NativeBoneAffineBuilder:
    def __init__(self,library_path):
        self.library_path=Path(library_path).resolve(strict=True)
        self._library=ctypes.PyDLL(str(self.library_path))
        abi=self._library.posepriv_bone_affine_abi;abi.argtypes=[];abi.restype=ctypes.c_uint32
        if abi()!=1:raise ValueError('unsupported native bone affine ABI')
        python_abi=self._library.posepriv_bone_affine_python_abi
        python_abi.argtypes=[];python_abi.restype=ctypes.c_uint32
        if python_abi()!=((sys.version_info.major<<16)|sys.version_info.minor):
            raise ValueError('native bone affine library requires matching CPython major/minor')
        spec=importlib.util.spec_from_file_location('_posepriv_bone_affine',self.library_path)
        self._module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self._module)
        self._function=self._module.build

    def __call__(self,segment,*,world_to_model_scale=1.0,local_bone_length_q1_14=None,
                 world_origin=(0.0,0.0,0.0),local_x_reference=None):
        if type(segment)is BoneSegment3D and segment.valid is True and local_x_reference is not None:
            result=self._function(segment.start,segment.end,segment.length,world_to_model_scale,
                world_origin,local_x_reference,local_bone_length_q1_14)
            if result is not NotImplemented:return AffineMatrixQ1_14(result)
        return bone_affine_matrix_q1_14(segment,world_to_model_scale=world_to_model_scale,
            local_bone_length_q1_14=local_bone_length_q1_14,world_origin=world_origin,
            local_x_reference=local_x_reference)
