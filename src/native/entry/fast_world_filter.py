# SPDX-License-Identifier: GPL-2.0-only
from world_filter import load
from posepriv_coach.world_display_boundaries import filtered_observation

def install(preview_runtime,library):
    native=load(library)
    reference=preview_runtime.WorldPointStabilizer
    class NativeTypedWorldFilter(reference):
        def apply(self,observation):
            try:points=native.filter(self,observation)
            except native.UnsupportedInput:return super().apply(observation)
            return observation if points is None else filtered_observation(observation,points)
    preview_runtime.WorldPointStabilizer=NativeTypedWorldFilter
    return reference
