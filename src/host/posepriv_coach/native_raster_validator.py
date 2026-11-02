"""Explicit CPU-only PPS3 request validator using the existing C byte contract."""
import ctypes
from pathlib import Path
from .avatar_raster_packet import RASTER_DMA_CAPACITY


class NativeRasterRequestValidator:
    def __init__(self,library_path):
        self.library_path=Path(library_path).resolve(strict=True)
        self._library=ctypes.CDLL(str(self.library_path))
        abi=self._library.posepriv_raster_validator_abi
        abi.argtypes=[];abi.restype=ctypes.c_uint32
        if abi()!=1:raise ValueError('unsupported raster request validator ABI')
        self._run=self._library.posepriv_raster_validate_v1
        self._run.argtypes=[ctypes.c_char_p,ctypes.c_uint32]
        self._run.restype=ctypes.c_uint32

    def __call__(self,payload):
        if type(payload) is not bytes or not 16<=len(payload)<=RASTER_DMA_CAPACITY or len(payload)%16:
            raise ValueError('PPS3 request requires complete immutable bytes within DMA capacity')
        # c_char_p borrows immutable bytes, including embedded NULs. The explicit
        # length governs C reads; ctypes keeps the original alive across the call.
        output=self._run(payload,len(payload))
        if not output:raise ValueError('PPS3 packet violates native byte contract')
        return payload[4],int(output)
