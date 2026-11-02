# SPDX-License-Identifier: GPL-2.0-only
"""Typed internal Full33 filtered-output boundary; original project implementation.

The external WorldDisplayObservation constructor remains the raw/model boundary.
This function owns changed filtered XYZ and reuses an exact validated source,
confidence objects and normalized snapshot. No native-filter loader is required.
"""
import math
from types import MappingProxyType

from posepriv_coach.world_avatar_preview import WorldDisplayObservation


def filtered_observation(original, world_landmarks):
    """Own new XYZ while reusing an exact validated source/normalized snapshot.

    The native filter copies confidence objects from the immutable source point.
    Requiring that identity both preserves their exact values/types and avoids
    revalidating original presence/visibility ranges. Extra point fields survive.
    """
    if type(original) is not WorldDisplayObservation:
        raise TypeError('internal filtered output requires an exact validated WorldDisplayObservation')
    if (len(world_landmarks) not in (0,33) or
            len(world_landmarks)!=len(original.world_landmarks)):
        raise ValueError('one image and zero or 33 corresponding model landmarks required')
    if not world_landmarks:
        return original
    points=[]
    for point, source_point in zip(world_landmarks,original.world_landmarks):
        if not (math.isfinite(point['x']) and math.isfinite(point['y']) and math.isfinite(point['z'])):
            raise ValueError('finite model coordinates and separate confidences required')
        if (point['presence'] is not source_point['presence'] or
                point['visibility'] is not source_point['visibility']):
            raise ValueError('filtered world must retain exact source confidence fields')
        points.append(MappingProxyType(dict(point)))
    result=object.__new__(WorldDisplayObservation)
    object.__setattr__(result,'source',original.source)
    object.__setattr__(result,'world_landmarks',tuple(points))
    object.__setattr__(result,'normalized_landmarks',original.normalized_landmarks)
    return result

