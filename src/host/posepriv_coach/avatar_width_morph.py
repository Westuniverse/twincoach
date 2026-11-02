"""Explicit fixed-bind display widths; project GPL-2.0-only, ADR0204.

No personal profile, image/model inference, source download or device access.
Model source/license metadata remain unchanged (including existing CC0 assets).
Widths are visual shape controls, not physical limb lengths or quality scores.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace

from .mesh_asset import AvatarMeshAsset, quantize_mesh_position

WIDTH_PARAMETERS = (
    "head_width", "neck_width", "shoulder_width", "waist_width", "arm_width", "leg_width",
)
_COMMON_BONES = {"spine", "neck", "head"} | {
    side + "_" + part for side in ("left", "right")
    for part in ("upper_arm", "lower_arm", "upper_leg", "lower_leg", "hand")
}


def validate_width_parameters(parameters: Mapping[str, float]) -> dict[str, float]:
    """Require all six explicit finite builtin numbers in [.7, 1.3]."""
    if not isinstance(parameters, Mapping) or set(parameters) != set(WIDTH_PARAMETERS):
        raise ValueError("explicit complete six width parameters required")
    result = dict(parameters)
    if any(type(value) not in (int, float) or not math.isfinite(value)
           or not .7 <= value <= 1.3 for value in result.values()):
        raise ValueError("width parameters must be finite builtin numbers in [.7, 1.3]")
    return result


def _cross_for_triangle(vertices, triangle):
    a, b, c = (vertices[index].position for index in triangle)
    x, y, z = b.x-a.x, b.y-a.y, b.z-a.z
    u, v, w = c.x-a.x, c.y-a.y, c.z-a.z
    return y*w-z*v, z*u-x*w, x*v-y*u


def morph_avatar_widths(asset: AvatarMeshAsset, parameters: Mapping[str, float]) -> AvatarMeshAsset:
    """Return a new asset with only rest vertex positions changed.

    All-one parameters return the original object, retaining caches and exact
    integer data. Nonidentity changes use existing nearest/half-away Q1.14
    quantization, reject overflow and newly collapsed triangles, and preserve
    every skeleton, vertex weight/index, topology, color and texture field.
    Head/neck change model X only. Limbs change their frontal-plane radial width
    with zero deformation at original endpoints. This is not a collision solver.
    """
    parameters = validate_width_parameters(parameters)
    if (not isinstance(asset, AvatarMeshAsset) or len(asset.bone_names) != 13
            or set(asset.bone_names) != _COMMON_BONES
            or len(asset.bone_bind_lengths_q1_14) != 13
            or len(asset.inverse_bind_matrices_q1_14) != 13):
        raise ValueError("width morph requires complete common-bind thirteen-bone asset")
    if all(value == 1 for value in parameters.values()):
        return asset

    import numpy as np

    # Reuse the exact project bind inversion/matmul used to create the reviewed
    # private candidate; do not involve planner/rig history or any pose source.
    from .preview_rigid_avatar import _inverse_bind, _matmul

    try:
        binds = []
        for matrix in asset.inverse_bind_matrices_q1_14:
            binds.append(_inverse_bind(matrix))
    except (ValueError, TypeError, StopIteration, ZeroDivisionError) as exc:
        raise ValueError("nonsingular common-bind inverse matrices required") from exc
    lengths = np.array(asset.bone_bind_lengths_q1_14, dtype=float)/16384
    starts = {name: binds[slot][:3, 3] for slot, name in enumerate(asset.bone_names)}
    ends = {name: _matmul(binds[slot], np.array([0., lengths[slot], 0., 1.]))[:3]
            for slot, name in enumerate(asset.bone_names)}
    origin = starts["spine"]
    torso_height = float(starts["head"][1]-origin[1])
    if not math.isfinite(torso_height) or torso_height <= 0:
        raise ValueError("common bind must have head above spine origin")
    positions = np.array([(v.position.x, v.position.y, v.position.z)
                          for v in asset.vertices], dtype=np.float64)/16384
    weights = np.zeros((len(positions), len(asset.bone_names)))
    for row, vertex in enumerate(asset.vertices):
        for slot, weight in zip(vertex.matrix_indices, vertex.weights_q0_15):
            weights[row, slot] += weight/32768
    result = positions.copy()
    for slot, name in enumerate(asset.bone_names):
        delta = positions-starts[name]
        displacement = np.zeros_like(positions)
        if name == "spine":
            t = np.clip((positions[:, 1]-origin[1])/torso_height, 0, 1)
            scale = (parameters["waist_width"]
                     + (parameters["shoulder_width"]-parameters["waist_width"])*t)
            displacement[:, 0] = (positions[:, 0]-origin[0])*(scale-1)
        elif name in ("head", "neck"):
            if name == "head":
                t = np.clip(delta[:, 1]/(lengths[slot]*.15), 0, 1)
                taper = t*t*(3-2*t)
            else:
                taper = np.ones(len(positions))
            displacement[:, 0] = delta[:, 0]*(parameters[name+"_width"]-1)*taper
        elif name.endswith(("_upper_arm", "_lower_arm", "_upper_leg", "_lower_leg")):
            axis = ends[name]-starts[name]
            magnitude = float(np.linalg.norm(axis))
            if not math.isfinite(magnitude) or magnitude <= 0:
                raise ValueError("nonzero finite common-bind limb axis required")
            axis = axis/magnitude
            normal = np.array([-axis[1], axis[0], 0.])
            magnitude = float(np.linalg.norm(normal))
            if magnitude <= 1e-12:
                raise ValueError("common-bind limb must project into frontal XY plane")
            normal /= magnitude
            t = np.clip((delta@axis)/lengths[slot], 0, 1)
            taper = 4*t*(1-t)
            scale = parameters["arm_width" if "arm" in name else "leg_width"]
            displacement = (delta@normal)[:, None]*normal*(scale-1)*taper[:, None]
        else:
            continue
        result += weights[:, slot, None]*displacement
    vertices = tuple(replace(vertex, position=quantize_mesh_position(tuple(map(float, point)), 1.))
                     for vertex, point in zip(asset.vertices, result))
    for triangle in asset.triangles:
        if (not any(_cross_for_triangle(vertices, triangle))
                and any(_cross_for_triangle(asset.vertices, triangle))):
            raise ValueError("width morph introduced a zero-area quantized triangle")
    return replace(asset, vertices=vertices)
