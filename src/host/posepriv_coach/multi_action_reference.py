"""Opt-in causal multi-action research baseline; no approved exercise protocol.

Standing bilateral squat, simultaneous bilateral elbow curl and lateral raise.
All thresholds are provisional engineering values. Unknown/missing evidence
cannot complete a repetition; quality is independent of classification.
No learning, model inference, runtime imports or production defaults are added.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import islice
import math
from numbers import Real
from typing import Mapping

from .contracts import CameraView, Keypoint, PoseFrame
ACTIONS = ('squat', 'curl', 'lateral_raise')
ANATOMICAL_VIEW_MAPPINGS = ('device_names', 'swapped')


def angle(a, b, c):
    u = (a[0] - b[0], a[1] - b[1])
    v = (c[0] - b[0], c[1] - b[1])
    d = math.hypot(*u) * math.hypot(*v)
    if d <= 1e-9:
        return None
    cosine = (u[0] * v[0] + u[1] * v[1]) / d
    return math.degrees(math.acos(max(-1, min(1, cosine))))


def extract_action_features(
    front: PoseFrame, side: PoseFrame, threshold: float = 0.5,
    *, side_knee_policy: str = 'best_chain',
    include_side_knees: bool = False,
    include_stance_geometry: bool = False,
    anatomical_view_mapping: str = 'device_names',
) -> dict[str, float | None]:
    """Pixel-isotropic geometry with independent per-feature missingness.

    threshold is either a legacy confidence gate or an explicit encoded raw
    response gate, never a calibration learned by this reference.
    mean_available is an explicit experimental mean of valid projected side
    angles, not a 3D bilateral measurement. best_chain preserves the baseline.

    Input view IDs always identify the physical source streams. The explicit
    swapped mapping routes anatomical frontal geometry from physical SIDE and
    profile geometry from physical FRONT. It only aliases local references;
    source frames, COCO left/right names, rotations and calibration are intact.
    Camera placement alone does not verify the person's orientation throughout
    a recording. The default trusts device names and performs no inference.
    """
    if front.view is not CameraView.FRONT or side.view is not CameraView.SIDE:
        raise ValueError('expected front and side PoseFrames')
    if not usable(threshold) or not 0 <= threshold <= 1:
        raise ValueError('feature gate must be finite within [0,1]')
    if side_knee_policy not in ('best_chain', 'mean_available'):
        raise ValueError('unsupported side knee policy')
    if type(include_side_knees) is not bool:
        raise ValueError('include_side_knees must be boolean')
    if type(include_stance_geometry) is not bool:
        raise ValueError('include_stance_geometry must be boolean')
    if anatomical_view_mapping not in ANATOMICAL_VIEW_MAPPINGS:
        raise ValueError('unsupported anatomical view mapping')
    if anatomical_view_mapping == 'swapped':
        front, side = side, front

    def points(frame, names):
        result = []
        for n in names:
            p = frame.keypoints.get(n)
            if (p is None or not math.isfinite(p.x) or not math.isfinite(p.y)
                    or not math.isfinite(p.confidence) or p.confidence < threshold
                    or not (0 <= p.x <= 1 and 0 <= p.y <= 1)):
                return None
            result.append((p.x * frame.source_width, p.y * frame.source_height, p.confidence))
        return result

    def joint(frame, names):
        p = points(frame, names)
        return angle(*p) if p else None
    result = {}
    if include_side_knees:
        result.update(side_knee_left=None, side_knee_right=None)
    knees = []
    trunks = []
    hips = points(front, ('left_hip', 'right_hip'))
    shoulders = points(front, ('left_shoulder', 'right_shoulder'))
    result['pelvis_y'] = sum((p[1] for p in hips)) / 2 / front.source_height if hips else None
    if hips and shoulders:
        hxy = tuple((sum((p[i] for p in hips)) / 2 for i in (0, 1)))
        sxy = tuple((sum((p[i] for p in shoulders)) / 2 for i in (0, 1)))
        result['torso_length'] = math.hypot(hxy[0] - sxy[0], hxy[1] - sxy[1]) / front.source_height
    else:
        result['torso_length'] = None
    if include_stance_geometry:
        ankles = points(front, ('left_ankle', 'right_ankle'))
        torso = result['torso_length']
        result['front_ankle_span_torso'] = (
            abs(ankles[0][0] - ankles[1][0]) / (torso * front.source_height)
            if ankles and torso is not None and torso * front.source_height > 1e-9
            else None
        )
    for limb in ('left', 'right'):
        hip, knee, ankle = (f'{limb}_{j}' for j in ('hip', 'knee', 'ankle'))
        shoulder, elbow, wrist = (f'{limb}_{j}' for j in ('shoulder', 'elbow', 'wrist'))
        result[f'front_knee_{limb}'] = joint(front, (hip, knee, ankle))
        result[f'elbow_{limb}'] = joint(front, (shoulder, elbow, wrist))
        result[f'arm_{limb}'] = joint(front, (hip, shoulder, elbow))
        p = points(side, (hip, knee, ankle))
        if p:
            value = angle(*p)
            if value is not None:
                knees.append((min((v[2] for v in p)), value))
                if include_side_knees:
                    result[f'side_knee_{limb}'] = value
        p = points(side, (shoulder, hip))
        if p:
            a, b = p
            dx = abs(a[0] - b[0])
            dy = abs(a[1] - b[1])
            if dx + dy > 1e-09:
                trunks.append((min(a[2], b[2]), math.degrees(math.atan2(dx, dy))))
    if not knees:
        result['knee'] = None
    elif side_knee_policy == 'mean_available':
        result['knee'] = sum(value for _, value in knees) / len(knees)
    else:
        result['knee'] = max(knees, key=lambda r: r[0])[1]
    result['trunk'] = max(trunks, key=lambda r: r[0])[1] if trunks else None
    order = points(front, ('left_knee', 'right_knee', 'left_ankle', 'right_ankle'))
    result['leg_order'] = float(min((p[1] for p in order[2:])) > max((p[1] for p in order[:2]))) if order else None
    for name in ('elbow', 'arm'):
        a, b = (result[f'{name}_left'], result[f'{name}_right'])
        result[name] = (a + b) / 2 if a is not None and b is not None else None
        result[name + '_asymmetry'] = abs(a - b) if a is not None and b is not None else None
    return result


def usable(v) -> bool:
    return isinstance(v, (int, float)) and (not isinstance(v, bool)) and math.isfinite(v)


class SourceTimeSeconds(float):
    """Offline seconds adapter retaining integer source ns for interval arithmetic.

    This is a clock only, not a pose/feature schema or a nominal-frame model input.
    Existing seconds callers remain supported independently.
    """
    def __new__(cls, elapsed_ns):
        if type(elapsed_ns) is not int or elapsed_ns < 0:
            raise ValueError('nonnegative integer source nanoseconds required')
        value = super().__new__(cls, elapsed_ns / 1e9)
        if not math.isfinite(value):
            raise ValueError('finite source duration required')
        object.__setattr__(value, '_nanoseconds', elapsed_ns)
        return value

    @property
    def nanoseconds(self):
        return self._nanoseconds

    def __setattr__(self, name, value):
        raise AttributeError('source clock is immutable')

    def __reduce__(self):
        return type(self), (self.nanoseconds,)


def _ns_pair(current, previous):
    return isinstance(current, SourceTimeSeconds) and isinstance(previous, SourceTimeSeconds)


def _elapsed(current, previous):
    return ((current.nanoseconds - previous.nanoseconds) / 1e9
            if _ns_pair(current, previous) else current - previous)


def _after(current, previous):
    return (current.nanoseconds > previous.nanoseconds
            if _ns_pair(current, previous) else current > previous)


def _exceeds(current, previous, seconds):
    # A picosecond protects ordinary float subtraction, far below one source ns.
    # Explicit ns clocks need no tolerance and preserve even very large origins.
    return _elapsed(current, previous) > seconds + (0. if _ns_pair(current, previous) else 1e-12)


def _at_least(current, previous, seconds):
    return _elapsed(current, previous) >= seconds - (0. if _ns_pair(current, previous) else 1e-12)


@dataclass(frozen=True)
class PoseTimeFeatureUpdate:
    """Geometry research features, with observed support; no quality/probability."""

    timestamp_ns: int
    reason: str
    context_start_ns: int | None
    samples: int
    span_seconds: float
    coverage: float
    features: tuple[float, ...] | None = None
    gap_reset: bool = False
    statistic_weighting: str = 'time_weighted'
    feature_schema: str = 'coco17_time_window_102_v1'


class CausalPoseTimeFeatures:
    """Opt-in single-stream COCO17 pixel features using actual source timestamps.

    Native frame IDs may skip normally during inference. Only timestamp gaps
    reset this window. The caller preserves its source/sync clock and separately
    qualifies point responses; this helper neither infers exposure times nor
    supplies missing confidence. No learned model or exercise router is invoked.

    Features are hip-centered, torso-scaled mean/std/latest XY (34 each).
    time_weighted uses trapezoidal weights at actual samples, normalized over
    their observed span. It creates no intermediate poses or unobserved support.
    """

    def __init__(self, source_width: int, source_height: int, *,
                 window_seconds: float = 2., maximum_gap_seconds: float = .5,
                 minimum_history_seconds: float = 1.5, minimum_coverage: float = .8,
                 statistic_weighting: str = 'time_weighted', maximum_samples: int = 256):
        if (type(source_width) is not int or source_width <= 0
                or type(source_height) is not int or source_height <= 0):
            raise ValueError('positive integer source dimensions required')
        durations = (window_seconds, maximum_gap_seconds, minimum_history_seconds)
        if (not all(usable(v) and 0 < v <= 60 for v in durations)
                or maximum_gap_seconds > window_seconds
                or minimum_history_seconds > window_seconds
                or not usable(minimum_coverage) or not 0 <= minimum_coverage <= 1
                or statistic_weighting not in ('observed', 'time_weighted')
                or type(maximum_samples) is not int or not 2 <= maximum_samples <= 4096):
            raise ValueError('invalid bounded time-window configuration')
        self.window_ns, self.gap_ns, self.minimum_ns = (round(v * 1e9) for v in durations)
        if min(self.window_ns, self.gap_ns, self.minimum_ns) <= 0:
            raise ValueError('durations must resolve to positive nanoseconds')
        self.source_width, self.source_height = source_width, source_height
        self.minimum_coverage = minimum_coverage
        self.statistic_weighting = statistic_weighting
        self.history = deque(maxlen=maximum_samples)
        self.last_time = None

    def _normalized(self, points):
        if points is None or not all(isinstance(v, Real) and not isinstance(v, bool)
                                     and math.isfinite(v) for point in points for v in point):
            return None
        points = tuple(tuple(float(v) for v in point) for point in points)
        if not all(0 <= x < self.source_width and 0 <= y < self.source_height for x, y in points):
            return None
        hip = tuple((points[11][i] + points[12][i]) / 2 for i in (0, 1))
        shoulder = tuple((points[5][i] + points[6][i]) / 2 for i in (0, 1))
        scale = math.hypot(shoulder[0] - hip[0], shoulder[1] - hip[1])
        if not math.isfinite(scale) or scale <= 1:
            return None
        values = tuple((point[i] - hip[i]) / scale for point in points for i in (0, 1))
        return values if all(math.isfinite(v) for v in values) else None

    def update(self, timestamp_ns: int, xy_px) -> PoseTimeFeatureUpdate:
        if (type(timestamp_ns) is not int or timestamp_ns < 0
                or self.last_time is not None and timestamp_ns <= self.last_time):
            raise ValueError('strictly increasing nonnegative integer nanoseconds required')
        try:
            points = None if xy_px is None else tuple(tuple(point) for point in xy_px)
        except TypeError as exc:
            raise ValueError('expected 17 pairs of pixel coordinates or missing pose') from exc
        if points is not None and (len(points) != 17 or any(len(p) != 2 for p in points)):
            raise ValueError('expected COCO17 pixel coordinates')
        gap = self.last_time is not None and timestamp_ns - self.last_time > self.gap_ns
        self.last_time = timestamp_ns
        if gap:
            self.history.clear()
        try:
            values = self._normalized(points)
        except (TypeError, ValueError, OverflowError):
            values = None
        if values is None:
            self.history.clear()
            return PoseTimeFeatureUpdate(timestamp_ns, 'invalid_pose', None, 0, 0., 0., gap_reset=gap,
                                         statistic_weighting=self.statistic_weighting)
        self.history.append((timestamp_ns, values))
        while self.history[0][0] <= timestamp_ns - self.window_ns:
            self.history.popleft()
        span_ns = timestamp_ns - self.history[0][0]
        coverage = span_ns / self.window_ns
        common = (timestamp_ns, self.history[0][0], len(self.history), span_ns / 1e9, coverage)
        if span_ns < self.minimum_ns or coverage < self.minimum_coverage:
            return PoseTimeFeatureUpdate(common[0], 'warming_up', *common[1:], gap_reset=gap,
                                         statistic_weighting=self.statistic_weighting)
        samples = [row[1] for row in self.history]
        if self.statistic_weighting == 'observed':
            weights, denominator = [1.] * len(samples), len(samples)
        else:
            times = [row[0] for row in self.history]
            weights = [(times[1] - times[0]) / 2]
            weights += [(times[i + 1] - times[i - 1]) / 2 for i in range(1, len(times) - 1)]
            weights.append((times[-1] - times[-2]) / 2)
            denominator = span_ns
        try:
            mean = tuple(math.fsum(w * row[j] for w, row in zip(weights, samples)) / denominator for j in range(34))
            std = tuple(math.sqrt(max(0., math.fsum(w * (row[j] - mean[j]) ** 2 for w, row in zip(weights, samples)) / denominator)) for j in range(34))
        except (OverflowError, ValueError):
            mean, std = (), ()
        if len(mean) != 34 or not all(math.isfinite(v) for v in mean + std):
            self.history.clear()
            return PoseTimeFeatureUpdate(timestamp_ns, 'invalid_pose', None, 0, 0., 0., gap_reset=gap,
                                         statistic_weighting=self.statistic_weighting)
        return PoseTimeFeatureUpdate(common[0], 'ready', *common[1:], mean + std + samples[-1], gap,
                                     statistic_weighting=self.statistic_weighting)


@dataclass(frozen=True)
class MetricTimeWindow:
    """One metric's observed support and mean/std/latest in its input units."""

    name: str
    reason: str
    context_start_ns: int | None
    samples: int
    span_seconds: float
    coverage: float
    features: tuple[float, float, float] | None = None


