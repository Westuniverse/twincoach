"""Explicit CPU rig math; project GPL-2.0-only, existing CPython API (PSF)/NumPy.

References: preview_rigid_avatar._matmul and PreviewRigidAvatar._canonical.
The C methods retain original Python fallbacks and owned outputs. Unsupported
types are passed to those oracles unchanged; no coercion or temporal state.
"""
import ctypes
import importlib.util
from pathlib import Path
import sys


def rig_math_library(cfg):
    value=cfg.get('preview_rig_math_library')
    if value is not None and (type(value)is not str or not value):
        raise ValueError('preview_rig_math_library must be a nonempty path string or None')
    return value


class NativeRigMath:
    def __init__(self,library_path):
        self.library_path=Path(library_path).resolve(strict=True)
        self._library=ctypes.PyDLL(str(self.library_path))
        abi=self._library.posepriv_rig_math_abi;abi.argtypes=[];abi.restype=ctypes.c_uint32
        if abi()!=1:raise ValueError('unsupported native rig math ABI')
        python_abi=self._library.posepriv_rig_math_python_abi
        python_abi.argtypes=[];python_abi.restype=ctypes.c_uint32
        if python_abi()!=((sys.version_info.major<<16)|sys.version_info.minor):
            raise ValueError('native rig math library requires matching CPython major/minor')
        spec=importlib.util.spec_from_file_location('_posepriv_rig_math',self.library_path)
        self._module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self._module)
        from .preview_rigid_avatar import _matmul,PreviewRigidAvatar
        self._module.setup(_matmul,PreviewRigidAvatar._canonical)
        # The rig stores these C-bound methods directly; no Python hot wrapper.
        self.matmul=self._module.matmul
        self.canonical=self._module.canonical
