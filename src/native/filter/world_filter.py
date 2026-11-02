# SPDX-License-Identifier: GPL-2.0-only
"""One constructor injection; original owner states and output constructor stay."""
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(library=None):
    path = Path(library) if library else HERE/'libposepriv_world_filter.so'
    spec = importlib.util.spec_from_file_location('_posepriv_world_filter', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def constructor(base_class, library=None):
    native = load(library)
    from posepriv_coach.world_avatar_preview import WorldDisplayObservation

    class NativeWorldPointStabilizer(base_class):
        def apply(self, observation):
            try:
                result = native.filter(self, observation)
            except native.UnsupportedInput:
                # Native commits only after the complete accepted observation.
                # Original fallback uses the same canonical owner/history.
                return super().apply(observation)
            if result is None:
                return observation
            return WorldDisplayObservation(observation.source, result,
                                           observation.normalized_landmarks)

    return NativeWorldPointStabilizer


def install(preview_runtime, library=None):
    """Call once in the geometry child before its PreviewGeometryBuilder exists."""
    original = preview_runtime.WorldPointStabilizer
    preview_runtime.WorldPointStabilizer = constructor(original, library)
    return original