@dataclass(frozen=True)
class MetricTimeFeatureUpdate:
    timestamp_ns: int
    metrics: tuple[MetricTimeWindow, ...]
    gap_reset: bool = False
    feature_schema: str = 'metric_time_windows_mean_std_latest_v1'
    statistic_weighting: str = 'time_weighted'


class CausalMetricTimeFeatures:
    """Independent, bounded causal windows for already-qualified scalar metrics.

    Missing/invalid current values clear only their own metric history; omitted
    names also mean missing, never carry-forward. A source timestamp gap clears
    all histories. Samples exactly at the window's left boundary are excluded,
    matching CausalPoseTimeFeatures. Coverage is the retained contiguous observed
    span divided by the configured window, with no interpolation or gap filling.

    Mean/std use trapezoidal weights at actual observations (std is a weighted
    observation statistic, not an integral of an interpolated squared signal).
    The caller supplies units, view/response qualification and meaningful metric
    names. Per-metric readiness is neither a complete 102-vector nor permission
    to classify, count or score an exercise. Existing consumers are not invoked.
    """

    def __init__(self, metric_names, *, window_seconds: float = 2.,
                 maximum_gap_seconds: float = .5, minimum_history_seconds: float = 1.5,
                 minimum_coverage: float = .8, maximum_samples: int = 256):
        if isinstance(metric_names, (str, bytes)):
            raise ValueError('a bounded sequence of distinct metric names required')
        try:
            names = tuple(islice(metric_names, 65))
        except TypeError as exc:
            raise ValueError('metric names must be a sequence') from exc
        if (not 1 <= len(names) <= 64
                or any(not isinstance(n, str) or not n or n.isspace() or len(n) > 64 for n in names)
                or len(set(names)) != len(names)):
            raise ValueError('one to 64 distinct nonempty metric names required')
        durations = (window_seconds, maximum_gap_seconds, minimum_history_seconds)
        if (not all(usable(v) and 0 < v <= 60 for v in durations)
                or maximum_gap_seconds > window_seconds
                or minimum_history_seconds > window_seconds
                or not usable(minimum_coverage) or not 0 <= minimum_coverage <= 1
                or type(maximum_samples) is not int or not 2 <= maximum_samples <= 4096):
            raise ValueError('invalid bounded metric time-window configuration')
        self.window_ns, self.gap_ns, self.minimum_ns = (round(v * 1e9) for v in durations)
        if min(self.window_ns, self.gap_ns, self.minimum_ns) <= 0:
            raise ValueError('durations must resolve to positive nanoseconds')
        self.metric_names = names
        self.minimum_coverage = minimum_coverage
        self._histories = {n: deque(maxlen=maximum_samples) for n in names}
        self._last_time = None

    def update(self, timestamp_ns: int, values: Mapping[str, float | None] | None) -> MetricTimeFeatureUpdate:
        if (type(timestamp_ns) is not int or timestamp_ns < 0
                or self._last_time is not None and timestamp_ns <= self._last_time):
            raise ValueError('strictly increasing nonnegative integer nanoseconds required')
        if values is not None and (not isinstance(values, Mapping)
                                   or any(n not in self._histories for n in values)):
            raise ValueError('expected qualified values for declared metric names only')
        # Validate the entire update before advancing any clock or history.
        qualified = {}
        for name in self.metric_names:
            value = None if values is None else values.get(name)
            try:
                value = (float(value) if isinstance(value, Real) and not isinstance(value, bool)
                         else None)
            except (ValueError, OverflowError):
                value = None
            qualified[name] = value if value is not None and math.isfinite(value) else None
        gap = self._last_time is not None and timestamp_ns - self._last_time > self.gap_ns
        self._last_time = timestamp_ns
        if gap:
            for history in self._histories.values():
                history.clear()
        result = []
        for name, history in self._histories.items():
            value = qualified[name]
            if value is None:
                history.clear()
                result.append(MetricTimeWindow(name, 'invalid_metric', None, 0, 0., 0.))
                continue
            history.append((timestamp_ns, value))
            while history[0][0] <= timestamp_ns - self.window_ns:
                history.popleft()
            span_ns = timestamp_ns - history[0][0]
            coverage = span_ns / self.window_ns
            common = (name, history[0][0], len(history), span_ns / 1e9, coverage)
            if span_ns < self.minimum_ns or coverage < self.minimum_coverage:
                result.append(MetricTimeWindow(common[0], 'warming_up', *common[1:]))
                continue
            times, samples = zip(*history)
            weights = [(times[1] - times[0]) / (2 * span_ns)]
            weights += [(times[i + 1] - times[i - 1]) / (2 * span_ns)
                        for i in range(1, len(times) - 1)]
            weights.append((times[-1] - times[-2]) / (2 * span_ns))
            # Scale both moments before arithmetic; finite extreme inputs must
            # not overflow merely because weights are measured in nanoseconds.
            scale = max(abs(v) for v in samples)
            normalized = [v / scale for v in samples] if scale else [0.] * len(samples)
            mean = max(-1., min(1., math.fsum(w * v for w, v in zip(weights, normalized))))
            variance = math.fsum(w * (v - mean) ** 2 for w, v in zip(weights, normalized))
            std = math.sqrt(max(0., min(1., variance)))
            features = (mean * scale, std * scale, samples[-1])
            result.append(MetricTimeWindow(common[0], 'ready', *common[1:], features))
        return MetricTimeFeatureUpdate(timestamp_ns, tuple(result), gap)


