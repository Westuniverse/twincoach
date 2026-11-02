"""3D 数字人顶点蒙皮的位精确软件参考。"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import InitVar, dataclass, field
import math
from struct import Struct, iter_unpack
from typing import Any, Callable, Iterable, Protocol, overload

from .pl_metrics import AXIS_BEAT_BYTES, AxisBeat


TRANSFORMED_BITS = 18
TRANSFORMED_FRACTION_BITS = 14
TRANSFORMED_MIN = -(1 << (TRANSFORMED_BITS - 1))
TRANSFORMED_MAX = (1 << (TRANSFORMED_BITS - 1)) - 1
SKIN_WEIGHT_BITS = 16
SKIN_WEIGHT_FRACTION_BITS = 15
SKIN_WEIGHT_SUM = 1 << SKIN_WEIGHT_FRACTION_BITS
SKIN_OUTPUT_FRACTION_BITS = 14
SKIN_OUTPUT_MIN = -(1 << 15)
SKIN_OUTPUT_MAX = (1 << 15) - 1
SKIN_ROUND_SHIFT = (
    TRANSFORMED_FRACTION_BITS + SKIN_WEIGHT_FRACTION_BITS - SKIN_OUTPUT_FRACTION_BITS
)
MATRIX_WORD_BYTES = 24
MATRIX_PACKET_BEATS = 2
MATRIX_PACKET_BYTES = MATRIX_PACKET_BEATS * AXIS_BEAT_BYTES
SKIN_OUTPUT_BYTES = 8
MATRIX_COUNT = 16
MATRIX_INDEX_COUNT = 4
AFFINE_INPUT_MIN = -(1 << 15)
AFFINE_INPUT_MAX = (1 << 15) - 1
AFFINE_PRODUCT_FRACTION_BITS = 28
AFFINE_TO_TRANSFORMED_SHIFT = AFFINE_PRODUCT_FRACTION_BITS - TRANSFORMED_FRACTION_BITS
_SKIN_OUTPUT_WORD_STRUCT = Struct("<Q")
_SKIN_OUTPUT_POINT_STRUCT = Struct("<hhh2x")
_MATRIX_WORD_STRUCT = Struct("<12h")
_NUMPY_RIGID_MIN_VERTICES = 256


@dataclass(frozen=True, slots=True)
class VertexQ1_14:
    x: int
    y: int
    z: int

    def __post_init__(self) -> None:
        if any(
            value < AFFINE_INPUT_MIN or value > AFFINE_INPUT_MAX
            for value in (self.x, self.y, self.z)
        ):
            raise ValueError("vertex must fit signed 16 bit")


@dataclass(frozen=True, slots=True)
class AffineMatrixQ1_14:
    values: tuple[int, int, int, int, int, int, int, int, int, int, int, int]

    def __post_init__(self) -> None:
        if len(self.values) != 12:
            raise ValueError("affine matrix must contain twelve coefficients")
        if any(value < AFFINE_INPUT_MIN or value > AFFINE_INPUT_MAX for value in self.values):
            raise ValueError("affine matrix coefficient must fit signed 16 bit")

    @classmethod
    def identity(cls) -> "AffineMatrixQ1_14":
        one = 1 << 14
        return cls((one, 0, 0, 0, 0, one, 0, 0, 0, 0, one, 0))


def compose_affine_q1_14(
    left: AffineMatrixQ1_14,
    right: AffineMatrixQ1_14,
) -> AffineMatrixQ1_14:
    """Return ``left × right`` using the runtime's row-major 3x4 ABI.

    Composition is performed with a wide Q2.28 accumulator and the same
    symmetric rounding convention as the point datapath. Overflow is rejected
    instead of silently saturating a bone matrix.
    """

    left_values = left.values
    right_values = right.values
    result: list[int] = []
    for row in range(3):
        left_base = row * 4
        for column in range(3):
            accumulated = (0
                + left_values[left_base] * right_values[column]
                + left_values[left_base + 1] * right_values[4 + column]
                + left_values[left_base + 2] * right_values[8 + column])
            result.append(_checked_affine_coefficient(accumulated))
        translated = (
            (0
                + left_values[left_base] * right_values[3]
                + left_values[left_base + 1] * right_values[7]
                + left_values[left_base + 2] * right_values[11])
            + (left_values[left_base + 3] << 14)
        )
        result.append(_checked_affine_coefficient(translated))
    return AffineMatrixQ1_14(tuple(result))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class TransformedPointQ3_14:
    x: int
    y: int
    z: int

    def __post_init__(self) -> None:
        if any(
            value < TRANSFORMED_MIN or value > TRANSFORMED_MAX
            for value in (self.x, self.y, self.z)
        ):
            raise ValueError("transformed point must fit signed 18 bit")


@dataclass(frozen=True, slots=True)
class SkinnedPointQ1_14:
    x: int
    y: int
    z: int


@dataclass(frozen=True, slots=True)
class VertexSkinningRequest:
    """One vertex transaction matching ``vertex_skinning_axis`` input TDATA."""

    vertex: VertexQ1_14
    matrix_indices: tuple[int, int, int, int]
    weights_q0_15: tuple[int, int, int, int]

    def __post_init__(self) -> None:
        if len(self.matrix_indices) != MATRIX_INDEX_COUNT:
            raise ValueError("exactly four matrix indices are required")
        if any(index < 0 or index >= MATRIX_COUNT for index in self.matrix_indices):
            raise ValueError("matrix index must fit the 16-entry matrix bank")
        if len(self.weights_q0_15) != MATRIX_INDEX_COUNT:
            raise ValueError("exactly four skinning weights are required")
        if any(weight < 0 or weight > SKIN_WEIGHT_SUM for weight in self.weights_q0_15):
            raise ValueError("Q0.15 skinning weight is out of range")
        if sum(self.weights_q0_15) != SKIN_WEIGHT_SUM:
            raise ValueError("Q0.15 skinning weights must sum to 32768")


@dataclass(frozen=True, slots=True)
class MatrixWrite:
    """One 192-bit row-major matrix-bank write."""

    index: int
    matrix: AffineMatrixQ1_14

    def __post_init__(self) -> None:
        if not 0 <= self.index < MATRIX_COUNT:
            raise ValueError("matrix index must fit the 16-entry matrix bank")


@dataclass(frozen=True, slots=True)
class SkinningOutputBeat:
    """One 64-bit output transaction from ``vertex_skinning_axis``."""

    data: bytes
    last: bool = False

    def __post_init__(self) -> None:
        if len(self.data) != SKIN_OUTPUT_BYTES:
            raise ValueError("skinning output beat must contain 8 bytes")


@dataclass(frozen=True, slots=True)
class PackedSkinningOutputBatch(Sequence[SkinningOutputBeat]):
    """Validated 16-byte-stride DMA output with lazy beat materialization."""

    padded_data: bytes
    upper64_check: InitVar[Callable[[bytes], None] | None] = field(default=None, kw_only=True)

    def __post_init__(self, upper64_check) -> None:
        if len(self.padded_data) % AXIS_BEAT_BYTES != 0:
            raise ValueError("padded skinning output must contain complete AXI beats")
        if upper64_check is not None and type(self.padded_data) is bytes:
            upper64_check(self.padded_data)
            return
        # Compact only the upper words, then scan every byte in C. Include the
        # temporary copy in performance measurements; the original DMA payload
        # is retained unchanged. Zero-byte count is independent of endianness.
        reserved = memoryview(self.padded_data).cast("Q")[1::2].tobytes()
        if reserved.count(0) != len(reserved):
            raise ValueError("avatar DMA output reserved bytes must be zero")

    def __len__(self) -> int:
        return len(self.padded_data) // AXIS_BEAT_BYTES

    @overload
    def __getitem__(self, index: int) -> SkinningOutputBeat: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[SkinningOutputBeat, ...]: ...

    def __getitem__(
        self,
        index: int | slice,
    ) -> SkinningOutputBeat | tuple[SkinningOutputBeat, ...]:
        beat_count = len(self)
        if isinstance(index, slice):
            return tuple(self[item] for item in range(*index.indices(beat_count)))
        resolved_index = index + beat_count if index < 0 else index
        if resolved_index < 0 or resolved_index >= beat_count:
            raise IndexError("skinning output batch index out of range")
        offset = resolved_index * AXIS_BEAT_BYTES
        return SkinningOutputBeat(
            self.padded_data[offset : offset + SKIN_OUTPUT_BYTES],
            last=resolved_index == beat_count - 1,
        )

    def __iter__(self) -> Iterator[SkinningOutputBeat]:
        last_index = len(self) - 1
        for index, (data,) in enumerate(iter_unpack("<8s8x", self.padded_data)):
            yield SkinningOutputBeat(data, last=index == last_index)

    def unpack_points(self) -> tuple[SkinnedPointQ1_14, ...]:
        """Decode signed Q1.14 points without first constructing output beats."""

        points: list[SkinnedPointQ1_14] = []
        for (word,) in iter_unpack("<Q8x", self.padded_data):
            if word >> 48:
                raise ValueError("skinning output reserved bits must be zero")
            points.append(
                SkinnedPointQ1_14(
                    _signed_field(word & 0xFFFF, 16),
                    _signed_field((word >> 16) & 0xFFFF, 16),
                    _signed_field((word >> 32) & 0xFFFF, 16),
                )
            )
        return tuple(points)

    def float_points(self) -> tuple[tuple[float, float, float], ...]:
        """Decode signed Q1.14 points directly for the display state."""

        points: list[tuple[float, float, float]] = []
        q14 = 1 << 14
        for point_x, point_y, point_z, reserved in iter_unpack(
            "<hhhH8x", self.padded_data
        ):
            if reserved:
                raise ValueError("skinning output reserved bits must be zero")
            points.append((point_x / q14, point_y / q14, point_z / q14))
        return tuple(points)


class VertexSkinningBackend(Protocol):
    """Common PS reference/DMA exchange contract for a vertex batch."""

    def exchange(
        self, requests: tuple[VertexSkinningRequest, ...]
    ) -> Sequence[SkinningOutputBeat]: ...


def quantize_skinning_weights(weights: Iterable[float]) -> tuple[int, int, int, int]:
    """把四个非负权重归一化为总和严格等于 32768 的 Q0.15。"""

    values = tuple(weights)
    if len(values) != 4:
        raise ValueError("exactly four skinning weights are required")
    if any(not math.isfinite(value) or value < 0.0 for value in values):
        raise ValueError("skinning weights must be finite and non-negative")
    total = sum(values)
    if total <= 0.0:
        raise ValueError("at least one skinning weight must be positive")

    scaled = tuple(value * SKIN_WEIGHT_SUM / total for value in values)
    quantized = [math.floor(value) for value in scaled]
    remainder = SKIN_WEIGHT_SUM - sum(quantized)
    order = sorted(range(4), key=lambda index: (scaled[index] - quantized[index], -index), reverse=True)
    for index in order[:remainder]:
        quantized[index] += 1
    return tuple(quantized)  # type: ignore[return-value]


def pack_matrix_write(write: MatrixWrite) -> bytes:
    """Pack one matrix-bank write in the RTL's little-endian coefficient order."""

    return _MATRIX_WORD_STRUCT.pack(*write.matrix.values)


