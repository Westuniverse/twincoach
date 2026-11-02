# SPDX-License-Identifier: GPL-2.0-only
"""Explicit native initial-plan builder for one fixed WorldAvatar owner."""
import importlib.util
from pathlib import Path

from .avatar_image_source import ImageAvatarFramePlan
from .avatar_skinning import AffineMatrixQ1_14, MatrixWrite
from .skeleton_pose import SkeletonJoint3D, BoneSegment3D
from .world_avatar_preview import WorldDisplayObservation


class NativeWorldPlan:
    """Materialize a plan without changing rig/head/body/filter state.

    Bind after selecting the owner's rig, framing and immutable requests, then
    assign to ``owner.initial_plan_builder``. Rebuild this builder if those
    geometry parameters change. Resetting temporal state keeps the builder.
    The library must be compiled for the running CPython and NumPy.
    """

    def __init__(self, owner, library_path):
        self.library_path = Path(library_path).resolve(strict=True)
        spec = importlib.util.spec_from_file_location('_posepriv_world_prepare', self.library_path)
        self._module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self._module)
        rig = owner.rig
        self._context = self._module.create(
            SkeletonJoint3D, BoneSegment3D, ImageAvatarFramePlan,
            WorldDisplayObservation, rig.links, rig.indices, rig.origin,
            rig.torso_length,
            tuple(MatrixWrite(i, AffineMatrixQ1_14.identity()) for i in range(16)),
            owner.requests, owner.framing,
        )

    def __call__(self, observation):
        return self._module.prepare(self._context, observation)