@dataclass(frozen=True)
class ProjectedRootTimeFeatureUpdate:
    """Observed image motion, not world displacement or a quality score."""

    timestamp_ns: int
    reason: str
    context_start_ns: int | None
    samples: int
    span_seconds: float
    coverage: float
    features: tuple[float, ...] | None = None
    gap_reset: bool = False
    statistic_weighting: str = 'time_weighted'
    feature_schema: str = 'projected_root_time_weighted_27_v1'


class CausalProjectedRootTimeFeatures:
    """Opt-in single-view motion of four already-qualified pixel observations.

    Input order is left/right shoulder, left/right hip. Missing body support
    clears this history; unrelated joints are not inputs. The caller qualifies
    model responses and starts a new instance at each annotation boundary.
    Integer source timestamps are retained, never resampled or synchronized.

    Each retained window defines its own origin: pelvis displacement divided
    by the first torso length, and log(torso / first torso). For each of those
    three channels, nine statistics are emitted in STATISTICS order. Coverage
    is actual observed span / configured window, not count / nominal FPS.
    This schema is incompatible with fixed-60-frame legacy root features.
    No classifier, counter, exercise score or deployed path is invoked.
    """

    CHANNELS = ('pelvis_dx_torso0', 'pelvis_dy_torso0', 'log_torso_ratio')
    STATISTICS = ('mean', 'std', 'min', 'max', 'latest', 'endpoint_slope_per_second',
                  'absolute_variation_per_second', 'coverage', 'latest_observed')

    def __init__(self, source_width: int, source_height: int, *,
                 window_seconds: float = 2., maximum_gap_seconds: float = .5,
                 minimum_history_seconds: float = 1.5, minimum_coverage: float = .8,
                 maximum_samples: int = 256):
        if (type(source_width) is not int or source_width <= 0
                or type(source_height) is not int or source_height <= 0):
            raise ValueError('positive integer source dimensions required')
        config = CausalMetricTimeFeatures(
            self.CHANNELS, window_seconds=window_seconds,
            maximum_gap_seconds=maximum_gap_seconds,
            minimum_history_seconds=minimum_history_seconds,
            minimum_coverage=minimum_coverage, maximum_samples=maximum_samples)
        self.window_ns, self.gap_ns, self.minimum_ns = config.window_ns, config.gap_ns, config.minimum_ns
        self.minimum_coverage = config.minimum_coverage
        self.source_width, self.source_height = source_width, source_height
        self.history = deque(maxlen=maximum_samples)
        self.last_time = None

    def update(self, timestamp_ns: int, body_xy_px) -> ProjectedRootTimeFeatureUpdate:
        if (type(timestamp_ns) is not int or timestamp_ns < 0
                or self.last_time is not None and timestamp_ns <= self.last_time):
            raise ValueError('strictly increasing nonnegative integer nanoseconds required')
        try:
            points = (None if body_xy_px is None else
                      tuple(tuple(islice(point, 3)) for point in islice(body_xy_px, 5)))
        except TypeError as exc:
            raise ValueError('expected four body pixel-coordinate pairs or missing body') from exc
        if points is not None and (len(points) != 4 or any(len(p) != 2 for p in points)):
            raise ValueError('expected left/right shoulder and left/right hip')
        gap = self.last_time is not None and timestamp_ns - self.last_time > self.gap_ns
        self.last_time = timestamp_ns
        if gap:
            self.history.clear()
        body = None
        try:
            if points is not None and all(isinstance(v, Real) and not isinstance(v, bool)
                                          and math.isfinite(v) for p in points for v in p):
                points = tuple(tuple(float(v) for v in p) for p in points)
                if all(0 <= x < self.source_width and 0 <= y < self.source_height for x, y in points):
                    shoulder = tuple(points[0][j] / 2 + points[1][j] / 2 for j in (0, 1))
                    hip = tuple(points[2][j] / 2 + points[3][j] / 2 for j in (0, 1))
                    torso = math.hypot(shoulder[0] - hip[0], shoulder[1] - hip[1])
                    if math.isfinite(torso) and torso > 1:
                        body = hip, torso
        except (OverflowError, ValueError):
            body = None
        if body is None:
            self.history.clear()
            return ProjectedRootTimeFeatureUpdate(timestamp_ns, 'invalid_body', None, 0, 0., 0., gap_reset=gap)
        self.history.append((timestamp_ns, *body))
        while self.history[0][0] <= timestamp_ns - self.window_ns:
            self.history.popleft()
        span_ns = timestamp_ns - self.history[0][0]
        coverage = span_ns / self.window_ns
        common = (timestamp_ns, self.history[0][0], len(self.history), span_ns / 1e9, coverage)
        if span_ns < self.minimum_ns or coverage < self.minimum_coverage:
            return ProjectedRootTimeFeatureUpdate(common[0], 'warming_up', *common[1:], gap_reset=gap)
        origin, scale0 = self.history[0][1:]
        times = [row[0] for row in self.history]
        weights = [(times[1] - times[0]) / (2 * span_ns)]
        weights += [(times[j + 1] - times[j - 1]) / (2 * span_ns)
                    for j in range(1, len(times) - 1)]
        weights.append((times[-1] - times[-2]) / (2 * span_ns))
        try:
            samples = [((hip[0] - origin[0]) / scale0, (hip[1] - origin[1]) / scale0,
                        math.log(torso) - math.log(scale0))
                       for _, hip, torso in self.history]
            if not all(math.isfinite(v) for row in samples for v in row):
                raise ValueError('nonfinite relative motion')
            features = []
            for column in zip(*samples):
                scale = max(abs(v) for v in column)
                normalized = [v / scale for v in column] if scale else [0.] * len(column)
                mean = max(-1., min(1., math.fsum(w * v for w, v in zip(weights, normalized))))
                std = math.sqrt(max(0., min(1., math.fsum(
                    w * (v - mean) ** 2 for w, v in zip(weights, normalized)))))
                slope = (column[-1] - column[0]) / common[3]
                variation = math.fsum(abs(b - a) for a, b in zip(column, column[1:])) / common[3]
                features.extend((mean * scale, std * scale, min(column), max(column),
                                 column[-1], slope, variation, coverage, 1.))
            if not all(math.isfinite(v) for v in features):
                raise ValueError('nonfinite motion statistics')
        except (OverflowError, ValueError):
            self.history.clear()
            return ProjectedRootTimeFeatureUpdate(timestamp_ns, 'invalid_body', None, 0, 0., 0., gap_reset=gap)
        return ProjectedRootTimeFeatureUpdate(common[0], 'ready', *common[1:], tuple(features), gap)


