# SPDX-License-Identifier: MIT
"""Explicit single-view four-correspondence tracking, not calibrated recovery.

The default retains one private grayscale frame; the explicitly selected native
backend retains its private previous pyramid instead. A lost track requires an
explicit new seed; no held geometry, automatic detection or retry.
Use a tracker from one calling thread. No camera, window or hardware is opened.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from .capture import CapturedFrame
from .relative_object import (
    AssumedObjectCamera, ColoredMarkerFrame, RectangleObjectPrior,
    RelativeObjectFrame, _libraries, _validate_bgr_frame, estimate_relative_object,
)


def _uint64(value: int, name: str) -> None:
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError(f"{name} must be a uint64 integer")


@dataclass(frozen=True, slots=True)
class TrackingLimits:
    maximum_gap_ns: int = 200_000_000
    max_frame_gap: int = 5
    maximum_fb_error_px: float = 1.0
    maximum_patch_error: float = 15.0
    maximum_displacement_px: float = 64.0

    def __post_init__(self) -> None:
        for name in ("maximum_gap_ns", "max_frame_gap"):
            value = getattr(self, name)
            _uint64(value, name)
            if value == 0:
                raise ValueError(f"{name} must be positive")
        for name in ("maximum_fb_error_px", "maximum_patch_error", "maximum_displacement_px"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True, slots=True)
class TrackingStep:
    result: RelativeObjectFrame
    sync_timestamp_ns: int
    status: str
    visual_valid: bool
    maximum_fb_error_px: float | None = None
    maximum_patch_error: float | None = None
    maximum_displacement_px: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.result, RelativeObjectFrame):
            raise ValueError("tracking result must be RelativeObjectFrame")
        _uint64(self.sync_timestamp_ns, "sync_timestamp_ns")
        if self.status not in ("seeded", "seed_rejected", "needs_seed", "lost",
                               "model_rejected", "tracked"):
            raise ValueError("unknown tracking status")
        if type(self.visual_valid) is not bool:
            raise ValueError("visual_valid must be boolean")
        if self.result.accepted != (self.status in ("seeded", "tracked")):
            raise ValueError("tracking status and model acceptance disagree")
        if self.visual_valid != (self.status in ("tracked", "model_rejected")):
            raise ValueError("only qualified updates have visual validity")
        for value in (self.maximum_fb_error_px, self.maximum_patch_error,
                      self.maximum_displacement_px):
            if value is not None and (isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError("tracking diagnostics must be finite and nonnegative")

    def summary(self) -> dict[str, Any]:
        """Scalar diagnostics only; no pixels, corner coordinates or pose geometry."""
        observation = self.result.observation
        return {
            **self.result.camera.assumptions(),
            "schema": "posepriv.relative_object_tracking.v1",
            "status": self.status,
            "model_status": self.result.status,
            "visual_valid": self.visual_valid,
            "accepted": self.result.accepted,
            "correspondence_source": "explicit_seed_then_sparse_LK_not_detection",
            "valid_correspondences": sum(p is not None for p in observation.points),
            "view": observation.view.value,
            "frame_id": observation.frame_id,
            "timestamp_ns": observation.timestamp_ns,
            "sync_timestamp_ns": self.sync_timestamp_ns,
            "reason": ";".join(observation.problems) or None,
            "maximum_fb_error_px": self.maximum_fb_error_px,
            "maximum_patch_error": self.maximum_patch_error,
            "maximum_displacement_px": self.maximum_displacement_px,
            "reprojection_rms_px": self.result.reprojection_rms_px,
            "reprojection_max_px": self.result.reprojection_max_px,
            "candidate_count": self.result.candidate_count,
            "marker_height_ratio": self.result.prior.height_ratio,
            "model_depth_ratio": self.result.prior.model_depth_ratio,
            "model_depth_is_prior": True,
            "three_dimensional_accuracy": None,
        }


class RelativeRectangleTracker:
    """Fail-closed four-point LK with existing, unchanged relative planar PnP."""

    def __init__(self, camera: AssumedObjectCamera, prior: RectangleObjectPrior,
                 limits: TrackingLimits = TrackingLimits(), *, native_library=None) -> None:
        if (not isinstance(camera, AssumedObjectCamera)
                or not isinstance(prior, RectangleObjectPrior)
                or not isinstance(limits, TrackingLimits)):
            raise ValueError("explicit assumed camera, rectangle prior and tracking limits required")
        self._camera = camera
        self._prior = prior
        self._limits = limits
        self._closed = False
        self._native_flow = None
        if native_library is not None:
            from .native_object_tracking import NativePyramidLKFlow
            self._native_flow = NativePyramidLKFlow(native_library,
                                                   width=camera.width, height=camera.height)
        self.clear()

    @property
    def camera(self) -> AssumedObjectCamera:
        return self._camera

    @property
    def prior(self) -> RectangleObjectPrior:
        return self._prior

    @property
    def limits(self) -> TrackingLimits:
        return self._limits

    @property
    def active(self) -> bool:
        return self._points is not None and (self._gray is not None or self._native_flow is not None)

    @property
    def retained_bytes(self) -> int:
        if not self.active:
            return 0
        image_bytes = (self._gray.nbytes if self._native_flow is None
                       else self._native_flow.retained_bytes)
        return int(image_bytes + self._points.nbytes)

    def clear(self) -> None:
        self._gray = None
        self._points = None
        self._view = None
        self._frame_id = None
        self._timestamp_ns = None
        if self._native_flow is not None and not self._closed:
            self._native_flow.clear()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.clear()
        finally:
            try:
                if self._native_flow is not None:
                    self._native_flow.close()
            finally:
                self._closed = True

    def _clear_after_exception(self) -> None:
        # A library reset fault must not replace the original input/PnP/flow
        # exception. Native execution faults remain sticky until close.
        try:
            self.clear()
        except BaseException:
            pass

    def __enter__(self):
        if self._closed:
            raise RuntimeError("tracking instance is closed")
        if self._native_flow is not None:
            self._native_flow.ensure_usable()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            self.close()
        except BaseException:
            if exc_type is None:
                raise

    def _validate_frame(self, frame: CapturedFrame):
        if not isinstance(frame, CapturedFrame):
            raise ValueError("tracking input must be CapturedFrame")
        # The capture dataclass allows bool/float scalar aliases; do not inherit
        # that weak identity check at this correspondence boundary.
        for name in ("frame_id", "timestamp_ns", "sync_timestamp_ns"):
            _uint64(getattr(frame, name), name)
        ColoredMarkerFrame(frame.view, frame.frame_id, frame.timestamp_ns,
                           frame.width, frame.height, (None,) * 4)
        if (frame.width, frame.height) != (self.camera.width, self.camera.height):
            raise ValueError("tracking source dimensions differ from the assumed camera")
        return _validate_bgr_frame(frame)

    def _retain(self, frame: CapturedFrame, gray, points) -> None:
        # Copies are private and bounded, including when capture reuses its
        # BGR buffer or a test/backend owns the LK output array.
        next_points = points.copy(order="C")
        if self._native_flow is None:
            self._gray = gray.copy(order="C")
        elif self._points is None:
            self._native_flow.seed_bgr(frame.payload)
        else:
            self._native_flow.commit()
        self._points = next_points
        self._view = frame.view
        self._frame_id = frame.frame_id
        self._timestamp_ns = frame.timestamp_ns

    def _empty(self, frame: CapturedFrame, status: str, reason: str,
               *, fb=None, patch=None, displacement=None) -> TrackingStep:
        self.clear()
        observation = ColoredMarkerFrame(frame.view, frame.frame_id, frame.timestamp_ns,
            frame.width, frame.height, (None,) * 4, (reason,))
        result = RelativeObjectFrame(observation, self.camera, self.prior, "incomplete_markers")
        return TrackingStep(result, frame.sync_timestamp_ns, status, False,
                            fb, patch, displacement)

    def seed(self, frame: CapturedFrame, observation: ColoredMarkerFrame) -> TrackingStep:
        if self._closed:
            raise RuntimeError("tracking instance is closed")
        if self._native_flow is not None:
            self._native_flow.ensure_usable()
        self.clear()
        try:
            image = self._validate_frame(frame)
            if not isinstance(observation, ColoredMarkerFrame) or (
                    frame.view, frame.frame_id, frame.timestamp_ns, frame.width, frame.height) != (
                    observation.view, observation.frame_id, observation.timestamp_ns,
                    observation.width, observation.height):
                raise ValueError("tracking seed requires the exact source-frame identity")
            result = estimate_relative_object(observation, self.camera, self.prior)
            if not result.accepted:
                return TrackingStep(result, frame.sync_timestamp_ns, "seed_rejected", False)
            cv2, np = _libraries()
            points = np.array([(x * frame.width, y * frame.height)
                               for x, y in observation.points], dtype=np.float32).reshape(4, 1, 2)
            # Float32 conversion near the image limit can round to width/height.
            # Reject rather than clamp an initialization correspondence.
            if not self._inside(points, frame.width, frame.height):
                rejected = self._empty(frame, "seed_rejected", "seed_float32_outside")
                return rejected
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if self._native_flow is None else None
            self._retain(frame, gray, points)
            return TrackingStep(result, frame.sync_timestamp_ns, "seeded", False)
        except BaseException:
            self._clear_after_exception()
            raise

    @staticmethod
    def _inside(points, width: int, height: int) -> bool:
        _, np = _libraries()
        return bool(np.all(points[:, 0, 0] >= 0) and np.all(points[:, 0, 0] < width)
                    and np.all(points[:, 0, 1] >= 0) and np.all(points[:, 0, 1] < height))

    @staticmethod
    def _flow_output(output):
        """Check schema, then status, then successful-point/error values.

        OpenCV leaves failed-point err undefined. A normal zero status is loss,
        not a malformed successful result; never inspect its err values.
        """
        _, np = _libraries()
        if not isinstance(output, (tuple, list)) or len(output) != 3:
            raise RuntimeError("LK must return points, status and patch error")
        points, status, error = output
        if (not isinstance(points, np.ndarray) or points.dtype != np.float32
                or points.shape != (4, 1, 2)
                or not isinstance(status, np.ndarray) or status.dtype != np.uint8
                or status.shape != (4, 1)):
            raise RuntimeError("LK requires exact four-point float32/uint8 output shapes")
        if np.any((status != 0) & (status != 1)):
            raise RuntimeError("LK returned a malformed status value")
        if not np.all(status == 1):
            return None
        if (not isinstance(error, np.ndarray) or error.dtype != np.float32
                or error.shape != (4, 1) or not np.all(np.isfinite(points))
                or not np.all(np.isfinite(error)) or np.any(error < 0)):
            raise RuntimeError("successful LK output must be finite with nonnegative float32 errors")
        return points, float(np.max(error))

    def update(self, frame: CapturedFrame) -> TrackingStep:
        if self._closed:
            raise RuntimeError("tracking instance is closed")
        if self._native_flow is not None:
            self._native_flow.ensure_usable()
        try:
            image = self._validate_frame(frame)
            if not self.active:
                return self._empty(frame, "needs_seed", "needs_seed")
            if frame.view != self._view:
                raise ValueError("tracking source view changed")
            if frame.frame_id <= self._frame_id or frame.timestamp_ns <= self._timestamp_ns:
                raise ValueError("tracking frame ID and raw timestamp must strictly increase")
            if frame.timestamp_ns - self._timestamp_ns > self.limits.maximum_gap_ns:
                return self._empty(frame, "lost", "raw_timestamp_gap")
            if frame.frame_id - self._frame_id > self.limits.max_frame_gap:
                return self._empty(frame, "lost", "frame_id_gap")
            cv2, np = _libraries()
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if self._native_flow is None else None
            options = {"winSize": (21, 21), "maxLevel": 3,
                       "criteria": (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
                       "flags": 0}
            forward_output = (cv2.calcOpticalFlowPyrLK(
                self._gray, gray, self._points.copy(), None, **options)
                if self._native_flow is None else self._native_flow.forward_bgr(image, self._points))
            forward = self._flow_output(forward_output)
            if forward is None:
                return self._empty(frame, "lost", "forward_status")
            points, forward_patch = forward
            if not self._inside(points, frame.width, frame.height):
                return self._empty(frame, "lost", "forward_outside", patch=forward_patch)
            displacement = float(np.max(np.linalg.norm(
                points.astype(np.float64) - self._points.astype(np.float64), axis=2)))
            if forward_patch > self.limits.maximum_patch_error:
                return self._empty(frame, "lost", "forward_patch_error", patch=forward_patch,
                                   displacement=displacement)
            if displacement > self.limits.maximum_displacement_px:
                return self._empty(frame, "lost", "displacement", patch=forward_patch,
                                   displacement=displacement)
            backward_output = (cv2.calcOpticalFlowPyrLK(
                gray, self._gray, points.copy(), None, **options)
                if self._native_flow is None else self._native_flow.backward(points))
            backward = self._flow_output(backward_output)
            if backward is None:
                return self._empty(frame, "lost", "backward_status",
                                   displacement=displacement)
            returned, backward_patch = backward
            patch = max(forward_patch, backward_patch)
            fb = float(np.max(np.linalg.norm(
                returned.astype(np.float64) - self._points.astype(np.float64), axis=2)))
            diagnostics = {"fb": fb, "patch": patch, "displacement": displacement}
            if not self._inside(returned, frame.width, frame.height):
                return self._empty(frame, "lost", "backward_outside", **diagnostics)
            if patch > self.limits.maximum_patch_error:
                return self._empty(frame, "lost", "backward_patch_error", **diagnostics)
            if fb > self.limits.maximum_fb_error_px:
                return self._empty(frame, "lost", "forward_backward_error", **diagnostics)
            pixel_points = points[:, 0].astype(np.float64)
            edges = np.roll(pixel_points, -1, axis=0) - pixel_points
            following = np.roll(edges, -1, axis=0)
            cross = edges[:, 0] * following[:, 1] - edges[:, 1] * following[:, 0]
            if np.any(cross <= 0):
                return self._empty(frame, "lost", "nonconvex_or_mirrored_correspondences", **diagnostics)
            observation = ColoredMarkerFrame(frame.view, frame.frame_id, frame.timestamp_ns,
                frame.width, frame.height,
                tuple((float(p[0]) / frame.width, float(p[1]) / frame.height) for p in pixel_points))
            result = estimate_relative_object(observation, self.camera, self.prior)
            if not result.accepted:
                self.clear()
                return TrackingStep(result, frame.sync_timestamp_ns, "model_rejected", True,
                                    fb, patch, displacement)
            self._retain(frame, gray, points)
            return TrackingStep(result, frame.sync_timestamp_ns, "tracked", True,
                                fb, patch, displacement)
        except BaseException:
            self._clear_after_exception()
            raise