def unpack_matrix_write(index: int, data: bytes) -> MatrixWrite:
    """Decode one 24-byte matrix-bank payload."""

    if len(data) != MATRIX_WORD_BYTES:
        raise ValueError("matrix write must contain 24 bytes")
    values = _MATRIX_WORD_STRUCT.unpack(data)
    return MatrixWrite(index, AffineMatrixQ1_14(values))


def pack_matrix_packet_record(write: MatrixWrite) -> bytes:
    """Pack one matrix as the two 128-bit preamble beats used by the packet RTL."""

    packed = pack_matrix_write(write)
    reserved_bytes = AXIS_BEAT_BYTES - (MATRIX_WORD_BYTES - AXIS_BEAT_BYTES)
    return packed + bytes(reserved_bytes)


def pack_skinning_packet(
    matrix_writes: tuple[MatrixWrite, ...],
    requests: tuple[VertexSkinningRequest, ...],
) -> bytes:
    """Pack a complete matrix-preamble plus vertex batch for one AXI DMA call."""

    if len(matrix_writes) != MATRIX_COUNT:
        raise ValueError("packet preamble must contain all 16 matrix writes")
    if tuple(write.index for write in matrix_writes) != tuple(range(MATRIX_COUNT)):
        raise ValueError("packet matrix writes must be ordered from index 0 to 15")
    matrix_payload = b"".join(pack_matrix_packet_record(write) for write in matrix_writes)
    vertex_payload = b"".join(pack_vertex_skinning_request(request).data for request in requests)
    return matrix_payload + vertex_payload


