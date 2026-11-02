"""Bound complete avatar frames to the shared DMA's existing 128 KiB RX buffer."""
from __future__ import annotations

from collections.abc import Callable
import struct
import threading

from .avatar_skinning import MATRIX_COUNT, MATRIX_PACKET_BYTES, AXIS_BEAT_BYTES

RX_CAPACITY = 131072
MATRIX_HEADER_BYTES = MATRIX_COUNT * MATRIX_PACKET_BYTES
_WEIGHTS = struct.Struct("<8x4H")


def validate_skinning_packet(payload: bytes, *, max_vertices: int | None = 8192) -> int:
    """Check the immutable wire contract, without constructing vertex objects."""
    if not isinstance(payload, bytes):
        raise TypeError("skinning packet must be immutable bytes")
    if len(payload) < MATRIX_HEADER_BYTES+AXIS_BEAT_BYTES or len(payload) % AXIS_BEAT_BYTES:
        raise ValueError("skinning packet needs 512 header bytes and complete vertices")
    count = (len(payload)-MATRIX_HEADER_BYTES)//AXIS_BEAT_BYTES
    if max_vertices is not None:
        if type(max_vertices) is not int or max_vertices < 1 or count > max_vertices:
            raise ValueError("skinning packet exceeds vertex capacity")
    for offset in range(0, MATRIX_HEADER_BYTES, MATRIX_PACKET_BYTES):
        if any(payload[offset+24:offset+32]):
            raise ValueError("skinning matrix padding must be zero")
    for weights in _WEIGHTS.iter_unpack(memoryview(payload)[MATRIX_HEADER_BYTES:]):
        if sum(weights) != 32768:
            raise ValueError("skinning weights must sum to 32768")
    return count


def skinning_validation_functions(validator=None):
    if validator is None:return validate_skinning_packet,validate_skinning_output
    packet=getattr(validator,'validate_packet',None)
    output=getattr(validator,'validate_output',None)
    if not callable(packet) or not callable(output):
        raise TypeError('skinning validator must provide packet and output checks')
    return packet,output


def split_skinning_packet(payload: bytes, *, rx_capacity: int = RX_CAPACITY, validator=None) -> tuple[bytes, ...]:
    """Validate the whole frame before returning any independently complete batch."""
    if (type(rx_capacity) is not int or not AXIS_BEAT_BYTES <= rx_capacity <= RX_CAPACITY
            or rx_capacity % AXIS_BEAT_BYTES):
        raise ValueError("skinning RX capacity must be 16-byte aligned within 16..131072")
    packet_check,_ = skinning_validation_functions(validator)
    count = packet_check(payload, max_vertices=None)
    if count*AXIS_BEAT_BYTES <= rx_capacity:
        return (payload,)
    header = payload[:MATRIX_HEADER_BYTES]
    return tuple(header+payload[offset:offset+rx_capacity]
                 for offset in range(MATRIX_HEADER_BYTES, len(payload), rx_capacity))


def validate_skinning_output(output: bytes, vertex_count: int) -> None:
    if (type(vertex_count) is not int or not 1 <= vertex_count <= RX_CAPACITY//AXIS_BEAT_BYTES
            or not isinstance(output, bytes) or len(output) != vertex_count*AXIS_BEAT_BYTES):
        raise ValueError("skinning output length/type differs from bounded batch")
    for offset in range(0, len(output), AXIS_BEAT_BYTES):
        if any(output[offset+6:offset+16]):
            raise ValueError("skinning output reserved fields must be zero")


class BoundedSkinningPacketTransfer:
    """Borrow one batch exchange callable; return a full frame only after success.

    No device opening, retry, recovery or CPU substitution. A transfer failure
    latches this adapter until the caller recovers the device and replaces it.
    """
    def __init__(self, transfer: Callable[[bytes], bytes], *, rx_capacity: int = RX_CAPACITY, validator=None):
        if (type(rx_capacity) is not int or not AXIS_BEAT_BYTES <= rx_capacity <= RX_CAPACITY
                or rx_capacity % AXIS_BEAT_BYTES):
            raise ValueError("skinning RX capacity must be 16-byte aligned within 16..131072")
        if not callable(transfer):
            raise TypeError("skinning transfer must be callable")
        self._transfer = transfer
        _,self._validate_output=skinning_validation_functions(validator)
        self._validator=validator
        self._rx_capacity = rx_capacity
        self._lock = threading.Lock()
        self._faulted = False

    def __call__(self, payload: bytes) -> bytes:
        batches = split_skinning_packet(payload, rx_capacity=self._rx_capacity,validator=self._validator)
        with self._lock:
            if self._faulted:
                raise RuntimeError("skinning batch adapter faulted; recover device before replacing it")
            outputs = []
            try:
                for batch in batches:
                    output = self._transfer(batch)
                    self._validate_output(output, (len(batch)-MATRIX_HEADER_BYTES)//AXIS_BEAT_BYTES)
                    outputs.append(output)
            except BaseException:
                self._faulted = True
                raise
            return b"".join(outputs)


class ReusedSkinningFrameTransfer:
    """Explicit whole-frame candidate; transport owns validation/lock/faults.

    The default bounded packet adapter stays unchanged. The selected transport
    checks every unknown frame before I/O and every output batch before return.
    """
    def __init__(self,transport,*,rx_capacity=RX_CAPACITY):
        exchange=getattr(transport,'exchange_skinning_frame',None)
        if not callable(exchange):raise TypeError('whole-frame skinning transport required')
        self._exchange=exchange;self._rx_capacity=rx_capacity

    def __call__(self,payload:bytes)->bytes:
        return self._exchange(payload,rx_capacity=self._rx_capacity)
