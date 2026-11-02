"""Optional exact 16-slot normal bases; final view rotation stays in NumPy.

Source/license: project field_preview_gles.py GPL-2.0-only; existing CPython C
API (PSF) and NumPy. Unsupported row types return NotImplemented unchanged.
"""
import ctypes
import importlib.util
from pathlib import Path
import sys


class NativeGlesNormalPreparer:
    def __init__(self,library):
        self.library_path=Path(library).resolve(strict=True)
        self._library=ctypes.PyDLL(str(self.library_path))
        try:
            abi=self._library.posepriv_gles_prep_abi
            python_abi=self._library.posepriv_gles_prep_python_abi
        except AttributeError as error:
            raise ValueError('native GLES prep library is missing ABI functions')from error
        abi.argtypes=[];abi.restype=ctypes.c_uint32
        if abi()!=1:raise ValueError('unsupported native GLES prep ABI')
        python_abi.argtypes=[];python_abi.restype=ctypes.c_uint32
        if python_abi()!=((sys.version_info.major<<16)|sys.version_info.minor):
            raise ValueError('native GLES prep library requires matching CPython major/minor')
        spec=importlib.util.spec_from_file_location('_posepriv_gles_prep',self.library_path)
        self._module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self._module)
        self._function=self._module.inverse_values
        import numpy as np
        self.np=np

    def __call__(self,rows):
        result=self._function(rows)
        if result is NotImplemented:return NotImplemented
        if type(result)is not bytes or len(result)!=16*3*3*4:
            raise ValueError('native GLES normal result must contain 576 immutable bytes')
        # Immutable per-call bytes own the view; later calls cannot reuse them.
        return self.np.frombuffer(result,self.np.float32).reshape(16,3,3)
