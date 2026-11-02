"""Offline H36M17 conditioning/windows; no model, camera, scoring or device I/O.

Coordinates use actual oriented image pixels. Derived head/torso points and all
subsequent learned depth remain inferred, including high-confidence inputs.
"""
from __future__ import annotations

import math


def ordered_sources(records, *, width, height):
    """Deduplicate exact raw images, rejecting changed coordinates or clocks."""
    if type(width) is not int or type(height) is not int or min(width, height) <= 0:
        raise ValueError('positive oriented image dimensions required')
    sources = {}
    frame_times = {}
    geometry = None
    for row in records:
        if (row.get('schema_version') != 1 or
                row.get('scope') != 'offline_world_pose_reference'):
            raise ValueError('original offline world reference schema required')
        current = (row['chapter'], row['view'], row['rotation_degrees'])
        if (current[1] not in ('front', 'side') or current[2] not in (0, 90, 180, 270) or
                geometry is not None and current != geometry):
            raise ValueError('one original chapter/camera/rotation required')
        geometry = current
        if ('image_width' in row and (row['image_width'], row['image_height']) !=
                (width, height)):
            raise ValueError('oriented image dimensions differ')
        frame, stamp = row['source_frame_id'], row['source_timestamp_ns']
        if (type(frame) is not int or type(stamp) is not int or min(frame, stamp) < 0 or
                frame in frame_times and frame_times[frame] != stamp):
            raise ValueError('original frame identity and clock required')
        frame_times[frame] = stamp
        people = row['normalized_landmarks']
        if len(people) > 1 or people and len(people[0]) != 33:
            raise ValueError('zero or one original 33-point person required')
        for point in people[0] if people else ():
            if (not all(math.isfinite(point[k]) for k in ('x', 'y', 'presence', 'visibility')) or
                    not all(0 <= point[k] <= 1 for k in ('presence', 'visibility'))):
                raise ValueError('finite coordinates and original separate confidences required')
        key = (frame, stamp)
        if key in sources and sources[key]['normalized_landmarks'] != people:
            raise ValueError('same raw image has different conditioning coordinates')
        sources.setdefault(key, row)
    result = sorted(sources.values(), key=lambda row: row['source_timestamp_ns'])
    if not result or any(b['source_timestamp_ns'] <= a['source_timestamp_ns']
                         for a, b in zip(result, result[1:])):
        raise ValueError('nonempty, strictly ordered original source clocks required')
    return result


def h36m_conditioning(points, *, width, height):
    """MediaPipe33 -> H36M17 XY/confidence, with four explicit virtual points.

    Joint9 is the predicted nose; joint10 is an eye/ear midpoint proxy, not a
    detected Halpe head or anatomical H36M head annotation. Never call input GT.
    """
    import numpy as np
    if len(points) != 33:
        raise ValueError('33 original image points required')
    source = np.array([[v['x'] * width, v['y'] * height,
                        min(v['presence'], v['visibility'])] for v in points])
    output = np.zeros((17, 3))
    direct = {1: 24, 2: 26, 3: 28, 4: 23, 5: 25, 6: 27, 9: 0,
              11: 11, 12: 13, 13: 15, 14: 12, 15: 14, 16: 16}
    for target, origin in direct.items():
        output[target] = source[origin]
    for target, indices in ((0, (23, 24)), (8, (11, 12)), (10, (2, 5, 7, 8))):
        output[target, :2] = source[list(indices), :2].mean(axis=0)
        output[target, 2] = source[list(indices), 2].min()
    output[7, :2] = (output[0, :2] + output[8, :2]) / 2
    output[7, 2] = min(output[0, 2], output[8, 2])
    return output


def continuous_segments(rows, *, max_gap_ns=100_000_000):
    """Real source indices only; missing detections and long gaps split context."""
    if type(max_gap_ns) is not int or max_gap_ns <= 0:
        raise ValueError('positive original-source gap bound required')
    segments, current = [], []
    previous_stamp = None
    for index, row in enumerate(rows):
        stamp = row['source_timestamp_ns']
        if previous_stamp is not None and stamp <= previous_stamp:
            raise ValueError('strict original source order required')
        previous_stamp = stamp
        if not row['normalized_landmarks']:
            if current:
                segments.append(tuple(current))
                current = []
            continue
        if current and stamp - rows[current[-1]]['source_timestamp_ns'] > max_gap_ns:
            segments.append(tuple(current))
            current = []
        current.append(index)
    if current:
        segments.append(tuple(current))
    return tuple(segments)


def overlapping_windows(length):
    """At most243 real sources/window, stride121; no padding or resampling."""
    if type(length) is not int or length <= 0:
        raise ValueError('nonempty real segment required')
    starts = [0] if length <= 243 else sorted(set(range(0, length - 242, 121)) | {length - 243})
    return tuple((start, min(length, start + 243)) for start in starts)


def crop_extent(conditioning):
    """Scale of upstream crop_scale([1,1]), including its confidence mask."""
    import numpy as np
    points = np.asarray(conditioning)
    if points.ndim != 3 or points.shape[1:] != (17, 3) or not np.isfinite(points).all():
        raise ValueError('finite [real_sources,17,XYconfidence] conditioning required')
    valid = points[points[..., 2] != 0, :2]
    if len(valid) < 4:
        raise ValueError('insufficient conditioning for upstream crop')
    scale = float(np.ptp(valid, axis=0).max())
    if scale <= 0:
        raise ValueError('nondegenerate upstream crop required')
    return scale


def root_relative_pixel_equivalent(prediction, crop_scale_pixels):
    """Undo uniform crop scale before overlapping learned estimates.

    This is weak-perspective pixel-equivalent depth, not calibrated pixels/metres.
    Root subtraction cancels crop translation and the model's global root.
    """
    import numpy as np
    prediction = np.asarray(prediction)
    if (prediction.ndim != 3 or prediction.shape[1:] != (17, 3) or
            not np.isfinite(prediction).all() or not math.isfinite(crop_scale_pixels) or
            crop_scale_pixels <= 0):
        raise ValueError('finite real-window predictions and positive crop scale required')
    return (prediction - prediction[:, 0:1]) * crop_scale_pixels / 2


def overlap_weights(length):
    """Positive triangular weights keep even short/unpaired edge sources covered."""
    import numpy as np
    if type(length) is not int or not 1 <= length <= 243:
        raise ValueError('real window of1..243 sources required')
    indices = np.arange(length)
    return 1 + np.minimum(indices, indices[::-1])
