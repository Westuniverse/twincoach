"""Exact declared matrix equivalence for position skinning; no pose estimation."""
from dataclasses import dataclass

from .avatar_skinning import AffineMatrixQ1_14
from .avatar_skinning_palettes import WeightedSkinningVertex


@dataclass(frozen=True, slots=True)
class SkinningMatrixAliases:
    groups: tuple[int, ...]
    representatives: tuple[int, ...]

    def __post_init__(self):
        groups, representatives = tuple(self.groups), tuple(self.representatives)
        if (not groups or not representatives or
                any(type(g) is not int or not 0 <= g < len(representatives) for g in groups) or
                set(groups) != set(range(len(representatives))) or
                any(type(i) is not int or not 0 <= i < len(groups) or groups[i] != g
                    for g, i in enumerate(representatives))):
            raise ValueError('dense alias groups and one corresponding source representative required')
        object.__setattr__(self, 'groups', groups)
        object.__setattr__(self, 'representatives', representatives)

    def compress(self, matrices):
        """Validate every full matrix before the caller performs any frame I/O."""
        matrices = tuple(matrices)
        if len(matrices) != len(self.groups) or any(not isinstance(m, AffineMatrixQ1_14) or
                any(type(v) is not int for v in m.values) for m in matrices):
            raise ValueError('complete original matrix bank required')
        selected = tuple(matrices[i] for i in self.representatives)
        if any(m != selected[g] for m, g in zip(matrices, self.groups)):
            raise ValueError('declared matrix aliases differ in this frame')
        return selected

    def remap_vertices(self, vertices):
        result = []
        for vertex in vertices:
            if not isinstance(vertex, WeightedSkinningVertex) or any(i >= len(self.groups) for i in vertex.bone_indices):
                raise ValueError('weighted vertices must refer to the original alias bank')
            result.append(WeightedSkinningVertex(vertex.vertex,
                tuple(self.groups[i] for i in vertex.bone_indices), vertex.weights_q0_15))
        return tuple(result)