def pack_vertex_skinning_request(
    request: VertexSkinningRequest, *, last: bool = False
) -> AxisBeat:
    """Pack a vertex request into the 128-bit ``vertex_skinning_axis`` layout."""

    word = 0
    for shift, value in zip((0, 16, 32), (request.vertex.x, request.vertex.y, request.vertex.z), strict=True):
        word |= (value & 0xFFFF) << shift
    for shift, index in zip((48, 52, 56, 60), request.matrix_indices, strict=True):
        word |= index << shift
    for shift, weight in zip((64, 80, 96, 112), request.weights_q0_15, strict=True):
        word |= weight << shift
    return AxisBeat(word.to_bytes(AXIS_BEAT_BYTES, "little"), last)


def unpack_vertex_skinning_request(beat: AxisBeat) -> VertexSkinningRequest:
    """Decode a 16-byte vertex request and validate its fixed-point contract."""

    if len(beat.data) != AXIS_BEAT_BYTES:
        raise ValueError("vertex AXI beat must contain 16 bytes")
    word = int.from_bytes(beat.data, "little")
    vertex = VertexQ1_14(
        *(
            _signed_field((word >> shift) & 0xFFFF, 16)
            for shift in (0, 16, 32)
        )
    )
    indices = tuple((word >> shift) & 0xF for shift in (48, 52, 56, 60))
    weights = tuple((word >> shift) & 0xFFFF for shift in (64, 80, 96, 112))
    return VertexSkinningRequest(vertex, indices, weights)  # type: ignore[arg-type]


def pack_skinned_point(point: SkinnedPointQ1_14) -> bytes:
    """Pack the 64-bit output as x/y/z little-endian signed Q1.14 fields."""

    word = (point.x & 0xFFFF) | ((point.y & 0xFFFF) << 16) | ((point.z & 0xFFFF) << 32)
    return word.to_bytes(SKIN_OUTPUT_BYTES, "little")


def unpack_skinned_point(data: bytes) -> SkinnedPointQ1_14:
    """Decode an 8-byte output transaction; upper 16 bits are reserved zero."""

    if len(data) != SKIN_OUTPUT_BYTES:
        raise ValueError("skinning output must contain 8 bytes")
    word = int.from_bytes(data, "little")
    if (word >> 48) != 0:
        raise ValueError("skinning output reserved bits must be zero")
    return SkinnedPointQ1_14(
        _signed_field(word & 0xFFFF, 16),
        _signed_field((word >> 16) & 0xFFFF, 16),
        _signed_field((word >> 32) & 0xFFFF, 16),
    )


def skin_vertex_from_bank(
    request: VertexSkinningRequest,
    matrix_bank: tuple[AffineMatrixQ1_14, ...],
) -> SkinnedPointQ1_14:
    """Run the same indexed four-matrix operation as the PL wrapper."""

    if len(matrix_bank) != MATRIX_COUNT:
        raise ValueError("matrix bank must contain exactly 16 entries")
    matrices = tuple(matrix_bank[index] for index in request.matrix_indices)
    return skin_vertex(request.vertex, matrices, request.weights_q0_15)  # type: ignore[arg-type]


def _pack_validated_skinned_vertex_from_bank(
    request: VertexSkinningRequest,
    matrix_bank: tuple[AffineMatrixQ1_14, ...],
) -> bytes:
    """Fuse the validated reference hot loop without replacing its oracle.

    ``VertexSkinningRequest`` and the matrix-bank owners already validate all
    ranges.  Keeping this helper private lets tests compare it through the
    backend against ``skin_vertex_from_bank`` while avoiding intermediate
    point objects and transforms whose weights are exactly zero.
    """

    return _pack_skinned_fields_from_bank(
        request.vertex.x,
        request.vertex.y,
        request.vertex.z,
        request.matrix_indices,
        request.weights_q0_15,
        matrix_bank,
    )


