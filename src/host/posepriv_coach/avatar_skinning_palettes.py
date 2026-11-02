"""Global-bone skinning through complete local 16-slot packets (ABI 0.1).

Matrices supplied at execution already include inverse bind. This module does
not estimate a pose or change the existing packet/mesh contracts.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import struct

from .avatar_skinning import (
    AXIS_BEAT_BYTES, MATRIX_COUNT, MATRIX_PACKET_BYTES, SKIN_WEIGHT_SUM,
    AffineMatrixQ1_14, MatrixWrite, PackedSkinningOutputBatch,
    VertexQ1_14, VertexSkinningRequest, pack_matrix_packet_record,
    pack_vertex_skinning_request, SkinnedPointQ1_14,
    transform_vertex_q2_28, quantize_affine_point_q3_14,
    _round_shift_away_from_zero, _saturate_q1_14,
)
from .shared_skinning_batches import RX_CAPACITY, skinning_validation_functions


@dataclass(frozen=True, slots=True)
class GlobalSkinningVertex:
    vertex: VertexQ1_14
    bone_indices: tuple[int, int, int, int]
    weights_q0_15: tuple[int, int, int, int]

    def __post_init__(self):
        indices, weights = tuple(self.bone_indices), tuple(self.weights_q0_15)
        if not isinstance(self.vertex, VertexQ1_14):
            raise TypeError('fixed-point vertex required')
        if len(indices) != 4 or any(type(i) is not int or i < 0 for i in indices):
            raise ValueError('four nonnegative global bone indices required')
        if (len(weights) != 4 or any(type(w) is not int or not 0 <= w <= SKIN_WEIGHT_SUM
                                    for w in weights) or sum(weights) != SKIN_WEIGHT_SUM):
            raise ValueError('four integer weights must sum to 32768')
        object.__setattr__(self, 'bone_indices', indices)
        object.__setattr__(self, 'weights_q0_15', weights)


@dataclass(frozen=True, slots=True)
class SkinningPaletteBatch:
    bone_indices: tuple[int, ...]
    source_indices: tuple[int, ...]
    vertex_payload: bytes
    # (destination byte offset, source byte offset, byte count)
    scatter_runs: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True, slots=True, init=False)
class SkinningPalettePlan:
    bone_count: int
    vertex_count: int
    batches: tuple[SkinningPaletteBatch, ...]

    def __init__(self, vertices: Iterable[GlobalSkinningVertex], *, bone_count: int,
                 max_vertices: int = RX_CAPACITY // AXIS_BEAT_BYTES):
        if type(bone_count) is not int or bone_count < 1:
            raise ValueError('positive global bone count required')
        if type(max_vertices) is not int or not 1 <= max_vertices <= RX_CAPACITY // AXIS_BEAT_BYTES:
            raise ValueError('batch capacity must be within 1..8192 vertices')
        vertices = tuple(vertices)
        if not vertices or any(not isinstance(v, GlobalSkinningVertex) for v in vertices):
            raise ValueError('nonempty global skinning vertices required')
        if any(i >= bone_count for v in vertices for i in v.bone_indices):
            raise ValueError('global bone index exceeds matrix count')
        supports = [frozenset(i for i, w in zip(v.bone_indices, v.weights_q0_15) if w)
                    for v in vertices]
        order = sorted(range(len(vertices)), key=lambda i: (-len(supports[i]), i))
        groups: list[tuple[set[int], list[int]]] = []
        for i in order:
            choices = []
            support = supports[i]
            for index, (bones, members) in enumerate(groups):
                added = len(support - bones)
                if len(members) < max_vertices and len(bones) + added <= MATRIX_COUNT:
                    choices.append((added, -(len(bones) + added), index))
            if choices:
                index = min(choices)[2]
            else:
                index = len(groups)
                groups.append((set(), []))
            groups[index][0].update(support)
            groups[index][1].append(i)
        batches = []
        for bones, members in groups:
            palette = tuple(sorted(bones))
            local = {bone: i for i, bone in enumerate(palette)}
            requests = []
            runs = []
            for offset, source in enumerate(members):
                v = vertices[source]
                indices = tuple(local[i] if w else 0 for i, w in
                                zip(v.bone_indices, v.weights_q0_15))
                request = VertexSkinningRequest(v.vertex, indices, v.weights_q0_15)
                requests.append(pack_vertex_skinning_request(request).data)
                if runs and runs[-1][0] + runs[-1][2] == source * AXIS_BEAT_BYTES:
                    dst, src, size = runs[-1]
                    runs[-1] = (dst, src, size + AXIS_BEAT_BYTES)
                else:
                    runs.append((source * AXIS_BEAT_BYTES, offset * AXIS_BEAT_BYTES, AXIS_BEAT_BYTES))
            batches.append(SkinningPaletteBatch(palette, tuple(members), b''.join(requests), tuple(runs)))
        object.__setattr__(self, 'bone_count', bone_count)
        object.__setattr__(self, 'vertex_count', len(vertices))
        object.__setattr__(self, 'batches', tuple(batches))

    @property
    def matrix_header_bytes(self) -> int:
        return len(self.batches) * MATRIX_COUNT * MATRIX_PACKET_BYTES

    def execute(self, matrices: Iterable[AffineMatrixQ1_14], *,
                transfer: Callable[[bytes], bytes], validator=None, native_ops=None) -> PackedSkinningOutputBatch:
        """Borrow a packet transfer; publish only the complete original order.

        The caller retains the transfer/device's existing fault lifecycle. There
        is no retry, device opening, fallback, pose estimation or scoring here.
        """
        matrices = tuple(matrices)
        if len(matrices) != self.bone_count or any(
                not isinstance(m, AffineMatrixQ1_14) for m in matrices):
            raise ValueError('one affine matrix per global bone required')
        if not callable(transfer):
            raise TypeError('packet transfer required')
        if native_ops is not None: native_ops.check_palette(self)
        _, validate_output = skinning_validation_functions(validator)
        identity = AffineMatrixQ1_14.identity()
        headers = []
        for batch in self.batches:
            bank = tuple(matrices[i] for i in batch.bone_indices)
            bank += (identity,) * (MATRIX_COUNT - len(bank))
            headers.append(b''.join(pack_matrix_packet_record(MatrixWrite(i, m))
                                    for i, m in enumerate(bank)))
        result = bytearray(self.vertex_count * AXIS_BEAT_BYTES)
        destination = memoryview(result)
        for index, (batch, header) in enumerate(zip(self.batches, headers)):
            output = transfer(header + batch.vertex_payload)
            validate_output(output, len(batch.source_indices))
            if native_ops is not None:
                native_ops.scatter(index, output, result)
            else:
                source = memoryview(output)
                for dst, src, size in batch.scatter_runs:
                    destination[dst:dst + size] = source[src:src + size]
        return PackedSkinningOutputBatch(bytes(result))


@dataclass(frozen=True, slots=True)
class WeightedSkinningVertex:
    """All authored influences, after explicit Q0.15 quantization."""
    vertex: VertexQ1_14
    bone_indices: tuple[int, ...]
    weights_q0_15: tuple[int, ...]

    def __post_init__(self):
        indices, weights = tuple(self.bone_indices), tuple(self.weights_q0_15)
        if not isinstance(self.vertex, VertexQ1_14):
            raise TypeError('fixed-point vertex required')
        if not indices or any(type(i) is not int or i < 0 for i in indices):
            raise ValueError('nonnegative global bone indices required')
        if (len(weights) != len(indices) or
                any(type(w) is not int or not 0 <= w <= SKIN_WEIGHT_SUM for w in weights) or
                sum(weights) != SKIN_WEIGHT_SUM):
            raise ValueError('corresponding integer weights must sum to 32768')
        object.__setattr__(self, 'bone_indices', indices)
        object.__setattr__(self, 'weights_q0_15', weights)


def reference_weighted_vertex(vertex: WeightedSkinningVertex,
                              matrices: tuple[AffineMatrixQ1_14, ...]) -> SkinnedPointQ1_14:
    """Unrestricted scalar golden: 18-bit transforms, one final weighted round."""
    total = [0, 0, 0]
    for bone, weight in zip(vertex.bone_indices, vertex.weights_q0_15):
        if not weight: continue
        point = quantize_affine_point_q3_14(transform_vertex_q2_28(vertex.vertex, matrices[bone]))
        for axis, value in enumerate((point.x, point.y, point.z)):
            total[axis] += value * weight
    return SkinnedPointQ1_14(*(_saturate_q1_14(_round_shift_away_from_zero(v, 15)) for v in total))


@dataclass(frozen=True, slots=True, init=False)
class WeightedSkinningPlan:
    """Keep arbitrary influence counts using exact bounded rigid sub-results.

    Compact vertices still use the four-influence PL path. Only wider vertices
    use rigid PL transforms plus PS integer blending. If a wide single-bone
    result could overflow Q1.14, fail before I/O instead of accepting an early
    saturation. Pose/frame source semantics remain the caller's responsibility.
    """
    palettes: SkinningPalettePlan
    vertex_count: int
    compact: tuple[tuple[int, int], ...]
    wide: tuple[tuple[int, tuple[tuple[int, int], ...]], ...]
    bounds: tuple[tuple[int, tuple[int, ...], tuple[int, ...]], ...]

    def __init__(self, vertices: Iterable[WeightedSkinningVertex], *, bone_count: int,
                 max_vertices: int = RX_CAPACITY // AXIS_BEAT_BYTES):
        vertices = tuple(vertices)
        if not vertices or any(not isinstance(v, WeightedSkinningVertex) for v in vertices):
            raise ValueError('nonempty weighted vertices required')
        if (type(bone_count) is not int or bone_count < 1 or
                any(i >= bone_count for v in vertices for i in v.bone_indices)):
            raise ValueError('global bone index exceeds positive matrix count')
        wire, compact, wide, bounds = [], [], [], {}
        for source, vertex in enumerate(vertices):
            weights = {}
            for bone, weight in zip(vertex.bone_indices, vertex.weights_q0_15):
                if weight: weights[bone] = weights.get(bone, 0) + weight
            influences = tuple(sorted(weights.items()))
            if len(influences) <= 4:
                bones = tuple(i for i, _ in influences)
                values = tuple(w for _, w in influences)
                compact.append((source, len(wire)))
                wire.append(GlobalSkinningVertex(vertex.vertex, bones + (0,) * (4 - len(bones)),
                                                  values + (0,) * (4 - len(values))))
            else:
                contributions = []
                point = (vertex.vertex.x, vertex.vertex.y, vertex.vertex.z)
                for bone, weight in influences:
                    contributions.append((len(wire), weight))
                    wire.append(GlobalSkinningVertex(vertex.vertex, (bone, 0, 0, 0),
                                                     (SKIN_WEIGHT_SUM, 0, 0, 0)))
                    if bone not in bounds: bounds[bone] = (list(point), list(point))
                    lo, hi = bounds[bone]
                    for axis in range(3):
                        lo[axis] = min(lo[axis], point[axis]); hi[axis] = max(hi[axis], point[axis])
                wide.append((source, tuple(contributions)))
        palettes = SkinningPalettePlan(wire, bone_count=bone_count, max_vertices=max_vertices)
        object.__setattr__(self, 'palettes', palettes)
        object.__setattr__(self, 'vertex_count', len(vertices))
        object.__setattr__(self, 'compact', tuple(compact))
        object.__setattr__(self, 'wide', tuple(wide))
        object.__setattr__(self, 'bounds', tuple((bone, tuple(lo), tuple(hi))
                          for bone, (lo, hi) in sorted(bounds.items())))

    def execute(self, matrices: Iterable[AffineMatrixQ1_14], *,
                transfer: Callable[[bytes], bytes], validator=None, native_ops=None) -> PackedSkinningOutputBatch:
        matrices = tuple(matrices)
        if len(matrices) != self.palettes.bone_count or any(
                not isinstance(m, AffineMatrixQ1_14) for m in matrices):
            raise ValueError('one affine matrix per global bone required')
        if native_ops is not None: native_ops.check_weighted(self)
        for bone, lo, hi in self.bounds:
            m = matrices[bone].values
            for row in range(3):
                offset = row * 4
                lower = upper = m[offset + 3] << 14
                for axis in range(3):
                    a, b = m[offset + axis] * lo[axis], m[offset + axis] * hi[axis]
                    lower += min(a, b); upper += max(a, b)
                if (_round_shift_away_from_zero(lower, 14) < -32768 or
                        _round_shift_away_from_zero(upper, 14) > 32767):
                    raise ValueError('wide-influence transform may exceed exact Q1.14 intermediate range')
        wire = self.palettes.execute(matrices, transfer=transfer, validator=validator,
                                     native_ops=native_ops).padded_data
        if native_ops is not None: return native_ops.blend(wire)
        result = bytearray(self.vertex_count * AXIS_BEAT_BYTES)
        for source, index in self.compact:
            result[source * 16:source * 16 + 16] = wire[index * 16:index * 16 + 16]
        for source, contributions in self.wide:
            x = y = z = 0
            for index, weight in contributions:
                a, b, c = struct.unpack_from('<hhh', wire, index * 16)
                x += a * weight; y += b * weight; z += c * weight
            struct.pack_into('<hhh', result, source * 16,
                *(_saturate_q1_14(_round_shift_away_from_zero(v, 15)) for v in (x, y, z)))
        return PackedSkinningOutputBatch(bytes(result))
