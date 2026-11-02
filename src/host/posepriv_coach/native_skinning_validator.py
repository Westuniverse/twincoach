"""Explicit CPU-only adapter of the existing PPS4 C input/output byte contract."""
import ctypes
from pathlib import Path

from .shared_skinning_batches import MATRIX_HEADER_BYTES,AXIS_BEAT_BYTES,RX_CAPACITY


def native_upper64_scan_enabled(config):
    value=config.get('preview_native_upper64_scan',False)
    if type(value) is not bool:raise ValueError('preview_native_upper64_scan must be boolean')
    return value


class NativeSkinningValidator:
    def __init__(self,library_path):
        self.library_path=Path(library_path).resolve(strict=True)
        self._library=ctypes.CDLL(str(self.library_path))
        abi=self._library.posepriv_skinning_validator_abi
        abi.argtypes=[];abi.restype=ctypes.c_uint32
        if abi()!=1:raise ValueError('unsupported skinning validator ABI')
        self._input=self._library.posepriv_skinning_validate_input_v1
        self._output=self._library.posepriv_skinning_validate_output_v1
        for function in (self._input,self._output):
            function.argtypes=[ctypes.c_char_p,ctypes.c_uint32]
            function.restype=ctypes.c_uint32
        self._upper64_zero=None

    def require_upper64_scan(self):
        """Resolve only the explicitly selected CPU candidate; ABI1 defaults stay compatible."""
        if self._upper64_zero is None:
            try:function=self._library.posepriv_skinning_upper64_zero_v1
            except AttributeError as error:
                raise ValueError('native upper64 scan requires posepriv_skinning_upper64_zero_v1') from error
            function.argtypes=[ctypes.c_char_p,ctypes.c_size_t]
            function.restype=ctypes.c_uint32
            self._upper64_zero=function

    def validate_upper64(self,output):
        """Check every upper word in immutable exact bytes, without a temporary copy."""
        if type(output) is not bytes:raise TypeError('native upper64 scan requires exact immutable bytes')
        if len(output)%AXIS_BEAT_BYTES:
            raise ValueError('padded skinning output must contain complete AXI beats')
        self.require_upper64_scan()
        if not self._upper64_zero(output,len(output)):
            raise ValueError('avatar DMA output reserved bytes must be zero')

    def validate_packet(self,payload,*,max_vertices=8192):
        if type(payload) is not bytes:raise TypeError('native skinning packet must be immutable native bytes')
        if len(payload)<528 or len(payload)%16:
            raise ValueError('skinning packet needs 512 header bytes and complete vertices')
        count=(len(payload)-MATRIX_HEADER_BYTES)//AXIS_BEAT_BYTES
        if max_vertices is not None and (type(max_vertices) is not int or max_vertices<1 or count>max_vertices):
            raise ValueError('skinning packet exceeds vertex capacity')
        if count<=RX_CAPACITY//AXIS_BEAT_BYTES:
            if self._input(payload,len(payload))!=count:raise ValueError('skinning input violates native byte contract')
        else:
            # Validate the entire logical frame before any hardware I/O. Reuse
            # the bounded kernel predicate without widening its physical ABI.
            header=payload[:MATRIX_HEADER_BYTES]
            for start in range(MATRIX_HEADER_BYTES,len(payload),RX_CAPACITY):
                packet=header+payload[start:start+RX_CAPACITY]
                if not self._input(packet,len(packet)):raise ValueError('skinning input violates native byte contract')
        return count

    def validate_output(self,output,vertex_count):
        if (type(vertex_count) is not int or not 1<=vertex_count<=RX_CAPACITY//AXIS_BEAT_BYTES
                or type(output) is not bytes or len(output)!=vertex_count*AXIS_BEAT_BYTES):
            raise ValueError('skinning output length/type differs from bounded batch')
        if not self._output(output,len(output)):raise ValueError('skinning output violates native byte contract')

    def validate_frame_into(self,payload,scratch,*,rx_capacity=RX_CAPACITY):
        """Same predicate over every batch before I/O; reuse owned TX scratch."""
        if type(payload)is not bytes:raise TypeError('native skinning packet must be immutable native bytes')
        if len(payload)<528 or len(payload)%AXIS_BEAT_BYTES:
            raise ValueError('skinning packet needs 512 header bytes and complete vertices')
        if(type(rx_capacity)is not int or not AXIS_BEAT_BYTES<=rx_capacity<=RX_CAPACITY
                or rx_capacity%AXIS_BEAT_BYTES):
            raise ValueError('skinning RX capacity must be16-byte aligned within16..131072')
        if(not isinstance(scratch,ctypes.Array)or type(scratch)._type_ is not ctypes.c_char
                or ctypes.sizeof(scratch)<MATRIX_HEADER_BYTES+rx_capacity):
            raise ValueError('owned skinning TX scratch is too small')
        source=ctypes.cast(ctypes.c_char_p(payload),ctypes.c_void_p).value
        destination=ctypes.addressof(scratch)
        pointer=ctypes.cast(scratch,ctypes.c_char_p)
        ctypes.memmove(destination,source,MATRIX_HEADER_BYTES)
        for start in range(MATRIX_HEADER_BYTES,len(payload),rx_capacity):
            size=min(rx_capacity,len(payload)-start)
            ctypes.memmove(destination+MATRIX_HEADER_BYTES,source+start,size)
            if self._input(pointer,MATRIX_HEADER_BYTES+size)!=size//AXIS_BEAT_BYTES:
                raise ValueError('skinning input violates native byte contract')
        return (len(payload)-MATRIX_HEADER_BYTES)//AXIS_BEAT_BYTES

    def validate_output_buffer(self,buffer,vertex_count,*,offset=0):
        """Check a completed RX range directly; caller owns and serializes it."""
        if(type(vertex_count)is not int or not 1<=vertex_count<=RX_CAPACITY//AXIS_BEAT_BYTES
                or type(offset)is not int or offset<0
                or not isinstance(buffer,ctypes.Array)or type(buffer)._type_ is not ctypes.c_char
                or offset+vertex_count*AXIS_BEAT_BYTES>ctypes.sizeof(buffer)):
            raise ValueError('skinning output range differs from bounded RX buffer')
        pointer=ctypes.cast(ctypes.byref(buffer,offset),ctypes.c_char_p)
        if not self._output(pointer,vertex_count*AXIS_BEAT_BYTES):
            raise ValueError('skinning output violates native byte contract')