def _pack_skinned_fields_from_bank(
    vertex_x: int,
    vertex_y: int,
    vertex_z: int,
    matrix_indices: tuple[int, int, int, int],
    weights_q0_15: tuple[int, int, int, int],
    matrix_bank: tuple[AffineMatrixQ1_14, ...],
) -> bytes:
    """Pack already range-checked ABI fields through the fused reference loop."""

    full_weight_matrix_index: int | None = None
    if weights_q0_15[0] == SKIN_WEIGHT_SUM:
        full_weight_matrix_index = matrix_indices[0]
    elif weights_q0_15[1] == SKIN_WEIGHT_SUM:
        full_weight_matrix_index = matrix_indices[1]
    elif weights_q0_15[2] == SKIN_WEIGHT_SUM:
        full_weight_matrix_index = matrix_indices[2]
    elif weights_q0_15[3] == SKIN_WEIGHT_SUM:
        full_weight_matrix_index = matrix_indices[3]
    if full_weight_matrix_index is not None:
        values = matrix_bank[full_weight_matrix_index].values
        # Validated rigid fields need no point objects or tiny per-axis calls.
        # Retain symmetric half-away-from-zero rounding (including -0.5 LSB)
        # and the same output bounds; the independent canonical oracle stays
        # untouched. The general four-weight path below is also unchanged.
        shift = AFFINE_TO_TRANSFORMED_SHIFT
        half = 1 << (shift - 1)
        raw_x = (
            values[0] * vertex_x + values[1] * vertex_y
            + values[2] * vertex_z + (values[3] << 14)
        )
        raw_y = (
            values[4] * vertex_x + values[5] * vertex_y
            + values[6] * vertex_z + (values[7] << 14)
        )
        raw_z = (
            values[8] * vertex_x + values[9] * vertex_y
            + values[10] * vertex_z + (values[11] << 14)
        )
        rounded_x = ((raw_x + half) >> shift) if raw_x >= 0 else -((-raw_x + half) >> shift)
        rounded_y = ((raw_y + half) >> shift) if raw_y >= 0 else -((-raw_y + half) >> shift)
        rounded_z = ((raw_z + half) >> shift) if raw_z >= 0 else -((-raw_z + half) >> shift)
        output_x = (
            SKIN_OUTPUT_MIN if rounded_x < SKIN_OUTPUT_MIN
            else SKIN_OUTPUT_MAX if rounded_x > SKIN_OUTPUT_MAX else rounded_x
        )
        output_y = (
            SKIN_OUTPUT_MIN if rounded_y < SKIN_OUTPUT_MIN
            else SKIN_OUTPUT_MAX if rounded_y > SKIN_OUTPUT_MAX else rounded_y
        )
        output_z = (
            SKIN_OUTPUT_MIN if rounded_z < SKIN_OUTPUT_MIN
            else SKIN_OUTPUT_MAX if rounded_z > SKIN_OUTPUT_MAX else rounded_z
        )
        # All three fields are already saturated signed16. Preserve the exact
        # eight-byte little-endian ABI without assembling a Python wide word.
        return _SKIN_OUTPUT_POINT_STRUCT.pack(output_x, output_y, output_z)

    accumulated_x = 0
    accumulated_y = 0
    accumulated_z = 0
    for matrix_index, weight in zip(
        matrix_indices,
        weights_q0_15,
        strict=True,
    ):
        if weight == 0:
            continue
        values = matrix_bank[matrix_index].values
        transformed_x = min(
            TRANSFORMED_MAX,
            max(
                TRANSFORMED_MIN,
                _round_shift_away_from_zero(
                    values[0] * vertex_x
                    + values[1] * vertex_y
                    + values[2] * vertex_z
                    + (values[3] << 14),
                    AFFINE_TO_TRANSFORMED_SHIFT,
                ),
            ),
        )
        transformed_y = min(
            TRANSFORMED_MAX,
            max(
                TRANSFORMED_MIN,
                _round_shift_away_from_zero(
                    values[4] * vertex_x
                    + values[5] * vertex_y
                    + values[6] * vertex_z
                    + (values[7] << 14),
                    AFFINE_TO_TRANSFORMED_SHIFT,
                ),
            ),
        )
        transformed_z = min(
            TRANSFORMED_MAX,
            max(
                TRANSFORMED_MIN,
                _round_shift_away_from_zero(
                    values[8] * vertex_x
                    + values[9] * vertex_y
                    + values[10] * vertex_z
                    + (values[11] << 14),
                    AFFINE_TO_TRANSFORMED_SHIFT,
                ),
            ),
        )
        accumulated_x += transformed_x * weight
        accumulated_y += transformed_y * weight
        accumulated_z += transformed_z * weight

    output_x = _saturate_q1_14(
        _round_shift_away_from_zero(accumulated_x, SKIN_ROUND_SHIFT)
    )
    output_y = _saturate_q1_14(
        _round_shift_away_from_zero(accumulated_y, SKIN_ROUND_SHIFT)
    )
    output_z = _saturate_q1_14(
        _round_shift_away_from_zero(accumulated_z, SKIN_ROUND_SHIFT)
    )
    return _SKIN_OUTPUT_POINT_STRUCT.pack(output_x, output_y, output_z)