class FixedStanceGuard:
    """Opt-in observed stance excursion, not ground contact or a 3D measure.

    Warmup and missing observations are unknown. Excessive observed excursion
    is contradictory evidence. A source gap or missing current span starts a
    new observation window; every sample must have an actual source timestamp.
    """

    WINDOW_SECONDS = 1.5
    MAXIMUM_GAP_SECONDS = .5
    MINIMUM_HISTORY_SECONDS = .5
    MINIMUM_TIME_COVERAGE = .8
    MAXIMUM_EXCURSION_TORSO = .25

    @classmethod
    def contract(cls):
        return {
            'schema': 'projected_fixed_stance_v1',
            'span_definition': 'front_horizontal_ankle_span_per_pixel_torso_length',
            'window_seconds': cls.WINDOW_SECONDS,
            'maximum_gap_seconds': cls.MAXIMUM_GAP_SECONDS,
            'minimum_history_seconds': cls.MINIMUM_HISTORY_SECONDS,
            'minimum_time_coverage': cls.MINIMUM_TIME_COVERAGE,
            'maximum_excursion_torso': cls.MAXIMUM_EXCURSION_TORSO,
            'status': 'provisional_geometry_not_ground_contact_or_human_validated',
        }

    def __init__(self):
        self.history = deque()
        self.last_time = None
        self.last = None

    def update(self, t, span):
        if not usable(t) or (self.last_time is not None and not _after(t, self.last_time)):
            raise ValueError('strictly increasing finite seconds required')
        if self.last_time is not None and _exceeds(t, self.last_time, self.MAXIMUM_GAP_SECONDS):
            self.history.clear()
        self.last_time = t
        valid = usable(span) and span >= 0
        if not valid:
            self.history.clear()
        else:
            self.history.append((t, span))
        while self.history and _exceeds(t, self.history[0][0], self.WINDOW_SECONDS):
            self.history.popleft()
        duration = _elapsed(t, self.history[0][0]) if self.history else 0.
        observed = sum(_elapsed(b[0], a[0]) for a, b in zip(self.history, list(self.history)[1:])
                       if not _exceeds(b[0], a[0], self.MAXIMUM_GAP_SECONDS))
        coverage = observed / duration if duration > 0 else 0.
        excursion = max(v for _, v in self.history) - min(v for _, v in self.history) if self.history else None
        eligible = None
        if valid and excursion is not None and excursion > self.MAXIMUM_EXCURSION_TORSO:
            eligible = False
        elif valid and self.history and _at_least(t, self.history[0][0], self.MINIMUM_HISTORY_SECONDS) and coverage >= self.MINIMUM_TIME_COVERAGE:
            eligible = True
        self.last = {
            'eligible': eligible, 'span_torso': span if valid else None,
            'excursion_torso': excursion, 'history_seconds': duration,
            'time_coverage': coverage,
        }
        return eligible


