"""Current side-view ear/eye direction for display pitch, without pose calibration."""
from dataclasses import dataclass, replace
import math


@dataclass(frozen=True)
class HeadFaceAxis:
    reason: str
    sides: tuple[str, ...]
    source_age_ns: int
    pitch_rad: float | None = None


def _visible(point):
    return (point is not None and point.confidence >= .5 and
            math.isfinite(point.x) and math.isfinite(point.y) and
            0 <= point.x <= 1 and 0 <= point.y <= 1)


def current_head_face_axis(pair, neck, nose, *, forward_sign, display_timestamp_ns):
    """Preserve current root, length and frontal lean; replace only side pitch."""
    ready = max(pair.front.timestamp_ns, pair.side.timestamp_ns)
    if type(display_timestamp_ns) is not int or display_timestamp_ns < ready:
        raise ValueError('head face axis requires an integer display time at or after both sources')
    age = display_timestamp_ns - ready
    def reject(reason):
        return nose, HeadFaceAxis(reason, (), age)
    if neck is None or nose is None or not neck.valid or not nose.valid:
        return reject('missing_head')
    if age > 100_000_000:
        return reject('stale_source')
    if forward_sign not in (-1., 1.):
        return reject('unknown_forward')
    side = pair.side
    if not _visible(side.keypoints.get('nose')):
        return reject('nose_confidence')
    candidates = []
    for name in ('left', 'right'):
        ear, eye = (side.keypoints.get(name + suffix) for suffix in ('_ear', '_eye'))
        if not _visible(ear) or not _visible(eye):
            continue
        forward = forward_sign * (eye.x - ear.x) * side.source_width
        up = (ear.y - eye.y) * side.source_height
        if forward / max(side.source_width, side.source_height) < .015:
            continue
        angle = math.atan2(up, forward)
        if abs(angle) > math.radians(70):
            continue
        candidates.append((name, angle, min(ear.confidence, eye.confidence)))
    if not candidates:
        return reject('face_geometry_or_confidence')
    if len(candidates) == 2 and abs(candidates[0][1] - candidates[1][1]) > math.radians(20):
        return reject('face_disagreement')
    pitch = math.atan2(sum(math.sin(a) * w for _, a, w in candidates),
                       sum(math.cos(a) * w for _, a, w in candidates))
    offset = tuple(b - a for a, b in zip(neck.position, nose.position))
    length = math.sqrt(sum(v * v for v in offset))
    if not math.isfinite(length) or length <= 1.e-6:
        return reject('degenerate_head')
    x = offset[0] / length
    yz = math.sqrt(max(0., 1. - x * x))
    if yz <= 1.e-6:
        return reject('degenerate_head')
    target = replace(nose, position=(nose.position[0],
        neck.position[1] + length * yz * math.cos(pitch),
        neck.position[2] + length * yz * math.sin(pitch)))
    return target, HeadFaceAxis('supported', tuple(n for n, _, _ in candidates), age, pitch)