def _exchange_validated_reference_requests(
    requests: tuple[VertexSkinningRequest, ...],
    matrix_bank: tuple[AffineMatrixQ1_14, ...],
) -> tuple[SkinningOutputBeat, ...]:
    request_count = len(requests)
    return tuple(
        SkinningOutputBeat(
            _pack_validated_skinned_vertex_from_bank(request, matrix_bank),
            last=index == request_count - 1,
        )
        for index, request in enumerate(requests)
    )


@dataclass(frozen=True, slots=True)
class ReferenceVertexSkinningBackend:
    """Bit-accurate PS fallback used before real AXI DMA is connected."""

    matrix_bank: tuple[AffineMatrixQ1_14, ...]

    def __post_init__(self) -> None:
        if len(self.matrix_bank) != MATRIX_COUNT:
            raise ValueError("matrix bank must contain exactly 16 entries")

    def exchange(
        self, requests: tuple[VertexSkinningRequest, ...]
    ) -> tuple[SkinningOutputBeat, ...]:
        return _exchange_validated_reference_requests(requests, self.matrix_bank)


@dataclass(slots=True)
class ProgrammableReferenceVertexSkinningBackend:
    """Frame-programmable PS reference with the same ownership rule as DMA."""

    _matrix_bank: tuple[AffineMatrixQ1_14, ...] | None = field(default=None, init=False)

    def program_matrices(self, writes: tuple[MatrixWrite, ...]) -> None:
        if len(writes) != MATRIX_COUNT:
            raise ValueError("reference backend requires all 16 matrix writes")
        if tuple(write.index for write in writes) != tuple(range(MATRIX_COUNT)):
            raise ValueError("reference matrix writes must be ordered from index 0 to 15")
        self._matrix_bank = tuple(write.matrix for write in writes)

    def exchange(
        self, requests: tuple[VertexSkinningRequest, ...]
    ) -> tuple[SkinningOutputBeat, ...]:
        if self._matrix_bank is None:
            raise ValueError("program_matrices must be called before exchange")
        matrix_bank = self._matrix_bank
        self._matrix_bank = None
        return _exchange_validated_reference_requests(requests, matrix_bank)


@dataclass(frozen=True, slots=True)
class PackedDmaVertexSkinningBackend:
    """Adapt a fixed byte transport to the AXI vertex batch contract."""

    transfer: Callable[[bytes], bytes]

    def exchange(
        self, requests: tuple[VertexSkinningRequest, ...]
    ) -> tuple[SkinningOutputBeat, ...]:
        if not requests:
            return ()
        payload = b"".join(pack_vertex_skinning_request(request).data for request in requests)
        output = self.transfer(payload)
        expected_bytes = len(requests) * SKIN_OUTPUT_BYTES
        if len(output) != expected_bytes:
            raise ValueError("DMA output length does not match vertex batch")
        return tuple(
            SkinningOutputBeat(
                output[index * SKIN_OUTPUT_BYTES : (index + 1) * SKIN_OUTPUT_BYTES],
                last=index == len(requests) - 1,
            )
            for index in range(len(requests))
        )


@dataclass(slots=True)
class PackedDmaAvatarSkinningBackend:
    """DMA backend for ``avatar_skinning_packet_axis``.

    The matrix preamble is retained only until the next exchange. This mirrors
    the frame ownership rule in the RTL: a vertex batch must never observe a
    half-programmed matrix table.
    """

    transfer: Callable[[bytes], bytes]
    upper64_check: Callable[[bytes], None] | None = field(default=None, repr=False, compare=False, kw_only=True)
    _matrix_writes: tuple[MatrixWrite, ...] | None = field(default=None, init=False)
    _cached_requests: tuple[VertexSkinningRequest, ...] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _cached_vertex_payload: bytes | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def program_matrices(self, writes: tuple[MatrixWrite, ...]) -> None:
        """Stage one complete ordered matrix table for the next batch."""

        if len(writes) != MATRIX_COUNT:
            raise ValueError("packet backend requires all 16 matrix writes")
        if tuple(write.index for write in writes) != tuple(range(MATRIX_COUNT)):
            raise ValueError("packet matrix writes must be ordered from index 0 to 15")
        self._matrix_writes = tuple(writes)

    def exchange(
        self, requests: tuple[VertexSkinningRequest, ...]
    ) -> Sequence[SkinningOutputBeat]:
        if self._matrix_writes is None:
            raise ValueError("program_matrices must be called before exchange")
        if not requests:
            self._matrix_writes = None
            return ()
        if not (type(requests) is tuple and requests is self._cached_requests) and requests != self._cached_requests:
            self._cached_requests = requests
            self._cached_vertex_payload = b"".join(
                pack_vertex_skinning_request(request).data for request in requests
            )
        assert self._cached_vertex_payload is not None
        matrix_payload = b"".join(
            pack_matrix_packet_record(write) for write in self._matrix_writes
        )
        payload = matrix_payload + self._cached_vertex_payload
        self._matrix_writes = None
        output = self.transfer(payload)
        expected_bytes = len(requests) * AXIS_BEAT_BYTES
        if len(output) != expected_bytes:
            raise ValueError("DMA output length does not match padded avatar batch")
        return PackedSkinningOutputBatch(output, upper64_check=self.upper64_check)


@dataclass(slots=True)
class _NumpyRigidVertexBatch:
    matrix_indices: Any = field(repr=False, compare=False)
    vertices: Any = field(repr=False, compare=False)


