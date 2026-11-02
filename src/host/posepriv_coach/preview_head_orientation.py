"""Explicit current-image head orientation candidates for display, never scoring."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from .avatar_skinning import MatrixWrite
from .preview_rigid_avatar import _cross, _dot, _inverse_bind, _matmul, _norm
from .skeleton_pose import BoneSegment3D, bone_affine_matrix_q1_14, build_bone_segments
from .avatar_image_source import avatar_image_source


def _unit(value):
    import numpy as np
    value = np.asarray(value, dtype=float)
    length = _norm(value)
    if not math.isfinite(length) or length < 1e-8:
        raise ValueError('degenerate head orientation')
    return value / length


def _rotation(value):
    """Accept a proper uniform-scale rotation, remove scale and tiny roundoff."""
    import numpy as np
    value = np.asarray(value, dtype=float).reshape(3, 3)
    if not np.isfinite(value).all():
        raise ValueError('nonfinite head rotation')
    lengths = [_norm(value[:, k]) for k in range(3)]
    if min(lengths) < 1e-8 or max(lengths) - min(lengths) > .002 * max(lengths):
        raise ValueError('head rotation must have uniform positive scale')
    x, y, z = (_unit(value[:, k]) for k in range(3))
    if max(abs(_dot(x, y)), abs(_dot(x, z)), abs(_dot(y, z))) > .002:
        raise ValueError('head rotation must be orthogonal')
    y = _unit(y - _dot(x, y) * x)
    back = _cross(x, y)
    if _dot(back, z) < .99:
        raise ValueError('reflected head rotation')
    return np.column_stack((x, y, back))


def face_transform_rotation(matrix):
    """MediaPipe virtual camera -Z -> avatar camera +Z; ignore translation/scale.

    This is an orientation convention conversion, not physical calibration.
    The model's canonical face faces +Z, whereas the avatar faces -Z.
    """
    import numpy as np
    value = np.asarray(matrix, dtype=float)
    if value.size != 16 or not np.isfinite(value).all():
        raise ValueError('finite 4x4 face transform required')
    value = value.reshape(4, 4)
    if not np.allclose(value[3], (0., 0., 0., 1.), atol=1e-6, rtol=0.):
        raise ValueError('affine face transform required')
    sign = np.diag((1., 1., -1.))
    return tuple(_matmul(_matmul(sign, _rotation(value[:3, :3])), sign).flat)


def _current_world_points(landmarks, indices):
    import numpy as np
    if len(landmarks) != 33:
        raise ValueError('thirty-three world landmarks required')
    values = []
    for index in indices:
        point = landmarks[index]
        if (not all(math.isfinite(point[key]) for key in
                    ('x', 'y', 'z', 'presence', 'visibility')) or
                not 0 <= point['visibility'] <= 1 or not .5 <= point['presence'] <= 1):
            raise ValueError('current world face point unavailable')
        values.append(np.array((point['x'], -point['y'], point['z'])))
    return values


def world_face_plane_rotation(landmarks):
    """Learned eye/mouth plane, including explicitly inferred occlusion.

    Presence admits a current predicted point. Visibility is deliberately not
    promoted to observation confidence; all use of this adapter is inferred.
    """
    import numpy as np
    left, right, mouth_left, mouth_right = _current_world_points(landmarks, (2, 5, 9, 10))
    x = _unit(left - right)
    up = (left + right - mouth_left - mouth_right) / 2
    y = _unit(up - _dot(up, x) * x)
    return tuple(np.column_stack((x, y, _cross(x, y))).flat)


def world_body_rotation(landmarks):
    """Body basis from the very same source image as a head candidate."""
    import numpy as np
    left, right, hip_left, hip_right = _current_world_points(landmarks, (11, 12, 23, 24))
    y = _unit((left + right - hip_left - hip_right) / 2)
    lateral = left - right
    x = _unit(lateral - _dot(lateral, y) * y)
    return tuple(np.column_stack((x, y, _cross(x, y))).flat)


@dataclass(frozen=True, slots=True)
class CurrentHeadOrientation:
    frame_id: int
    timestamp_ns: int
    source: str
    rotation: tuple[float, ...]
    source_body_rotation: tuple[float, ...]

    def __post_init__(self):
        if (type(self.frame_id) is not int or self.frame_id < 0 or
                type(self.timestamp_ns) is not int or self.timestamp_ns < 0 or
                self.source not in ('face_transform', 'world_face_plane') or
                len(self.rotation) != 9):
            raise ValueError('current image head orientation required')
        object.__setattr__(self, 'rotation', tuple(_rotation(self.rotation).flat))
        if self.source_body_rotation is None or len(self.source_body_rotation) != 9:
            raise ValueError('same-image source body rotation required')
        object.__setattr__(self, 'source_body_rotation',
                           tuple(_rotation(self.source_body_rotation).flat))


@dataclass(frozen=True, slots=True)
class HeadOrientationPreview:
    preview: object
    source: str
    reason: str
    observation_age_ns: int | None
    target_error_rad: float | None
    rejected_sources: tuple[tuple[str, str], ...] = ()


def _quaternion(rotation):
    """Deterministic largest-diagonal conversion, also covering 180 degrees."""
    m = rotation
    trace = math.fsum(float(m[k, k]) for k in range(3))
    if trace > 0:
        s = 2 * math.sqrt(1 + trace)
        q = (s / 4, (m[2, 1] - m[1, 2]) / s,
             (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s)
    else:
        k = max(range(3), key=lambda i: m[i, i])
        j, l = (k + 1) % 3, (k + 2) % 3
        s = 2 * math.sqrt(max(0., 1 + m[k, k] - m[j, j] - m[l, l]))
        vector = [0., 0., 0.]
        vector[k] = s / 4
        vector[j] = (m[j, k] + m[k, j]) / s
        vector[l] = (m[l, k] + m[k, l]) / s
        q = ((m[l, j] - m[j, l]) / s, *vector)
    return _unit(q)


def _quaternion_rotation(q):
    import numpy as np
    w, x, y, z = _unit(q)
    return np.array(((1 - 2 * (y*y + z*z), 2 * (x*y - z*w), 2 * (x*z + y*w)),
                     (2 * (x*y + z*w), 1 - 2 * (x*x + z*z), 2 * (y*z - x*w)),
                     (2 * (x*z - y*w), 2 * (y*z + x*w), 1 - 2 * (x*x + y*y))))


def _follow(previous, target, dt, *, recovering, cutoff_hz=3., maximum_rate=2.):
    a, b = _quaternion(previous), _quaternion(target)
    cosine = float(_dot(a, b))
    if cosine < 0:
        b = -b
        cosine = -cosine
    angle = 2 * math.acos(max(-1., min(1., cosine)))
    if dt <= 0:
        return previous, angle
    if angle <= min(math.radians(1), maximum_rate * dt):
        return (target if recovering else previous), (0. if recovering else angle)
    step = min(angle * (1 - math.exp(-2 * math.pi * cutoff_hz * dt)), maximum_rate * dt)
    half = angle / 2
    q = (math.sin(half - step / 2) * a + math.sin(step / 2) * b) / math.sin(half)
    return _quaternion_rotation(q), max(0., angle - step)


class PreviewHeadOrientation:
    """Opt-in display head rotation; preserve all source/scoring observations.

    Candidates are ordered: current face transform, then learned world plane.
    A forward hemisphere check rejects the known back-of-head false face.
    Filter relative to the current body, so a torso turn cannot leave the head
    behind. With no accepted candidate, recover toward the original rig head.
    """

    def __init__(self, asset, links, topology_indices, *, maximum_rate=2.):
        if (type(maximum_rate) not in (int, float) or
                not math.isfinite(maximum_rate) or maximum_rate <= 0):
            raise ValueError('finite positive head angular rate required')
        self.maximum_rate = float(maximum_rate)
        self.slot = tuple(asset.bone_names).index('head')
        self.links = tuple(links)
        self.indices = tuple(topology_indices)
        self.link = self.links[self.indices[self.slot]]
        self.reuse_unchanged_segments = False
        self._head_segments = tuple(i for i, link in enumerate(self.links)
                                    if self.link.child in (link.parent, link.child))
        self._head_links = tuple(self.links[i] for i in self._head_segments)
        self.inverse = asset.inverse_bind_matrices_q1_14[self.slot]
        self.bind = _inverse_bind(self.inverse)
        self.length = asset.bone_bind_lengths_q1_14[self.slot] / 16384
        self.reset()

    def reset(self):
        self._timestamp = None
        self._rotation = None
        self._active = False

    def apply(self, preview, candidates=(), *, display_timestamp_ns):
        import numpy as np
        from .avatar_skinning import compose_affine_q1_14
        plan = preview.render_plan
        front = avatar_image_source(plan)
        previous = self._timestamp
        if previous is not None and front.timestamp_ns < previous:
            raise ValueError('non-monotonic current head timestamp')
        if previous is not None and front.timestamp_ns - previous > 250_000_000:
            self.reset()
            previous = None
        if preview.reason not in ('tracked', 'partial') or preview.body_scale is None:
            self.reset()
            return HeadOrientationPreview(preview, 'rig_reference', 'unavailable_body', None, None)
        baseline = _rotation(np.array(plan.matrix_writes[self.slot].matrix.values,
                                       dtype=float).reshape(3, 4)[:, :3])
        joints = plan.joints
        up = _unit(np.array(joints['neck'].position) - joints['pelvis'].position)
        x = np.array(joints['left_shoulder'].position) - joints['right_shoulder'].position
        body_x = _unit(x - _dot(x, up) * up)
        body_back = _cross(body_x, up)
        body = np.column_stack((body_x, up, body_back))
        target, source, reason, age = baseline, 'rig_reference', 'no_current_face', None
        rejected = []
        for candidate in candidates:
            if (candidate.frame_id != front.frame_id or
                    candidate.timestamp_ns != front.timestamp_ns):
                reason = 'source_mismatch'
                rejected.append((candidate.source, reason))
                continue
            age = display_timestamp_ns - candidate.timestamp_ns
            if not 0 <= age <= 100_000_000:
                reason = 'stale_face'
                rejected.append((candidate.source, reason))
                continue
            rotation = np.array(candidate.rotation).reshape(3, 3)
            source_body = np.array(candidate.source_body_rotation).reshape(3, 3)
            if _dot(rotation[:, 2], source_body[:, 2]) <= 0:
                reason = 'face_body_conflict'
                rejected.append((candidate.source, reason))
                continue
            # Retarget a source-relative neck rotation onto the displayed torso.
            target = _matmul(body, _matmul(source_body.T, rotation))
            source, reason = candidate.source, 'current_inferred'
            break
        recovering = source == 'rig_reference'
        dt = (front.timestamp_ns - previous) / 1e9 if previous is not None else 0.
        relative_target = _matmul(body.T, target)
        relative, error = (relative_target, 0.) if self._rotation is None else _follow(
            self._rotation, relative_target, dt, recovering=recovering,
            maximum_rate=self.maximum_rate)
        rotation = _matmul(body, relative)
        if not recovering and _dot(rotation[:, 2], body_back) <= 0:
            # Like the arm endpoint guard, inspect the final nonlinear result.
            relative, rotation, error = relative_target, target, 0.
            reason = 'current_direction_priority'
        active = not recovering or (self._active and error > 1e-7)
        if recovering and not active:
            self._timestamp, self._rotation, self._active = front.timestamp_ns, relative_target, False
            return HeadOrientationPreview(preview, source, reason, age, error, tuple(rejected))
        scale = preview.body_scale
        canonical = lambda v: tuple(round(float(x) * 16777216) / 16777216 for x in v)
        # Use the same canonical state for this output and a repeated source.
        relative = np.array(canonical(relative.flat)).reshape(3, 3)
        rotation = _matmul(body, relative)
        neck = np.array(joints[self.link.parent].position)
        y = _unit(_matmul(rotation, self.bind[:3, 1]))
        end = neck + scale * self.length * y
        segment = BoneSegment3D(self.link.name, self.link.parent, self.link.child,
            tuple(neck / scale), tuple(end / scale), self.length, 0., True)
        segment = replace(segment, start=canonical(segment.start), end=canonical(segment.end))
        matrix = bone_affine_matrix_q1_14(segment, world_to_model_scale=scale,
            local_x_reference=canonical(_matmul(rotation, self.bind[:3, 0])))
        writes = list(plan.matrix_writes)
        writes[self.slot] = MatrixWrite(self.slot, compose_affine_q1_14(matrix, self.inverse))
        joints = dict(joints)
        joints[self.link.child] = replace(joints[self.link.child], position=tuple(end), confidence=0.)
        if self.reuse_unchanged_segments:
            segments = list(plan.segments)
            for i, changed in zip(self._head_segments, build_bone_segments(joints, self._head_links)):
                segments[i] = changed
            segments = tuple(segments)
        else:
            segments = build_bone_segments(joints, self.links)
        rendered = replace(plan, joints=joints, segments=segments,
                           matrix_writes=tuple(writes))
        mode = source if not recovering else 'head_reference_recovery'
        result = replace(preview, render_plan=rendered, head_mode=mode,
            reason='partial' if preview.reason == 'tracked' else preview.reason,
            inferred_slots=tuple(sorted(set(preview.inferred_slots) | {self.slot})))
        self._timestamp = front.timestamp_ns
        self._rotation = np.array(canonical(relative.flat)).reshape(3, 3)
        self._active = active
        return HeadOrientationPreview(result, source, reason, age, error, tuple(rejected))