def _stance_progress(progress_function):
    def constrained(action, f):
        if action in ('curl', 'lateral_raise') and f.get('fixed_stance_eligible') is not True:
            return None
        return progress_function(action, f)
    return constrained


def _stance_features(guard, t, f):
    if guard is None:
        return f
    return {**f, 'fixed_stance_eligible': guard.update(t, f.get('front_ankle_span_torso'))}


def _stance_guard(flag):
    if type(flag) is not bool:
        raise ValueError('require_fixed_stance must be boolean')
    return FixedStanceGuard() if flag else None


def action_progress(action: str, f: Mapping[str, float | None]) -> float | None:
    if action == 'squat':
        k = f.get('knee')
        a = f.get('front_knee_left')
        b = f.get('front_knee_right')
        if not all((usable(v) for v in (k, a, b))) or f.get('leg_order') != 1:
            return None
        if k < 150 and max(a, b) > 170 and (abs(a - b) > 25):
            return None
        return max(0.0, min(1.0, (180 - k) / 100))
    if action == 'curl':
        e, a, k = (f.get('elbow'), f.get('arm'), f.get('knee'))
        asym = f.get('elbow_asymmetry')
        if not all((usable(v) for v in (e, a, k, asym))) or asym > 30 or a > 45 or (k < 150) or (f.get('leg_order') != 1):
            return None
        return max(0.0, min(1.0, (180 - e) / 140))
    if action == 'lateral_raise':
        e, a, k = (f.get('elbow'), f.get('arm'), f.get('knee'))
        asym = f.get('arm_asymmetry')
        if not all((usable(v) for v in (e, a, k, asym))) or asym > 25 or e < 130 or (k < 150) or (f.get('leg_order') != 1):
            return None
        return max(0.0, min(1.0, a / 90))
    raise ValueError('unsupported action')