@dataclass(slots=True)
class ReferencePacketAvatarTransport:
    """在主机上解析完整头像 DMA 包并调用位精确参考蒙皮。

    该类只用于 ABI 回放和软件—RTL 对照，不模拟 DDR、缓存维护或 AXI 延迟。
    真正接板时可以把同一个 ``transfer`` 边界替换为 A53/Linux DMA 实现。
    """

    enable_numpy_acceleration: bool = True
    numpy_warmup_vertex_count: int = 0
    _cached_vertex_payload: bytes | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _cached_vertex_fields: tuple[
        tuple[
            int,
            int,
            int,
            tuple[int, int, int, int],
            tuple[int, int, int, int],
        ],
        ...,
    ] | None = field(default=None, init=False, repr=False, compare=False)
    _cached_rigid_vertex_groups: tuple[
        tuple[tuple[int, int, int, int], ...],
        ...,
    ] | None = field(default=None, init=False, repr=False, compare=False)
    _cached_numpy_rigid_vertex_batch: _NumpyRigidVertexBatch | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _numpy_module: Any | None = field(default=None, init=False, repr=False, compare=False)
    _numpy_import_attempted: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.numpy_warmup_vertex_count < 0:
            raise ValueError("NumPy warmup vertex count must be non-negative")
        if self.numpy_warmup_vertex_count >= _NUMPY_RIGID_MIN_VERTICES:
            self._optional_numpy()

    def _optional_numpy(self) -> Any | None:
        if not self.enable_numpy_acceleration:
            return None
        if not self._numpy_import_attempted:
            self._numpy_import_attempted = True
            try:
                import numpy as numpy_module  # type: ignore[import-not-found]
            except ImportError:
                numpy_module = None
            self._numpy_module = numpy_module
        return self._numpy_module

    def transfer(self, payload: bytes) -> bytes:
        matrix_bytes = MATRIX_COUNT * MATRIX_PACKET_BYTES
        if len(payload) < matrix_bytes:
            raise ValueError("avatar packet is shorter than the matrix preamble")
        vertex_payload = payload[matrix_bytes:]
        if len(vertex_payload) % AXIS_BEAT_BYTES != 0:
            raise ValueError("avatar vertex payload must contain complete AXI beats")
        writes = tuple(
            unpack_matrix_write(
                index,
                payload[
                    index * MATRIX_PACKET_BYTES : index * MATRIX_PACKET_BYTES + MATRIX_WORD_BYTES
                ],
            )
            for index in range(MATRIX_COUNT)
        )
        matrix_bank = tuple(write.matrix for write in writes)
        if vertex_payload != self._cached_vertex_payload:
            rigid_vertex_groups: list[list[tuple[int, int, int, int]]] = [
                [] for _ in range(MATRIX_COUNT)
            ]
            all_vertices_rigid = True
            decoded_fields: list[
                tuple[
                    int,
                    int,
                    int,
                    tuple[int, int, int, int],
                    tuple[int, int, int, int],
                ]
            ] = []
            for offset in range(0, len(vertex_payload), AXIS_BEAT_BYTES):
                word = int.from_bytes(
                    vertex_payload[offset : offset + AXIS_BEAT_BYTES],
                    "little",
                )
                matrix_indices = tuple(
                    (word >> shift) & 0xF for shift in (48, 52, 56, 60)
                )
                weights_q0_15 = tuple(
                    (word >> shift) & 0xFFFF for shift in (64, 80, 96, 112)
                )
                if sum(weights_q0_15) != SKIN_WEIGHT_SUM:
                    raise ValueError("skinning weights must sum to 32768")
                vertex_x = _signed_field(word & 0xFFFF, 16)
                vertex_y = _signed_field((word >> 16) & 0xFFFF, 16)
                vertex_z = _signed_field((word >> 32) & 0xFFFF, 16)
                decoded_fields.append(
                    (
                        vertex_x,
                        vertex_y,
                        vertex_z,
                        matrix_indices,  # type: ignore[arg-type]
                        weights_q0_15,  # type: ignore[arg-type]
                    )
                )
                full_weight_slot: int | None = None
                if weights_q0_15[0] == SKIN_WEIGHT_SUM:
                    full_weight_slot = 0
                elif weights_q0_15[1] == SKIN_WEIGHT_SUM:
                    full_weight_slot = 1
                elif weights_q0_15[2] == SKIN_WEIGHT_SUM:
                    full_weight_slot = 2
                elif weights_q0_15[3] == SKIN_WEIGHT_SUM:
                    full_weight_slot = 3
                if full_weight_slot is None:
                    all_vertices_rigid = False
                else:
                    rigid_vertex_groups[matrix_indices[full_weight_slot]].append(
                        (offset, vertex_x, vertex_y, vertex_z)
                    )
            self._cached_vertex_payload = vertex_payload
            self._cached_vertex_fields = tuple(decoded_fields)
            self._cached_rigid_vertex_groups = (
                tuple(tuple(group) for group in rigid_vertex_groups)
                if all_vertices_rigid
                else None
            )
            self._cached_numpy_rigid_vertex_batch = None
            if (
                self._cached_rigid_vertex_groups is not None
                and len(decoded_fields) >= _NUMPY_RIGID_MIN_VERTICES
            ):
                numpy_module = self._optional_numpy()
                if numpy_module is not None:
                    self._cached_numpy_rigid_vertex_batch = (
                        _build_numpy_rigid_vertex_batch(
                            self._cached_rigid_vertex_groups,
                            numpy_module,
                        )
                    )
        if self._cached_numpy_rigid_vertex_batch is not None:
            assert self._numpy_module is not None
            return _render_numpy_rigid_vertex_batch(
                self._cached_numpy_rigid_vertex_batch,
                matrix_bank,
                output_bytes=len(vertex_payload),
                numpy_module=self._numpy_module,
            )
        if self._cached_rigid_vertex_groups is not None:
            return _render_rigid_vertex_groups(
                self._cached_rigid_vertex_groups,
                matrix_bank,
                output_bytes=len(vertex_payload),
            )
        assert self._cached_vertex_fields is not None
        output = bytearray(len(vertex_payload))
        for index, (
            vertex_x,
            vertex_y,
            vertex_z,
            matrix_indices,
            weights_q0_15,
        ) in enumerate(self._cached_vertex_fields):
            offset = index * AXIS_BEAT_BYTES
            output[offset : offset + SKIN_OUTPUT_BYTES] = (
                _pack_skinned_fields_from_bank(
                    vertex_x,
                    vertex_y,
                    vertex_z,
                    matrix_indices,
                    weights_q0_15,
                    matrix_bank,
                )
            )
        return bytes(output)


