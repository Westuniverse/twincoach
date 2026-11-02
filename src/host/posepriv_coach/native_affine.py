"""Opt-in CPU integer affine composer; default planners keep the Python oracle."""
import ctypes
import importlib.util
from pathlib import Path
import sys

from .avatar_skinning import AffineMatrixQ1_14, compose_affine_q1_14


class NativeAffineComposer:
    def __init__(self, library_path):
        self.library_path = Path(library_path).resolve(strict=True)
        # A single fixed 3x4 multiply is tiny; avoid a GIL handoff per bone.
        self._library = ctypes.PyDLL(str(self.library_path))
        abi = self._library.posepriv_affine_compose_abi
        abi.argtypes = []; abi.restype = ctypes.c_uint32
        if abi() != 1:
            raise ValueError('unsupported native affine ABI')
        python_abi = self._library.posepriv_affine_python_abi
        python_abi.argtypes = []; python_abi.restype = ctypes.c_uint32
        if python_abi() != ((sys.version_info.major << 16) | sys.version_info.minor):
            raise ValueError('native affine library requires matching CPython major/minor')
        spec = importlib.util.spec_from_file_location('_posepriv_affine', self.library_path)
        self._module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self._module)
        self._function = self._module.compose
        self._raw_function = self._library.posepriv_affine_compose_v1
        self._raw_function.argtypes = [ctypes.POINTER(ctypes.c_int16)] * 3
        self._raw_function.restype = ctypes.c_uint32
        self._array = ctypes.c_int16 * 12

    @staticmethod
    def _native(matrix):
        return type(matrix) is AffineMatrixQ1_14 and type(matrix.values) is tuple

    def __call__(self, left, right):
        if not self._native(left) or not self._native(right):
            return compose_affine_q1_14(left, right)
        result = self._function(left.values, right.values)
        if result is NotImplemented:
            return compose_affine_q1_14(left, right)
        return AffineMatrixQ1_14(result)
