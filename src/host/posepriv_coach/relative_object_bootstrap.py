# SPDX-License-Identifier: MIT
"""Explicit reference/query initialization, not a continuous temporal update.

An operator may spend seconds selecting a frozen reference. Image matching must
qualify new query correspondences before a normal tracker can be seeded there.
Raw/sync identities are preserved, and tracker time/skip limits are unchanged.
No detection, semantic identity, metric recovery, camera, files or GUI here.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from .capture import CapturedFrame
from .contracts import CameraView
from .relative_object import (
    AssumedObjectCamera, ColoredMarkerFrame, RectangleObjectPrior,
    RelativeObjectFrame, _libraries, _validate_bgr_frame, estimate_relative_object,
)
from .relative_object_tracking import RelativeRectangleTracker, TrackingLimits, _uint64


def _identity(frame: CapturedFrame) -> tuple[CameraView, int, int, int, int, int]:
    return (frame.view, frame.frame_id, frame.timestamp_ns, frame.sync_timestamp_ns,
            frame.width, frame.height)


def _validate_identity(identity) -> None:
    if type(identity) is not tuple or len(identity) != 6:
        raise ValueError("reference/query identity must be an immutable six-field tuple")
    view, frame_id, raw, sync, width, height = identity
    for name, value in (("frame_id", frame_id), ("timestamp_ns", raw),
                        ("sync_timestamp_ns", sync)):
        _uint64(value, name)
    ColoredMarkerFrame(view, frame_id, raw, width, height, (None,) * 4)


@dataclass(frozen=True, slots=True)
class ReferenceRectangleMatch:
    reference_identity: tuple[CameraView, int, int, int, int, int]
    query_identity: tuple[CameraView, int, int, int, int, int]
    result: RelativeObjectFrame
    qualified: bool
    reason: str | None = None
    max_fb_error_px: float | None = None
    max_patch_error: float | None = None
    max_displacement_px: float | None = None

    def __post_init__(self) -> None:
        _validate_identity(self.reference_identity)
        _validate_identity(self.query_identity)
        ref, query = self.reference_identity, self.query_identity
        if (ref[0], ref[4], ref[5]) != (query[0], query[4], query[5]):
            raise ValueError("query must have the same reference view and dimensions")
        if query[1] <= ref[1] or query[2] <= ref[2]:
            raise ValueError("query frame ID and raw timestamp must be strictly later")
        if not isinstance(self.result, RelativeObjectFrame):
            raise ValueError("match result must be RelativeObjectFrame")
        obs = self.result.observation
        if (obs.view, obs.frame_id, obs.timestamp_ns, obs.width, obs.height) != (
                query[0], query[1], query[2], query[4], query[5]):
            raise ValueError("match result must belong to the exact query frame")
        if type(self.qualified) is not bool or (self.result.accepted and not self.qualified):
            raise ValueError("an accepted match requires qualified query correspondences")
        if self.accepted:
            if self.reason is not None:
                raise ValueError("accepted matches must not carry a rejection reason")
        elif type(self.reason) is not str or not self.reason:
            raise ValueError("a rejected match must explain its current query failure")
        for value in (self.max_fb_error_px, self.max_patch_error, self.max_displacement_px):
            if value is not None and (isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError("match diagnostics must be finite and nonnegative")

    @property
    def accepted(self) -> bool:
        return self.qualified and self.result.accepted

    def summary(self) -> dict[str, Any]:
        """Versioned scalar provenance; no pixels, points or pose geometry."""
        names = ("view", "frame_id", "timestamp_ns", "sync_timestamp_ns", "width", "height")
        record = {
            **self.result.summary(),
            "schema": "posepriv.relative_object_reference_match.v1",
            "correspondence_source": "explicit_reference_query_LK_initialization_not_continuous_update",
            "qualified": self.qualified,
            "accepted": self.accepted,
            "reason": self.reason,
            "reference_age_ns": self.query_identity[2] - self.reference_identity[2],
            "maximum_fb_error_px": self.max_fb_error_px,
            "maximum_patch_error": self.max_patch_error,
            "maximum_displacement_px": self.max_displacement_px,
            "continuous_temporal_tracking": False,
            "three_dimensional_accuracy": None,
        }
        for prefix, identity in (("reference", self.reference_identity), ("query", self.query_identity)):
            for name, value in zip(names, identity, strict=True):
                record[f"{prefix}_{name}"] = value.value if isinstance(value, CameraView) else value
        return record


def match_reference_rectangle(reference: CapturedFrame, observation: ColoredMarkerFrame,
        query: CapturedFrame, camera: AssumedObjectCamera, prior: RectangleObjectPrior,
        *, limits: TrackingLimits = TrackingLimits()) -> ReferenceRectangleMatch:
    """Qualify an explicit new image correspondence; never hold reference geometry.

    Matching may span an operator interaction interval, under unchanged spatial
    LK/PnP assumptions. This is not an exception to continuous track gap limits.
    Input owners must remain stable throughout this synchronous operation.
    """
    if (not isinstance(camera, AssumedObjectCamera)
            or not isinstance(prior, RectangleObjectPrior)
            or not isinstance(limits, TrackingLimits)):
        raise ValueError("explicit assumed camera, rectangle prior and tracking limits required")
    if not isinstance(reference, CapturedFrame) or not isinstance(query, CapturedFrame):
        raise ValueError("reference and query must be CapturedFrame")
    ref_identity, query_identity = _identity(reference), _identity(query)
    _validate_identity(ref_identity)
    _validate_identity(query_identity)
    if (reference.view != query.view or (reference.width, reference.height) != (query.width, query.height)
            or (reference.width, reference.height) != (camera.width, camera.height)):
        raise ValueError("reference/query view and dimensions must match the assumed camera")
    if query.frame_id <= reference.frame_id or query.timestamp_ns <= reference.timestamp_ns:
        raise ValueError("query frame ID and raw timestamp must be strictly later")
    if not isinstance(observation, ColoredMarkerFrame) or (
            reference.view, reference.frame_id, reference.timestamp_ns, reference.width, reference.height) != (
            observation.view, observation.frame_id, observation.timestamp_ns, observation.width, observation.height):
        raise ValueError("reference observation requires the exact frozen source-frame identity")
    # Prove both allocations before any pixel copy/C processing, even if the
    # reference geometry will subsequently be rejected.
    ref_image, query_image = _validate_bgr_frame(reference), _validate_bgr_frame(query)

    def rejected(reason: str, *, fb=None, patch=None, displacement=None):
        empty = ColoredMarkerFrame(query.view, query.frame_id, query.timestamp_ns,
                                   query.width, query.height, (None,) * 4, (reason,))
        result = RelativeObjectFrame(empty, camera, prior, "incomplete_markers")
        return ReferenceRectangleMatch(ref_identity, query_identity, result, False, reason,
                                       fb, patch, displacement)

    reference_model = estimate_relative_object(observation, camera, prior)
    if not reference_model.accepted:
        return rejected("reference_" + reference_model.status)
    cv2, np = _libraries()
    points = np.array([(x * reference.width, y * reference.height)
                       for x, y in observation.points], dtype=np.float32).reshape(4, 1, 2)
    if not RelativeRectangleTracker._inside(points, reference.width, reference.height):
        return rejected("reference_float32_outside")
    ref_gray = cv2.cvtColor(ref_image, cv2.COLOR_BGR2GRAY)
    query_gray = cv2.cvtColor(query_image, cv2.COLOR_BGR2GRAY)
    options = {"winSize": (21, 21), "maxLevel": 3,
               "criteria": (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01), "flags": 0}
    forward = RelativeRectangleTracker._flow_output(
        cv2.calcOpticalFlowPyrLK(ref_gray, query_gray, points.copy(), None, **options))
    if forward is None:
        return rejected("forward_status")
    found, forward_patch = forward
    if not RelativeRectangleTracker._inside(found, query.width, query.height):
        return rejected("forward_outside", patch=forward_patch)
    displacement = float(np.max(np.linalg.norm(found.astype(np.float64) - points.astype(np.float64), axis=2)))
    if forward_patch > limits.maximum_patch_error:
        return rejected("forward_patch_error", patch=forward_patch, displacement=displacement)
    if displacement > limits.maximum_displacement_px:
        return rejected("displacement", patch=forward_patch, displacement=displacement)
    backward = RelativeRectangleTracker._flow_output(
        cv2.calcOpticalFlowPyrLK(query_gray, ref_gray, found.copy(), None, **options))
    if backward is None:
        return rejected("backward_status", displacement=displacement)
    returned, backward_patch = backward
    patch = max(forward_patch, backward_patch)
    fb = float(np.max(np.linalg.norm(returned.astype(np.float64) - points.astype(np.float64), axis=2)))
    diagnostics = {"fb": fb, "patch": patch, "displacement": displacement}
    if not RelativeRectangleTracker._inside(returned, reference.width, reference.height):
        return rejected("backward_outside", **diagnostics)
    if patch > limits.maximum_patch_error:
        return rejected("backward_patch_error", **diagnostics)
    if fb > limits.maximum_fb_error_px:
        return rejected("forward_backward_error", **diagnostics)
    pixels = found[:, 0].astype(np.float64)
    edges = np.roll(pixels, -1, axis=0) - pixels
    following = np.roll(edges, -1, axis=0)
    cross = edges[:, 0] * following[:, 1] - edges[:, 1] * following[:, 0]
    if np.any(cross <= 0):
        return rejected("nonconvex_or_mirrored_correspondences", **diagnostics)
    current_observation = ColoredMarkerFrame(query.view, query.frame_id, query.timestamp_ns,
        query.width, query.height, tuple((float(p[0]) / query.width, float(p[1]) / query.height) for p in pixels))
    result = estimate_relative_object(current_observation, camera, prior)
    return ReferenceRectangleMatch(ref_identity, query_identity, result, True,
                                   None if result.accepted else "query_" + result.status,
                                   fb, patch, displacement)