def _build_numpy_rigid_vertex_batch(
    groups: tuple[tuple[tuple[int, int, int, int], ...], ...],
    numpy_module: Any,
) -> _NumpyRigidVertexBatch:
    """Cache rigid vertices in packet order for coefficient-wise vectorization."""

    vertex_count = sum(len(vertices) for vertices in groups)
    matrix_indices = numpy_module.empty(vertex_count, dtype=numpy_module.intp)
    vertex_values = numpy_module.empty((vertex_count, 3), dtype=numpy_module.int64)
    for matrix_index, vertices in enumerate(groups):
        if not vertices:
            continue
        output_indices = numpy_module.asarray(
            [offset // AXIS_BEAT_BYTES for offset, _x, _y, _z in vertices],
            dtype=numpy_module.intp,
        )
        matrix_indices[output_indices] = matrix_index
        vertex_values[output_indices] = numpy_module.asarray(
            [(x, y, z) for _offset, x, y, z in vertices],
            dtype=numpy_module.int64,
        ).reshape(-1, 3)
    return _NumpyRigidVertexBatch(matrix_indices, vertex_values)


def _render_numpy_rigid_vertex_batch(
    batch: _NumpyRigidVertexBatch,
    matrix_bank: tuple[AffineMatrixQ1_14, ...],
    *,
    output_bytes: int,
    numpy_module: Any,
) -> bytes:
    """Vectorize the same bounded int64 affine/round/saturate operation as scalar."""

    output = bytearray(output_bytes)
    output_i16 = numpy_module.frombuffer(output, dtype="<i2").reshape(-1, 8)
    matrices = numpy_module.asarray(
        tuple(matrix.values for matrix in matrix_bank),
        dtype=numpy_module.int64,
    ).reshape(-1, 3, 4)
    accumulated = numpy_module.empty_like(batch.vertices)
    vertex_x = batch.vertices[:, 0]
    vertex_y = batch.vertices[:, 1]
    vertex_z = batch.vertices[:, 2]
    for row in range(3):
        row_accumulated = matrices[batch.matrix_indices, row, 0] * vertex_x
        row_accumulated += matrices[batch.matrix_indices, row, 1] * vertex_y
        row_accumulated += matrices[batch.matrix_indices, row, 2] * vertex_z
        row_accumulated += (
            matrices[batch.matrix_indices, row, 3] << AFFINE_TO_TRANSFORMED_SHIFT
        )
        accumulated[:, row] = row_accumulated
    magnitude = numpy_module.abs(accumulated)
    quantized = (magnitude + (1 << (AFFINE_TO_TRANSFORMED_SHIFT - 1))) >> (
        AFFINE_TO_TRANSFORMED_SHIFT
    )
    quantized[accumulated < 0] *= -1
    numpy_module.clip(
        quantized,
        SKIN_OUTPUT_MIN,
        SKIN_OUTPUT_MAX,
        out=quantized,
    )
    output_i16[:, :3] = quantized
    return bytes(output)


def _render_rigid_vertex_groups(
    groups: tuple[tuple[tuple[int, int, int, int], ...], ...],
    matrix_bank: tuple[AffineMatrixQ1_14, ...],
    *,
    output_bytes: int,
) -> bytes:
    """Render a validated all-rigid batch without per-vertex weight dispatch."""

    output = bytearray(output_bytes)
    rounding_bias = 1 << (AFFINE_TO_TRANSFORMED_SHIFT - 1)
    for matrix_index, vertices in enumerate(groups):
        values = matrix_bank[matrix_index].values
        m0, m1, m2, t0 = values[0], values[1], values[2], values[3] << 14
        m4, m5, m6, t1 = values[4], values[5], values[6], values[7] << 14
        m8, m9, m10, t2 = values[8], values[9], values[10], values[11] << 14
        for offset, vertex_x, vertex_y, vertex_z in vertices:
            accumulated_x = m0 * vertex_x + m1 * vertex_y + m2 * vertex_z + t0
            magnitude_x = -accumulated_x if accumulated_x < 0 else accumulated_x
            output_x = (magnitude_x + rounding_bias) >> AFFINE_TO_TRANSFORMED_SHIFT
            if accumulated_x < 0:
                output_x = -output_x
            if output_x < SKIN_OUTPUT_MIN:
                output_x = SKIN_OUTPUT_MIN
            elif output_x > SKIN_OUTPUT_MAX:
                output_x = SKIN_OUTPUT_MAX

            accumulated_y = m4 * vertex_x + m5 * vertex_y + m6 * vertex_z + t1
            magnitude_y = -accumulated_y if accumulated_y < 0 else accumulated_y
            output_y = (magnitude_y + rounding_bias) >> AFFINE_TO_TRANSFORMED_SHIFT
            if accumulated_y < 0:
                output_y = -output_y
            if output_y < SKIN_OUTPUT_MIN:
                output_y = SKIN_OUTPUT_MIN
            elif output_y > SKIN_OUTPUT_MAX:
                output_y = SKIN_OUTPUT_MAX

            accumulated_z = m8 * vertex_x + m9 * vertex_y + m10 * vertex_z + t2
            magnitude_z = -accumulated_z if accumulated_z < 0 else accumulated_z
            output_z = (magnitude_z + rounding_bias) >> AFFINE_TO_TRANSFORMED_SHIFT
            if accumulated_z < 0:
                output_z = -output_z
            if output_z < SKIN_OUTPUT_MIN:
                output_z = SKIN_OUTPUT_MIN
            elif output_z > SKIN_OUTPUT_MAX:
                output_z = SKIN_OUTPUT_MAX

            word = (
                (output_x & 0xFFFF)
                | ((output_y & 0xFFFF) << 16)
                | ((output_z & 0xFFFF) << 32)
            )
            _SKIN_OUTPUT_WORD_STRUCT.pack_into(output, offset, word)
    return bytes(output)


def _signed_field(value: int, width: int) -> int:
    sign_bit = 1 << (width - 1)
    return value - (1 << width) if value & sign_bit else value


def _checked_affine_coefficient(value_q2_28: int) -> int:
    rounded = _round_shift_away_from_zero(value_q2_28, 14)
    if rounded < AFFINE_INPUT_MIN or rounded > AFFINE_INPUT_MAX:
        raise ValueError("composed affine matrix exceeds signed Q1.14 range")
    return rounded


def _round_shift_away_from_zero(value: int, shift: int) -> int:
    magnitude = abs(value)
    rounded = (magnitude + (1 << (shift - 1))) >> shift
    return -rounded if value < 0 else rounded


def _saturate_q1_14(value: int) -> int:
    return min(SKIN_OUTPUT_MAX, max(SKIN_OUTPUT_MIN, value))


def transform_vertex_q2_28(
    vertex: VertexQ1_14,
    matrix: AffineMatrixQ1_14,
) -> tuple[int, int, int]:
    """执行与 ``affine_transform_3d_core`` 相同的 3×4 仿射变换。"""

    m = matrix.values
    return (
        m[0] * vertex.x + m[1] * vertex.y + m[2] * vertex.z + (m[3] << 14),
        m[4] * vertex.x + m[5] * vertex.y + m[6] * vertex.z + (m[7] << 14),
        m[8] * vertex.x + m[9] * vertex.y + m[10] * vertex.z + (m[11] << 14),
    )


def quantize_affine_point_q3_14(point_q2_28: tuple[int, int, int]) -> TransformedPointQ3_14:
    """将仿射结果对称舍入并饱和到蒙皮混合器使用的 Q3.14。"""

    def quantize(value: int) -> int:
        rounded = _round_shift_away_from_zero(value, AFFINE_TO_TRANSFORMED_SHIFT)
        return min(TRANSFORMED_MAX, max(TRANSFORMED_MIN, rounded))

    return TransformedPointQ3_14(*(quantize(value) for value in point_q2_28))


def blend_transformed_points(
    points: tuple[
        TransformedPointQ3_14,
        TransformedPointQ3_14,
        TransformedPointQ3_14,
        TransformedPointQ3_14,
    ],
    weights_q0_15: tuple[int, int, int, int],
) -> SkinnedPointQ1_14:
    """执行与 `skinning_blend_4_core` 相同的乘加、舍入和饱和。"""

    if len(points) != 4 or len(weights_q0_15) != 4:
        raise ValueError("exactly four points and weights are required")
    if any(weight < 0 or weight > SKIN_WEIGHT_SUM for weight in weights_q0_15):
        raise ValueError("Q0.15 skinning weight is out of range")
    if sum(weights_q0_15) != SKIN_WEIGHT_SUM:
        raise ValueError("Q0.15 skinning weights must sum to 32768")

    def blend_axis(axis: str) -> int:
        accumulated = sum(
            getattr(point, axis) * weight
            for point, weight in zip(points, weights_q0_15, strict=True)
        )
        return _saturate_q1_14(_round_shift_away_from_zero(accumulated, SKIN_ROUND_SHIFT))

    return SkinnedPointQ1_14(blend_axis("x"), blend_axis("y"), blend_axis("z"))


def skin_vertex(
    vertex: VertexQ1_14,
    matrices: tuple[
        AffineMatrixQ1_14,
        AffineMatrixQ1_14,
        AffineMatrixQ1_14,
        AffineMatrixQ1_14,
    ],
    weights_q0_15: tuple[int, int, int, int],
) -> SkinnedPointQ1_14:
    """四骨骼线性混合蒙皮的软件位精确参考。"""

    if len(matrices) != 4:
        raise ValueError("exactly four affine matrices are required")
    transformed = tuple(
        quantize_affine_point_q3_14(transform_vertex_q2_28(vertex, matrix))
        for matrix in matrices
    )
    return blend_transformed_points(transformed, weights_q0_15)  # type: ignore[arg-type]
