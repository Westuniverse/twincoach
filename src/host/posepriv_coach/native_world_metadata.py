# SPDX-License-Identifier: GPL-2.0-only
"""Explicit batch builder for validated frozen WorldAvatar output metadata.

Original project implementation of the existing final confidence=0 copy loop.
Only display joints/segments are accepted. Raw observations and rig/filter state
remain with their original owners. Construction binds known CPython slot layouts
once; it never patches a module, changes a default or loads an FPGA device.
"""
import importlib.util
from pathlib import Path


class NativeWorldMetadata:
    """Callable ``(joints, segments) -> (dict, tuple)`` display-only builder."""
    def __init__(self,library_path):
        from .skeleton_pose import SkeletonJoint3D,BoneSegment3D
        self.library_path=Path(library_path).resolve(strict=True)
        spec=importlib.util.spec_from_file_location('_posepriv_world_metadata',self.library_path)
        self._module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self._module)
        self._module.setup(SkeletonJoint3D,BoneSegment3D)
        self.batch=self._module.batch

    def __call__(self,joints,segments):
        return self.batch(joints,segments)


def make_world_metadata_builder(library_path):
    """Cold-bind one native callable; callers retain the original plan boundary."""
    return NativeWorldMetadata(library_path).batch
