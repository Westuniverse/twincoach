"""Explicit private-bundle supervised action inference on physical paired views.

The source-time 97+97 schema and exported NumPy MLP are the delivered reference;
pose inference, source pairing, quality scoring and device setup stay with callers.
OTHER is a learned class; UNKNOWN denotes insufficient observed support. Raw RTMPose
responses are not calibrated probabilities. No files/devices are opened on import.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .action_feature_geometry import NAMES, metrics
from .contracts import CameraView
from .multi_action_reference import CausalMetricTimeFeatures, CausalProjectedRootTimeFeatures
from .pose_adapter import MOVENET_COCO_KEYPOINTS

ACTIONS = ("squat", "curl", "lateral_raise", "other")
FEATURE_SCHEMA = "posepriv.capture_metric_root_actual_ns97.v41"


def _float_rows(value, width, name):
    original = np.asarray(value)
    if (
        original.ndim not in (1, 2)
        or original.shape[-1] != width
        or original.dtype.kind not in "fiu"
        or not np.isfinite(original).all()
    ):
        raise ValueError(f"Finite {name} with final dimension {width} required")
    with np.errstate(over="ignore", invalid="ignore"):
        result = np.asarray(original, dtype=np.float32)
    if not np.isfinite(result).all():
        raise ValueError(f"{name} must be representable as float32")
    return result


def _bool_rows(value, shape, name):
    value = np.asarray(value)
    if value.dtype != np.bool_ or value.shape != shape:
        raise ValueError(f"Boolean {name} of shape {shape} required")
    return value


def target_observed(ready, target):
    names = ("trunk", "shoulder_left", "shoulder_right")
    if target == 0:
        names += ("knee_left", "knee_right", "hip_left", "hip_right")
    if target == 1:
        names += ("elbow_left", "elbow_right")
    return all(ready[NAMES.index(name)] for name in names)


def encode97(x, ready, root_ready, normalizer):
    """Encode observed rows once, retaining the delivered float32 assignment order."""
    x = _float_rows(x, 97, "observed features").reshape(-1, 97)
    ready = np.asarray(ready)
    if ready.shape == (14,) and len(x) == 1:
        ready = ready.reshape(1, 14)
    ready = _bool_rows(ready, (len(x), 14), "metric readiness")
    rr = np.asarray(root_ready)
    if rr.shape == () and len(x) == 1:
        rr = rr.reshape(1)
    rr = _bool_rows(rr, (len(x),), "root readiness") & bool(normalizer["root_known"])
    out = x.copy()
    known = ready & normalizer["metric_known"][None, :]
    for j in range(14):
        out[:, j * 3 : j * 3 + 3] = np.where(
            known[:, j, None],
            (x[:, j * 3 : j * 3 + 3] - normalizer["metric_mean"][j])
            / normalizer["metric_scale"][j],
            0,
        )
        out[:, 42 + j] = np.where(normalizer["metric_known"][j], x[:, 42 + j], 0)
        out[:, 56 + j] = known[:, j]
    raw = x[:, 70:].reshape(-1, 3, 9)
    root = out[:, 70:].reshape(-1, 3, 9)
    root[:, :, :7] = np.where(
        rr[:, None, None], (raw[:, :, :7] - normalizer["root_mean"]) / normalizer["root_scale"], 0
    )
    root[:, :, 7:] = np.where(rr[:, None, None], raw[:, :, 7:], 0)
    if not np.isfinite(out).all():
        raise ValueError("Nonfinite encoded features")
    return out, known, rr


def _owned_readonly(array):
    # Immutable backing bytes also prevent a caller from re-enabling WRITEABLE.
    return np.frombuffer(array.tobytes(), dtype=array.dtype).reshape(array.shape)


@dataclass(frozen=True)
class Snapshot:
    view: CameraView
    frame_id: int
    timestamp_ns: int
    x97: np.ndarray
    metric_ready: np.ndarray
    root_ready: bool
    warm: bool

    def __post_init__(self):
        if (
            not isinstance(self.view, CameraView)
            or type(self.frame_id) is not int
            or self.frame_id < 0
            or type(self.timestamp_ns) is not int
            or self.timestamp_ns < 0
            or type(self.root_ready) is not bool
            or type(self.warm) is not bool
        ):
            raise ValueError("Invalid physical source snapshot")
        features = _float_rows(self.x97, 97, "snapshot features")
        if features.shape != (97,):
            raise ValueError("Snapshot requires a single 97-vector")
        ready = _bool_rows(self.metric_ready, (14,), "snapshot readiness")
        object.__setattr__(self, "x97", _owned_readonly(features))
        object.__setattr__(self, "metric_ready", _owned_readonly(ready))


class FeatureStream:
    """Observe each physical source once; reset explicitly when geometry/take changes."""

    def __init__(self, view):
        self.view = CameraView(view)
        self.dimensions = self.last = self.first = None
        self.helper = self.root = None
        self.observations = 0

    def observe(self, raw):
        if (
            not isinstance(raw, Mapping)
            or raw.get("schema") != "rtmpose_coco17_raw_v1"
            or raw.get("view") != self.view.value
        ):
            raise ValueError("Require RTMPose raw schema and configured physical camera view")
        if (
            raw.get("coordinate_system") != "normalized_image_xy_unclipped"
            or raw.get("response_definition") != "min(max_simcc_x,max_simcc_y)_uncalibrated"
        ):
            raise ValueError("Coordinate or raw-response contract differs")
        frame, t, w, h = (
            raw.get(name) for name in ("frame_id", "timestamp_ns", "source_width", "source_height")
        )
        if any(type(v) is not int for v in (frame, t, w, h)) or frame < 0 or t < 0 or min(w, h) < 1:
            raise ValueError("Integer source ID/ns and positive dimensions required")
        if self.last is not None and (frame <= self.last[0] or t <= self.last[1]):
            raise ValueError("Repeated/reordered source observation; reset for a new take")
        if self.dimensions is not None and (w, h) != self.dimensions:
            raise ValueError("Camera geometry changed; reset for a new stream")
        points = raw.get("keypoints")
        if not isinstance(points, Mapping) or set(points) != set(MOVENET_COCO_KEYPOINTS):
            raise ValueError("COCO17 named joints required")
        xy = np.full((17, 2), np.nan)
        mask = np.zeros(17, bool)
        for j, name in enumerate(MOVENET_COCO_KEYPOINTS):
            point = points[name]
            if not isinstance(point, Mapping):
                raise TypeError("Named raw joint record must be a mapping")
            if not {"x", "y", "raw_response", "inside_source"}.issubset(point):
                raise ValueError("Raw joints require explicit coordinates and missingness")
            x, y, response = (point.get(k) for k in ("x", "y", "raw_response"))
            if type(response) not in (int, float) or not np.isfinite(response):
                raise ValueError("Finite raw response required")
            if (x is None) != (y is None) or type(point.get("inside_source")) is not bool:
                raise ValueError("Invalid missingness")
            if x is not None:
                if (
                    type(x) not in (int, float)
                    or type(y) not in (int, float)
                    or not np.isfinite([x, y]).all()
                ):
                    raise ValueError("Finite coordinates or explicit missing pair required")
                xy[j] = [x * w, y * h]
                if not np.isfinite(xy[j]).all():
                    raise ValueError("Finite source pixel coordinates required")
            inside = bool(np.isfinite(xy[j]).all() and 0 <= xy[j, 0] < w and 0 <= xy[j, 1] < h)
            if inside != point["inside_source"]:
                raise ValueError("Source-inside flag contradicts geometry")
            mask[j] = inside and response >= 0.5
        if self.helper is None:
            self.dimensions = (w, h)
            self.helper = CausalMetricTimeFeatures(NAMES)
            self.root = CausalProjectedRootTimeFeatures(w, h)
            self.first = t
        if self.last is not None and t - self.last[1] > 500_000_000:
            self.first = t
        result = self.helper.update(t, dict(zip(NAMES, metrics(xy, mask))))
        body = xy[[5, 6, 11, 12]] if mask[[5, 6, 11, 12]].all() else None
        root = self.root.update(t, body)
        root_ready = root.reason == "ready"
        values = np.array(
            [m.features if m.features is not None else (0.0, 0.0, 0.0) for m in result.metrics]
        ).ravel()
        coverage = np.array([m.coverage for m in result.metrics])
        ready = np.array([m.reason == "ready" for m in result.metrics])
        rv = np.array(root.features if root_ready else (0.0,) * 27)
        features = np.r_[values, coverage, ready, rv].astype(np.float32)
        snapshot = Snapshot(
            self.view, frame, t, features, ready, root_ready, t - self.first >= 2_000_000_000
        )
        self.last = (frame, t)
        self.observations += 1
        return snapshot


def _bundle_file(directory, name):
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("Model config requires bundle-relative file paths")
    path = (directory / name).resolve()
    if not path.is_relative_to(directory) or not path.is_file():
        raise ValueError("Model config path must name an existing file inside the bundle")
    return path


def _load_npz(path):
    with np.load(path, allow_pickle=False) as archive:
        return {key: _owned_readonly(archive[key]) for key in archive.files}


def _validate_normalizer(n):
    floats = {
        "metric_mean": (14, 3),
        "metric_scale": (14, 3),
        "root_mean": (3, 7),
        "root_scale": (3, 7),
    }
    shapes = {**floats, "metric_known": (14,), "root_known": ()}
    if set(n) != set(shapes):
        raise ValueError("Unexpected normalizer fields")
    for key, shape in shapes.items():
        value = n[key]
        if value.shape != shape or value.dtype.kind != ("f" if key in floats else "b"):
            raise ValueError("Invalid normalizer dimensions or dtype")
        if key in floats and (
            not np.isfinite(value).all() or key.endswith("scale") and not (value > 0).all()
        ):
            raise ValueError("Normalizer requires finite means and positive finite scales")


class SupervisedActionModel:
    """Load an explicit local bundle once; classification does not run a pose CNN.

    classify is pure with respect to stream identity. The synchronizer must only
    consume a pair when both physical sources advance; observe enforces per-view
    no reuse and still sees observations that the synchronizer later discards.
    """

    def __init__(self, bundle_dir):
        self.directory = Path(bundle_dir).resolve()
        self.config = json.loads(
            _bundle_file(self.directory, "config.json").read_text(encoding="utf-8")
        )
        expected = {
            "schema": "posepriv.stronger_pose_action_bundle.v1",
            "input_dimensions": 194,
            "physical_view_order": ["front", "side"],
            "per_view_features": FEATURE_SCHEMA,
            "classes": list(ACTIONS),
            "unknown_class_id": -1,
            "uncalibrated_response_minimum": 0.5,
            "window_ns": 2_000_000_000,
            "maximum_gap_ns": 500_000_000,
            "max_pair_skew_ns": 50_000_000,
        }
        if not isinstance(self.config, dict) or any(
            type(self.config.get(k)) is not type(v) or self.config.get(k) != v
            for k, v in expected.items()
        ):
            raise ValueError("Unsupported action class/physical-view/geometry/window contract")
        for key in ("pose_model", "action_onnx"):
            if key in self.config:
                _bundle_file(self.directory, self.config[key])
        normalizers = self.config.get("normalizers")
        weights = self.config.get("action_weights")
        if (
            not isinstance(normalizers, list)
            or len(normalizers) != 2
            or not isinstance(weights, list)
            or len(weights) != 3
        ):
            raise ValueError("Two physical-view normalizers and three ensemble members required")
        self.norms = [_load_npz(_bundle_file(self.directory, name)) for name in normalizers]
        self.weights = [_load_npz(_bundle_file(self.directory, name)) for name in weights]
        for n in self.norms:
            _validate_normalizer(n)
        dimensions = (194, 256, 128, 4)
        for p in self.weights:
            if set(p) != {f"{k}{i}" for k in ("w", "b") for i in range(3)}:
                raise ValueError("Unexpected action parameter fields")
            for i in range(3):
                for key, shape in (
                    (f"w{i}", (dimensions[i + 1], dimensions[i])),
                    (f"b{i}", (dimensions[i + 1],)),
                ):
                    if (
                        p[key].shape != shape
                        or p[key].dtype != np.float32
                        or not np.isfinite(p[key]).all()
                    ):
                        raise ValueError("Invalid finite float32 action parameter dimensions")
        self._resets = 0
        self.reset()

    def reset(self):
        self.streams = {v: FeatureStream(v) for v in ("front", "side")}
        self._resets += 1
        self._classified = self._unknown = 0

    def observe(self, raw):
        if not isinstance(raw, Mapping) or raw.get("view") not in self.streams:
            raise ValueError("Unknown physical camera view")
        return self.streams[raw["view"]].observe(raw)

    def logits194(self, encoded):
        x = _float_rows(encoded, 194, "paired encoded input")
        outputs = []
        with np.errstate(over="ignore", invalid="ignore"):
            for p in self.weights:
                value = x
                for i in range(3):
                    value = value @ p[f"w{i}"].T + p[f"b{i}"]
                    if i < 2:
                        value = np.maximum(value, 0)
                outputs.append(value)
            result = np.mean(np.stack(outputs), axis=0)
        if not np.isfinite(result).all():
            raise ValueError("Nonfinite model logits")
        return result

    def classify_features(self, front97, side97, front_ready, side_ready, front_root, side_root):
        fs, fr, ft = encode97(front97, front_ready, front_root, self.norms[0])
        ss, sr, st = encode97(side97, side_ready, side_root, self.norms[1])
        if len(fs) != len(ss):
            raise ValueError("Paired feature batch lengths differ")
        logits = self.logits194(np.concatenate([fs, ss], axis=1))
        prediction = logits.argmax(1).astype(np.int64)
        for i, p in enumerate(prediction):
            if not (
                ft[i] and target_observed(fr[i], int(p)) or st[i] and target_observed(sr[i], int(p))
            ):
                prediction[i] = -1
        return logits, prediction

    def classify(self, front, side):
        if (
            not isinstance(front, Snapshot)
            or not isinstance(side, Snapshot)
            or front.view is not CameraView.FRONT
            or side.view is not CameraView.SIDE
        ):
            raise ValueError("Physical front then side snapshots required")
        skew = abs(front.timestamp_ns - side.timestamp_ns)
        if skew > 50_000_000:
            raise ValueError("Pair exceeds 50ms source-clock skew")
        result = {
            "schema": "posepriv.supervised_action_prediction.v1",
            "front_frame_id": front.frame_id,
            "front_timestamp_ns": front.timestamp_ns,
            "side_frame_id": side.frame_id,
            "side_timestamp_ns": side.timestamp_ns,
            "skew_ns": skew,
            "timestamp_ns": max(front.timestamp_ns, side.timestamp_ns),
            "action": "unknown",
            "class_id": -1,
            "reason": "warmup",
            "logits": None,
            "provider": "rtmpose_m_plus_supervised_paired_mlp_v1",
            "production_qualified": False,
        }
        if front.warm and side.warm:
            logits, prediction = self.classify_features(
                front.x97,
                side.x97,
                front.metric_ready,
                side.metric_ready,
                front.root_ready,
                side.root_ready,
            )
            p = int(prediction[0])
            result.update(
                action="unknown" if p < 0 else ACTIONS[p],
                class_id=p,
                reason="insufficient_predicted_family_observation" if p < 0 else "model_prediction",
                logits=logits[0].tolist(),
            )
        self._classified += 1
        self._unknown += result["class_id"] == -1
        return result

    def stats(self):
        return {
            "schema": "posepriv.supervised_action_stats.v1",
            "observed_front": self.streams["front"].observations,
            "observed_side": self.streams["side"].observations,
            "classified_pairs": self._classified,
            "unknown_pairs": self._unknown,
            "resets": self._resets,
        }
