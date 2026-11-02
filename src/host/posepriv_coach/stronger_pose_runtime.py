"""Explicit CPU RTMPose-M/action candidate, separate from display confidence.

The private bundle supplies data, never imported Python. Canonical preprocessing
and SimCC decoding come from RTMPoseONNXReference. No ORT import/session, model
download, camera, or device operation happens on import or construction.
Source/license records remain with the existing bundle and repository dependency
register; no additional package is required by this adapter.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from threading import RLock
from time import monotonic_ns
from types import MappingProxyType
from typing import Any

from .capture import CapturedFrame
from .contracts import CameraView, Keypoint, PoseFrame
from .rtmpose_onnx_reference import RawRTMPoseFrame, load_rtmpose_onnx_reference

DISPLAY_WEIGHT_SEMANTICS = "binary_display_observation_weight_not_calibrated_probability"


@dataclass(frozen=True, slots=True)
class DisplayPoseObservation:
    """A render-only pose accompanied by its unchanged uncalibrated raw source.

    Accepted raw points get weight 1; missing/outside/response<.5 points get 0.
    This is an observation mask, not an estimated probability or expert score.
    Callers must keep this display route separate from quality scoring.
    """
    pose: PoseFrame
    raw: RawRTMPoseFrame
    confidence_semantics: str = DISPLAY_WEIGHT_SEMANTICS
    scoring_qualified: bool = False


def raw_pose_to_display(raw: RawRTMPoseFrame) -> DisplayPoseObservation:
    """Apply the fixed .5 raw/source gate without clipping or rewriting raw data."""
    if not isinstance(raw, RawRTMPoseFrame):
        raise TypeError("display adapter requires RawRTMPoseFrame")
    points = {}
    for name, point in raw.keypoints.items():
        finite = (point.x is not None and point.y is not None
                  and isfinite(point.x) and isfinite(point.y))
        accepted = (finite and point.inside_source and 0 <= point.x < 1
                    and 0 <= point.y < 1 and isfinite(point.raw_response)
                    and point.raw_response >= .5)
        # Zero-weight finite coordinates remain available to display diagnostics;
        # absent coordinates need a finite placeholder for the shared PoseFrame.
        points[name] = Keypoint(point.x if finite else 0., point.y if finite else 0.,
                               1. if accepted else 0.)
    pose = PoseFrame(raw.view, raw.frame_id, raw.timestamp_ns, points,
                     raw.source_width, raw.source_height)
    return DisplayPoseObservation(pose, raw)


@dataclass(frozen=True, slots=True)
class StrongerPoseObservation:
    raw: RawRTMPoseFrame
    action_snapshot: Any


@dataclass(frozen=True, slots=True)
class StrongerPosePairResult:
    observations: Mapping[CameraView, StrongerPoseObservation]
    display: Mapping[CameraView, DisplayPoseObservation]
    action: Mapping[str, Any]


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


class StrongerPoseRuntime:
    """One serialized CPU session plus physical per-view action histories.

    infer(frame) returns canonical raw data and observes it exactly once, even
    when that frame is subsequently unpaired. infer_observation also exposes
    the corresponding immutable action snapshot. classify consumes snapshots
    only after both physical streams have advanced. infer_pair is convenience
    for callers that already selected a complete pair, without anatomical swap.

    Upright source BGR and rotation are caller-owned. The bundle source-clock
    contract excludes an adjusted sync clock: unequal capture/sync timestamps
    are rejected rather than silently using one in the canonical reference.
    close serializes with in-flight inference; external process shutdown owns
    any hard timeout, since ORT has no portable bounded cancellation contract.
    pair_parallel=True uses two independent non-spinning ORT sessions only in
    infer_pair; both canonical outputs finish before front/side observe calls.
    num_threads applies separately to each session, not to a shared total pool.
    """
    def __init__(self, bundle_config: str | Path, *, num_threads: int = 2,
                 allow_spinning: bool = False, action_model=None,
                 reference_loader=None, pair_parallel: bool = False) -> None:
        if type(num_threads) is not int or num_threads < 1:
            raise ValueError("num_threads must be a positive integer")
        if type(allow_spinning) is not bool:
            raise ValueError("allow_spinning must be boolean")
        if type(pair_parallel) is not bool:
            raise ValueError("pair_parallel must be boolean")
        if pair_parallel and allow_spinning:
            raise ValueError("pair_parallel requires allow_spinning=False")
        path = Path(bundle_config).resolve()
        if path.is_dir():
            path /= "config.json"
        if path.name != "config.json":
            raise ValueError("bundle config must be the action bundle's config.json")
        config = json.loads(path.read_text(encoding="utf-8"))
        if (config.get("schema") != "posepriv.stronger_pose_action_bundle.v1"
                or config.get("physical_view_order") != ["front", "side"]
                or type(config.get("uncalibrated_response_minimum")) not in (int, float)
                or config["uncalibrated_response_minimum"] != .5):
            raise ValueError("unsupported stronger-model bundle/source contract")
        model_name = config.get("pose_model")
        if not isinstance(model_name, str) or not model_name or Path(model_name).is_absolute():
            raise ValueError("pose_model must be a relative bundle artifact")
        model_path = (path.parent / model_name).resolve()
        if not model_path.is_relative_to(path.parent) or not model_path.is_file():
            raise ValueError("pose_model must exist within the bundle")
        if action_model is not None:
            self._validate_action_model(action_model)
        self.bundle_config, self.model_path = path, model_path
        self.num_threads, self.allow_spinning = num_threads, allow_spinning
        self.pair_parallel = pair_parallel
        self._loader = reference_loader or load_rtmpose_onnx_reference
        self._action = action_model
        self._reference = None
        self._pair_reference = self._pair_executor = None
        self._lock = RLock()
        self._closed = self._failed = False
        self._frames = self._pairs = self._total_ns = 0
        self._latest = {}
        self._last_source = {}
        self._consumed = {}
        self.last_timing_ns = {}

    @staticmethod
    def _validate_action_model(model):
        if any(not callable(getattr(model, name, None)) for name in ("observe", "classify", "reset")):
            raise TypeError("action model must implement observe, classify and reset")

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("stronger pose runtime is closed")
        if self._failed:
            raise RuntimeError("stronger pose runtime failed; reset before reuse")

    def _ensure_models(self):
        if self._action is None:
            from .supervised_action import SupervisedActionModel
            self._action = SupervisedActionModel(self.bundle_config.parent)
            self._validate_action_model(self._action)
        if self._reference is None:
            self._reference = self._loader(self.model_path, num_threads=self.num_threads,
                                           allow_spinning=self.allow_spinning)

    def _validate_frame(self, frame):
        if not isinstance(frame, CapturedFrame) or type(frame.view) is not CameraView:
            raise TypeError("CapturedFrame with an explicit physical CameraView required")
        if (any(type(v) is not int for v in (frame.frame_id, frame.timestamp_ns,
                                           frame.sync_timestamp_ns, frame.width, frame.height))
                or min(frame.frame_id, frame.timestamp_ns) < 0
                or min(frame.width, frame.height) < 1):
            raise ValueError("source ID/ns/dimensions must be exact integers")
        if frame.sync_timestamp_ns != frame.timestamp_ns:
            raise ValueError("stronger bundle requires original source-read clock, without sync offset")
        if frame.pixel_format != "bgr8":
            raise ValueError("upright source bgr8 required")
        old = self._last_source.get(frame.view)
        if old is not None and (frame.frame_id <= old[0] or frame.timestamp_ns <= old[1]
                                or (frame.width, frame.height) != old[2:]):
            raise ValueError("source must advance with fixed dimensions; reset for a new stream")

    def _observe_raw(self, frame, raw):
        expected = (frame.view, frame.frame_id, frame.timestamp_ns, frame.width, frame.height)
        if (not isinstance(raw, RawRTMPoseFrame) or raw.view is not frame.view
                or any(type(v) is not int for v in (raw.frame_id, raw.timestamp_ns,
                                                   raw.source_width, raw.source_height))
                or (raw.view, raw.frame_id, raw.timestamp_ns,
                    raw.source_width, raw.source_height) != expected):
            raise ValueError("pose reference changed physical source identity")
        snapshot = self._action.observe(raw.to_record())
        result = StrongerPoseObservation(raw, snapshot)
        self._frames += 1
        self._last_source[frame.view] = (frame.frame_id, frame.timestamp_ns, frame.width, frame.height)
        self._latest[frame.view] = result
        return result

    def _parallel_observations(self, frames, bboxes):
        """Only canonical pose inference runs in workers; histories stay caller-owned."""
        from concurrent.futures import ThreadPoolExecutor, wait

        started = monotonic_ns()
        futures = {}
        try:
            self._ensure_models()
            if self._pair_reference is None:
                other = self._loader(self.model_path, num_threads=self.num_threads,
                                     allow_spinning=False)
                if (other is self._reference or getattr(other, 'session', other)
                        is getattr(self._reference, 'session', self._reference)):
                    raise ValueError("pair_parallel requires two independent pose sessions")
                self._pair_reference = other
            if self._pair_executor is None:
                self._pair_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='posepriv-pose-pair')
            by_view = {frame.view: frame for frame in frames}
            for view, reference in ((CameraView.FRONT, self._reference),
                                    (CameraView.SIDE, self._pair_reference)):
                futures[view] = self._pair_executor.submit(
                    reference.infer, by_view[view], bbox=None if bboxes is None else bboxes.get(view))
            wait(tuple(futures.values()))
            # Resolve both before observing either: a failed pose worker cannot
            # leave the other worker using sessions after reset/close returns.
            raws = {view: future.result() for view, future in futures.items()}
            inferred = monotonic_ns()
            observations = {view: self._observe_raw(by_view[view], raw) for view, raw in raws.items()}
            finished = monotonic_ns()
            self.last_timing_ns = {'pose_cpu': inferred-started, 'action_observe': finished-inferred}
            self._total_ns += finished-started
            return observations
        except Exception:
            self._failed = True
            raise
        finally:
            # Also drain an already-submitted worker if a later submit fails.
            if futures:
                wait(tuple(futures.values()))

    def infer_observation(self, frame: CapturedFrame, *, bbox=None) -> StrongerPoseObservation:
        with self._lock:
            self._ensure_open()
            self._validate_frame(frame)
            started = monotonic_ns()
            try:
                self._ensure_models()
                raw = self._reference.infer(frame, bbox=bbox)
                inferred = monotonic_ns()
                result = self._observe_raw(frame, raw)
            except Exception:
                self._failed = True
                raise
            finished = monotonic_ns()
            self.last_timing_ns = {"pose_cpu": inferred-started, "action_observe": finished-inferred}
            self._total_ns += finished-started
            return result

    def infer(self, frame: CapturedFrame, *, bbox=None) -> RawRTMPoseFrame:
        return self.infer_observation(frame, bbox=bbox).raw

    def latest_observation(self, view: CameraView) -> StrongerPoseObservation | None:
        with self._lock:
            self._ensure_open()
            return self._latest.get(CameraView(view))

    def classify(self, front: StrongerPoseObservation, side: StrongerPoseObservation) -> Mapping[str, Any]:
        with self._lock:
            self._ensure_open()
            for expected, observation in ((CameraView.FRONT, front), (CameraView.SIDE, side)):
                if (not isinstance(observation, StrongerPoseObservation)
                        or observation is not self._latest.get(expected)):
                    raise ValueError("classify requires latest snapshots owned by this runtime")
                raw = observation.raw
                if raw.frame_id <= self._consumed.get(expected, -1):
                    raise ValueError("both physical views must advance before another pair")
            try:
                prediction = _freeze(self._action.classify(front.action_snapshot, side.action_snapshot))
            except Exception:
                self._failed = True
                raise
            self._consumed = {CameraView.FRONT: front.raw.frame_id, CameraView.SIDE: side.raw.frame_id}
            self._pairs += 1
            return prediction

    def infer_pair(self, frames, *, bboxes: Mapping[CameraView, Any] | None = None) -> StrongerPosePairResult:
        with self._lock:
            self._ensure_open()
            frames = tuple(frames)
            if len(frames) != 2 or {f.view for f in frames} != set(CameraView):
                raise ValueError("one physical front and one physical side required")
            for frame in frames:
                self._validate_frame(frame)
            if self.pair_parallel:
                observations = self._parallel_observations(frames, bboxes)
            else:
                observations = {frame.view: self.infer_observation(
                    frame, bbox=None if bboxes is None else bboxes.get(frame.view)) for frame in frames}
            prediction = self.classify(observations[CameraView.FRONT], observations[CameraView.SIDE])
            display = {v: raw_pose_to_display(o.raw) for v, o in observations.items()}
            return StrongerPosePairResult(MappingProxyType(observations), MappingProxyType(display), prediction)

    def reset(self):
        """Start a new source history; no model reload or automatic recovery."""
        with self._lock:
            if self._closed:
                raise RuntimeError("stronger pose runtime is closed")
            if self._action is not None:
                self._action.reset()
            self._latest.clear()
            self._last_source.clear()
            self._consumed.clear()
            self._failed = False

    def stats(self):
        with self._lock:
            return {"backend": "onnxruntime_cpu_rtmpose_m", "frames": self._frames,
                        "classified_pairs": self._pairs, "total_infer_observe_ns": self._total_ns,
                        "loaded": self._reference is not None, "closed": self._closed, "failed": self._failed,
                        "num_threads": self.num_threads, "inter_op_num_threads": 1,
                        "pair_parallel": self.pair_parallel,
                        "pose_sessions": int(self._reference is not None)+int(self._pair_reference is not None),
                        "allow_spinning": self.allow_spinning, "production_qualified": False,
                        "display_confidence_semantics": DISPLAY_WEIGHT_SEMANTICS,
                        "display_scoring_qualified": False, "cached_views": len(self._latest),
                        "pending_views": sum(o.raw.frame_id > self._consumed.get(v, -1)
                                          for v, o in self._latest.items())}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._latest.clear()
            # Canonical ORT reference has no close API; dropping the owned
            # session reference releases its resources without a private hook.
            try:
                if self._pair_executor is not None:
                    self._pair_executor.shutdown(wait=True, cancel_futures=True)
            finally:
                self._pair_executor = self._pair_reference = None
                self._reference = self._action = None

    def __enter__(self):
        with self._lock:
            self._ensure_open()
        return self

    def __exit__(self, *exc):
        self.close()
