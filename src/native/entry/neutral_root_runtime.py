# SPDX-License-Identifier: GPL-2.0-only
"""Recorded-only v4 neutral knee support, after final head/recovery preparation."""
from dataclasses import replace
import json
import math
from pathlib import Path

import numpy as np

LEG_NAMES = tuple(side + '_' + name for side in ('left', 'right') for name in ('hip', 'knee', 'ankle'))
LOWER_INDICES = (23, 24, 25, 26, 27, 28)


def display_legs(joints, display_transform):
    """Same individual D@point operations used by the existing quality recorder."""
    D = np.asarray(display_transform).reshape(4, 4)
    return {name: dict(display_position=(D @ np.asarray((*joints[name].position, 1.)))[:3].tolist(),
                       valid=joints[name].valid) for name in LEG_NAMES}


def knee_angle(joints, side):
    a, b, c = (joints[side + '_' + n]['display_position'] for n in ('hip', 'knee', 'ankle'))
    u = [x - y for x, y in zip(a, b)]
    v = [x - y for x, y in zip(c, b)]
    cosine = sum(x * y for x, y in zip(u, v)) / math.sqrt(sum(x * x for x in u) * sum(x * x for x in v))
    return math.degrees(math.acos(max(-1., min(1., cosine))))


def neutral_delta(profile, reason, geometry, normalized_count, lower_q, joints):
    """Exact v4 relative_neutral_knee_support arithmetic, no temporal state."""
    if profile is None:
        return 0., False, 'unreviewed_capture'
    if reason not in ('partial', 'tracked'):
        return 0., False, 'no_current_render'
    if geometry != ('side', 360, 640, 270):
        return 0., False, 'different_camera_geometry'
    if normalized_count != 33:
        return 0., False, 'no_current_feet'
    angles = [knee_angle(joints, side) for side in ('left', 'right')]
    bends = [profile['neutral_knee_angles'][side] - angle
             for side, angle in zip(('left', 'right'), angles)]
    bend = min(bends)
    t = max(0., min(1., (bend - 15.) / 20.))
    weight = t * t * (3. - 2. * t)
    if weight == 0.:
        return 0., False, 'standing_no_root_correction'
    if lower_q < .5:
        return 0., False, 'untrusted_bilateral_support'
    if not all(joints[side + '_ankle']['valid'] for side in ('left', 'right')):
        return 0., False, 'no_final_ankles'
    current = min(joints[side + '_ankle']['display_position'][1] for side in ('left', 'right'))
    return weight * (profile['minimum_ankle_y'] - current), True, 'reviewed_bilateral_support'


def prepare_delta(prepared, work, profile):
    """Read six final joints and six original world qualities, never a record JSON."""
    rig, plan = prepared.get('rig'), prepared.get('plan')
    observation = work.get('world_observations', {}).get('side')
    if rig is None or plan is None or observation is None:
        return 0., False, 'missing_current_context'
    if not all(name in plan.joints for name in LEG_NAMES):
        return 0., False, 'missing_current_context'
    source = observation.source
    geometry = (source.view, source.width, source.height, source.rotation_degrees)
    count = len(observation.normalized_landmarks)
    if count != 33:
        return neutral_delta(profile, rig.reason, geometry, count, 0., {})
    q = min(min(observation.world_landmarks[i]['presence'], observation.world_landmarks[i]['visibility'])
            for i in LOWER_INDICES)
    return neutral_delta(profile, rig.reason, geometry, count, q,
                         display_legs(plan.joints, rig.display_transform))


def install_neutral_root_reference(owner, profiles, recording_id, *, recorded_input=False):
    """Explicit cold scope; unknown/live/missing context leaves prepare unwrapped."""
    receipt = dict(enabled=False, recording_id=recording_id, algorithm='v4_relative_neutral_knee_support')
    if recorded_input is not True:
        return dict(receipt, reason='not_explicit_recorded_input')
    if profiles is None:
        return dict(receipt, reason='missing_reviewed_profile')
    if isinstance(profiles, (str, Path)):
        profiles = json.loads(Path(profiles).read_text())
    profile = profiles.get(recording_id)
    if profile is None:
        return dict(receipt, reason='recording_outside_reviewed_reference_scope')
    if (profile.get('capture_id') != recording_id or
            profile.get('contact_mode') != 'relative_neutral_knee_support' or
            'minimum_ankle_y' not in profile or 'neutral_knee_angles' not in profile):
        return dict(receipt, reason='missing_reviewed_profile')
    if (getattr(owner, 'view', None), profile.get('view'), profile.get('width'),
            profile.get('height'), profile.get('rotation_degrees')) != ('side', 'side', 360, 640, 270):
        return dict(receipt, reason='physical_avatar_geometry_outside_reference_scope')
    original = owner.prepare

    def prepare(work):
        prepared = original(work)
        delta, active, _ = prepare_delta(prepared, work, profile)
        if active and delta != 0.:
            rig = prepared['rig']
            D = list(rig.display_transform)
            D[7] += delta
            prepared['rig'] = replace(rig, display_transform=tuple(D))
        return prepared

    owner.prepare = prepare
    return dict(receipt, enabled=True, reason='explicit_reviewed_recording', geometry=['side', 360, 640, 270])