def cross_view_action_progress(action: str, f: Mapping[str, float | None]) -> float | None:
    """Explicit projected-front conflict candidate; no 3D or quality inference.

    Only the finite front asymmetry veto may be corroborated by two separately
    observed, flexed and similar side knees. Missing points/order still reject.
    """
    prior = action_progress(action, f)
    if action != 'squat' or prior is not None:
        return prior
    k, a, b = (f.get(n) for n in ('knee', 'front_knee_left', 'front_knee_right'))
    if not all(usable(v) for v in (k, a, b)) or f.get('leg_order') != 1:
        return None
    if not (k < 150 and max(a, b) > 170 and abs(a - b) > 25):
        return None
    left, right = (f.get(n) for n in ('side_knee_left', 'side_knee_right'))
    if not all(usable(v) and 0 <= v <= 180 for v in (left, right)):
        return None
    if max(left, right) >= 150 or abs(left - right) > 25:
        return None
    if not min(left, right) <= k <= max(left, right):
        return None
    return max(0.0, min(1.0, (180 - k) / 100))


class ActionRouter:
    """Causal evidence router. Scores are geometric evidence, not class probabilities."""

    def __init__(self, window=1.5, stable=0.25, rest_hold=1.0, *, progress_function=action_progress,
                 require_fixed_stance=False):
        if not all((usable(v) and v > 0 for v in (window, stable, rest_hold))):
            raise ValueError('positive router timing required')
        if not callable(progress_function):
            raise ValueError('progress function must be callable')
        self.stance_guard = _stance_guard(require_fixed_stance)
        self.progress_function = _stance_progress(progress_function) if require_fixed_stance else progress_function
        self.window, self.stable, self.rest_hold = (window, stable, rest_hold)
        self.history = deque()
        self.candidate = 'unknown'
        self.since = None
        self.action = 'unknown'
        self.last_evidence = None
        self.last_time = None

    def update(self, t, f):
        if not usable(t) or (self.last_time is not None and not _after(t, self.last_time)):
            raise ValueError('strictly increasing finite seconds required')
        f = _stance_features(self.stance_guard, t, f)
        if self.last_time is not None and _exceeds(t, self.last_time, 0.5):
            self.history.clear()
            self.action = self.candidate = 'unknown'
            self.since = None
            self.last_evidence = None
        self.last_time = t
        self.history.append((t, dict(f)))
        while self.history and _exceeds(t, self.history[0][0], self.window):
            self.history.popleft()

        def motion(key, action):
            eligible = lambda f: usable(f.get(key)) and self.progress_function(action, f) is not None
            vals = [v[key] for _, v in self.history if eligible(v)]
            peaks = [self.progress_function(action, v) for _, v in self.history if eligible(v)]
            span = _elapsed(t, self.history[0][0]) if self.history else 0
            covered = sum(
                _elapsed(b[0], a[0])
                for a, b in zip(self.history, list(self.history)[1:])
                if not _exceeds(b[0], a[0], 0.5) and eligible(a[1]) and eligible(b[1])
            )
            if (not vals or not peaks or max(peaks) < 0.6
                    or not _at_least(t, self.history[0][0], 0.5) or covered / span < 0.6):
                return 0
            return max(vals) - min(vals)
        candidates = []
        for action, key, excursion in [('squat', 'knee', 25), ('curl', 'elbow', 35), ('lateral_raise', 'arm', 30)]:
            p = self.progress_function(action, f)
            if p is not None and motion(key, action) >= excursion:
                if action == 'squat':
                    rest = [
                        v['pelvis_y'] for _, v in self.history
                        if usable(v.get('pelvis_y')) and usable(v.get('knee'))
                        and v['knee'] >= 160 and v.get('leg_order') == 1
                    ]
                    baseline = sorted(rest)[len(rest) // 2] if rest else None
                    evidence = [
                        v for _, v in self.history
                        if self.progress_function('squat', v) is not None
                        and self.progress_function('squat', v) >= 0.6
                        and usable(v.get('pelvis_y'))
                        and usable(v.get('torso_length')) and v['torso_length'] > 0
                    ]
                    if baseline is None or not any(((v['pelvis_y'] - baseline) / v['torso_length'] >= 0.2 for v in evidence)):
                        continue
                candidates.append(action)
        # Preserve a valid in-flight type during arm/leg overlap. Missing or
        # contradictory primary evidence still releases this commitment.
        current_progress = self.progress_function(self.action, f) if self.action in ACTIONS else None
        if current_progress is not None and current_progress > 0.2:
            candidates = [self.action]
        observed = candidates[0] if len(candidates) == 1 else 'unknown'
        if observed != 'unknown':
            self.last_evidence = t
        elif len(candidates) > 1 or not any((self.progress_function(a, f) is not None for a in ACTIONS)):
            self.action = 'unknown'
            self.last_evidence = None
        elif self.action != 'unknown' and self.last_evidence is not None and not _exceeds(t, self.last_evidence, self.rest_hold):
            observed = self.action
        if observed != self.candidate:
            self.candidate = observed
            self.since = t
        if observed == 'unknown':
            self.action = 'unknown'
        elif self.since is not None and _at_least(t, self.since, self.stable):
            self.action = observed
        return self.action
# (ideal maximum degrees, tolerance degrees, weight), all provisional.
# These are compensatory metrics, independent of primary action progress.
BANDS = {
    'squat': {'trunk': (35, 30, .6), 'front_knee_asymmetry': (15, 30, .4)},
    'curl': {
        'arm': (20, 30, .4), 'trunk': (15, 25, .3),
        'elbow_asymmetry': (15, 30, .3),
    },
    'lateral_raise': {
        'arm_asymmetry': (15, 30, .4), 'elbow_deficit': (20, 30, .3),
        'trunk': (15, 25, .3),
    },
}
SCORE_INTEGRATION_POLICIES = ('sample_penalty_trapezoid_v1', 'linear_metric_exact_v1')


def _score_integration_policy(policy):
    if policy not in SCORE_INTEGRATION_POLICIES:
        raise ValueError('unsupported score integration policy')
    return policy


def _score_integration_metadata(result, policy):
    if policy == 'linear_metric_exact_v1':
        result.update(integration_policy=policy,
                      interpolation_assumption='linear_derived_metric_on_eligible_observed_intervals')
    return result


def _linear_metric_penalty_mean(a, b, ideal, tolerance):
    """Exact clipped penalty integral on a unit linear-derived-metric interval.

    This is an explicit model between two valid observations, not recovery of
    unobserved motion. Derived absolute asymmetries are themselves interpolated;
    their underlying signed angles or keypoint trajectories are not inferred.
    """
    penalty = lambda v: min(1, max(0, (v - ideal) / tolerance))
    knots = [(0., penalty(a)), (1., penalty(b))]
    for boundary, value in ((ideal, 0.), (ideal + tolerance, 1.)):
        if min(a, b) < boundary < max(a, b):
            delta = b - a
            fraction = ((boundary - a) / delta if math.isfinite(delta)
                        else (boundary / 2 - a / 2) / (b / 2 - a / 2))
            knots.append((fraction, value))
    knots.sort()
    return sum((v0 + v1) * .5 * (t1 - t0)
               for (t0, v0), (t1, v1) in zip(knots, knots[1:]))


def quality_metrics(f):
    result = dict(f)
    a, b = (f.get('front_knee_left'), f.get('front_knee_right'))
    result['front_knee_asymmetry'] = abs(a - b) if usable(a) and usable(b) else None
    e = f.get('elbow')
    result['elbow_deficit'] = 180 - e if usable(e) else None
    return result


def score_action_samples(action, samples, *, integration_policy='sample_penalty_trapezoid_v1'):
    """Integrate metric penalties over observed source-time intervals only.

    Scores are withheld unless every component covers at least 80% of the full
    repetition. Gaps over 0.5s are excluded even for direct diagnostic calls.
    The optional exact policy assumes a linear derived metric between valid
    adjacent samples and splits the clipped penalty at its slope changes.
    """
    _score_integration_policy(integration_policy)
    if action not in ACTIONS:
        raise ValueError('unsupported action')
    if len(samples) < 2:
        return _score_integration_metadata(
            {'valid': False, 'reason': 'insufficient_samples', 'components': {}}, integration_policy)
    if any(not usable(t) for t, _ in samples):
        raise ValueError('finite sample times required')
    duration = _elapsed(samples[-1][0], samples[0][0])
    if duration <= 0:
        raise ValueError('positive duration required')
    components = {}
    total = 0
    for name, (ideal, tol, weight) in BANDS[action].items():
        covered = area = 0.0
        for (ta, fa), (tb, fb) in zip(samples, samples[1:]):
            dt = _elapsed(tb, ta)
            if dt <= 0:
                raise ValueError('strict time ordering required')
            a, b = (quality_metrics(fa).get(name), quality_metrics(fb).get(name))
            if _exceeds(tb, ta, 0.5) or not usable(a) or (not usable(b)):
                continue
            covered += dt
            if integration_policy == 'linear_metric_exact_v1':
                area += _linear_metric_penalty_mean(a, b, ideal, tol) * dt
            else:
                pa = min(1, max(0, (a - ideal) / tol))
                pb = min(1, max(0, (b - ideal) / tol))
                area += (pa + pb) * 0.5 * dt
        cov = covered / duration
        value = 100 * (1 - area / covered) if covered > 0 else None
        components[name] = {
            'score_percent': value, 'coverage': cov, 'observed_seconds': covered,
            'ideal_max_deg': ideal, 'tolerance_deg': tol, 'weight': weight,
        }
        total += weight * (value if value is not None else 0)
    valid = all((c['coverage'] >= 0.8 for c in components.values()))
    result = {
        'valid': valid, 'score_percent': total if valid else None,
        'duration_seconds': duration, 'components': components,
        'status': 'provisional_rule_compliance_not_expert_quality',
    }
    return _score_integration_metadata(result, integration_policy)


@dataclass(frozen=True)
class ActionReferenceUpdate:
    action: str
    phase: str
    completed: bool = False
    score: dict | None = None


class ActionCounter:
    """Require observed rest, departure, sustained peak and return, in source time."""

    def __init__(self, *, progress_function=action_progress, require_fixed_stance=False,
                 score_integration_policy='sample_penalty_trapezoid_v1'):
        if not callable(progress_function):
            raise ValueError('progress function must be callable')
        self.score_integration_policy = _score_integration_policy(score_integration_policy)
        self.stance_guard = _stance_guard(require_fixed_stance)
        self.progress_function = _stance_progress(progress_function) if require_fixed_stance else progress_function
        self.last_time = None
        self.action = 'unknown'
        self.phase = 'wait_rest'
        self.samples = []
        self.last_features = None
        self.peak_since = None

    def cancel(self):
        self.phase = 'wait_rest'
        self.samples = []
        self.peak_since = None

    def update(self, action, t, f):
        if action not in ACTIONS + ('unknown',):
            raise ValueError('unsupported action')
        if not usable(t) or (self.last_time is not None and not _after(t, self.last_time)):
            raise ValueError('strictly increasing finite seconds required')
        f = _stance_features(self.stance_guard, t, f)
        previous_time = self.last_time
        previous_features = self.last_features
        if action != self.action or (previous_time is not None and _exceeds(t, previous_time, 0.5)):
            self.cancel()
        self.action = action
        self.last_time = t
        self.last_features = dict(f)
        p = self.progress_function(action, f) if action != 'unknown' else None
        if p is None:
            self.cancel()
            return ActionReferenceUpdate(action, 'unknown')
        if self.phase == 'wait_rest':
            if p <= 0.2:
                self.phase = 'rest'
        elif self.phase == 'rest':
            if p >= 0.3:
                self.phase = 'outbound'
                self.samples = [(previous_time, previous_features), (t, dict(f))]
        else:
            self.samples.append((t, dict(f)))
            if _exceeds(t, self.samples[0][0], 20):
                self.cancel()
                return ActionReferenceUpdate(action, 'unknown')
            if self.phase == 'outbound':
                if p >= 0.75:
                    if self.peak_since is None:
                        self.peak_since = t
                    if _at_least(t, self.peak_since, 0.1):
                        self.phase = 'peak'
                else:
                    self.peak_since = None
                if p <= 0.2:
                    self.phase = 'rest'
                    self.samples = []
                    self.peak_since = None
            elif self.phase in ('peak', 'returning'):
                if p < 0.55:
                    self.phase = 'returning'
                if p <= 0.2:
                    start = self.samples[0][0]
                    samples = self.samples
                    self.phase = 'rest'
                    self.samples = []
                    self.peak_since = None
                    if _at_least(t, start, 0.6):
                        return ActionReferenceUpdate(action, 'rest', True, score_action_samples(
                            action, samples, integration_policy=self.score_integration_policy))
        return ActionReferenceUpdate(action, self.phase)


class AutoActionSession:
    """Track candidate starts causally; emit only the committed class's completion.

    Initial unknown may accumulate a fully observed rest/start. Entering unknown
    from a committed class resets every candidate. A direct class switch may
    retain the incoming candidate only if its observed episode began after the
    previous class was acquired. Older partials and other candidates are reset.
    Nothing is emitted while unknown; missing data and gaps still cancel cycles.
    """

    def __init__(self, *, squat_front_policy='strict', require_fixed_stance=False,
                 score_integration_policy='sample_penalty_trapezoid_v1'):
        if squat_front_policy not in ('strict', 'side_corroborated'):
            raise ValueError('unsupported squat front policy')
        self.score_integration_policy = _score_integration_policy(score_integration_policy)
        self.progress_function = cross_view_action_progress if squat_front_policy == 'side_corroborated' else action_progress
        self.stance_guard = _stance_guard(require_fixed_stance)
        if require_fixed_stance:
            self.progress_function = _stance_progress(self.progress_function)
        self.router = ActionRouter(progress_function=self.progress_function)
        self.counters = {a: ActionCounter(progress_function=self.progress_function,
                                         score_integration_policy=self.score_integration_policy)
                         for a in ACTIONS}
        self.action = 'unknown'
        self.committed_since = None

    def update(self, t, f):
        f = _stance_features(self.stance_guard, t, f)
        action = self.router.update(t, f)
        if self.action != 'unknown' and action != self.action:
            incoming = self.counters.get(action)
            preserve = (
                action in ACTIONS and incoming is not None and bool(incoming.samples)
                and self.committed_since is not None
                and not _after(self.committed_since, incoming.samples[0][0])
            )
            self.counters = {
                a: incoming if a == action and preserve else ActionCounter(
                    progress_function=self.progress_function,
                    score_integration_policy=self.score_integration_policy)
                for a in ACTIONS
            }
        if action != self.action:
            self.committed_since = t if action in ACTIONS else None
        self.action = action
        updates = {a: c.update(a, t, f) for a, c in self.counters.items()}
        return updates[action] if action in ACTIONS else ActionReferenceUpdate('unknown', 'unknown')


def encode_raw_response_gate(value: float) -> float:
    """Monotonic diagnostic encoding into [0,1); never a probability calibration.

    Apply the same map to the threshold. This preserves response ordering and
    gating while using existing synchronization/geometry types. Rule confidence
    percentages derived from the encoded field must never be reported.
    """
    if not usable(value) or value < 0:
        raise ValueError("response gate must be finite and nonnegative")
    return value / (1 + value)


def raw_pose_for_diagnostics(raw) -> PoseFrame:
    """Explicit opt-in bridge; preserve raw records separately for all reporting."""
    points = {}
    for name, point in raw.keypoints.items():
        if point.inside_source and point.x is not None and point.raw_response > 0:
            points[name] = Keypoint(
                point.x, point.y, encode_raw_response_gate(point.raw_response),
            )
    return PoseFrame(
        raw.view, raw.frame_id, raw.timestamp_ns, points,
        raw.source_width, raw.source_height,
    )
