"""Explicit optional CPU C ABI 1; bind static gather/blend metadata once."""
from __future__ import annotations
import ctypes as c
from pathlib import Path

from .avatar_skinning import PackedSkinningOutputBatch
from .avatar_skinning_palettes import SkinningPalettePlan, WeightedSkinningPlan


class NativeSkinningPalettes:
    def __init__(self, plan: SkinningPalettePlan | WeightedSkinningPlan, library):
        if not isinstance(plan, (SkinningPalettePlan, WeightedSkinningPlan)):
            raise TypeError('palette or weighted plan required')
        self.plan = plan
        self.palette = plan.palettes if isinstance(plan, WeightedSkinningPlan) else plan
        self.library = c.CDLL(str(Path(library).resolve(strict=True)))
        version = self.library.posepriv_palette_ops_abi
        version.argtypes = []; version.restype = c.c_uint32
        if version() != 1: raise RuntimeError('native palette ABI differs')
        P, U, S = c.c_void_p, c.c_uint32, c.c_size_t
        self._scatter = self.library.posepriv_palette_scatter_v1
        self._scatter.argtypes = [P, S, U, P, S, P, U]; self._scatter.restype = c.c_int
        self._blend = self.library.posepriv_palette_blend_v1
        self._blend.argtypes = [P, S, U, P, S, U, P, P, P, U]; self._blend.restype = c.c_int
        self._batches = tuple((c.c_uint32 * len(b.source_indices))(*b.source_indices)
                              for b in self.palette.batches)
        if isinstance(plan, WeightedSkinningPlan):
            rows = [None] * plan.vertex_count
            for source, index in plan.compact: rows[source] = ((index, 32768),)
            for source, contributions in plan.wide: rows[source] = contributions
            offsets, indices, weights = [0], [], []
            for row in rows:
                for index, weight in row:
                    indices.append(index); weights.append(weight)
                offsets.append(len(indices))
            self._offsets = (c.c_uint32 * len(offsets))(*offsets)
            self._indices = (c.c_uint32 * len(indices))(*indices)
            self._weights = (c.c_uint16 * len(weights))(*weights)
            self._contributions = len(indices)

    def check_palette(self, plan):
        if plan is not self.palette: raise ValueError('native metadata belongs to another palette')

    def check_weighted(self, plan):
        if plan is not self.plan or not isinstance(plan, WeightedSkinningPlan):
            raise ValueError('native metadata belongs to another weighted plan')

    def scatter(self, batch_index, output, destination):
        indices = self._batches[batch_index]
        dst = (c.c_ubyte * len(destination)).from_buffer(destination)
        if not self._scatter(dst, len(destination), self.palette.vertex_count,
                c.c_char_p(output), len(output), indices, len(indices)):
            raise ValueError('native palette scatter failed')

    def blend(self, wire):
        self.check_weighted(self.plan)
        destination = bytearray(self.plan.vertex_count * 16)
        dst = (c.c_ubyte * len(destination)).from_buffer(destination)
        if not self._blend(dst, len(destination), self.plan.vertex_count,
                c.c_char_p(wire), len(wire), self.palette.vertex_count,
                self._offsets, self._indices, self._weights, self._contributions):
            raise ValueError('native weighted blend failed')
        return PackedSkinningOutputBatch(bytes(destination))
