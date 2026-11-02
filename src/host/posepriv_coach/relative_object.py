# SPDX-License-Identifier: MIT
"""Opt-in marked-object demonstration; assumed camera, never metric or stereo.

No cameras, DPU, DMA, scoring, image files or windows are opened here. Four
color IDs are correspondences to a known plane, not arbitrary object recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from .capture import CapturedFrame
from .contracts import CameraView
from .avatar_view import OrbitCamera, PreparedOrbitProjection

MARKER_IDS = ("red", "green", "blue", "yellow")
MARKER_BGR = ((0, 0, 255), (0, 255, 0), (255, 0, 0), (0, 255, 255))
BOX_EDGES = ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7),
             (7, 4), (0, 4), (1, 5), (2, 6), (3, 7))


def _libraries():
    import cv2
    import numpy as np
    return cv2, np


def _dimension(value: int, maximum: int, name: str) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [1,{maximum}]")


@dataclass(frozen=True, slots=True)
class AssumedObjectCamera:
    width: int
    height: int
    focal_width_ratio: float  # Required explicit assumption, not a calibration.

    def __post_init__(self) -> None:
        _dimension(self.width, 4096, "width")
        _dimension(self.height, 2160, "height")
        if not math.isfinite(self.focal_width_ratio) or not 0 < self.focal_width_ratio <= 8:
            raise ValueError("assumed focal/width ratio must be finite in (0,8]")

    def matrix(self):
        _, np = _libraries()
        f = self.focal_width_ratio * self.width
        return np.array(((f, 0, self.width / 2), (0, f, self.height / 2), (0, 0, 1)),
                        dtype=np.float64)

    def assumptions(self) -> dict[str, Any]:
        return {"camera_parameters": "assumed_not_calibrated", "metric_geometry": False,
                "method": "single_view_planar_PnP_not_stereo", "units": "marker_width_units",
                "source_width": self.width, "source_height": self.height,
                "focal_width_ratio": self.focal_width_ratio,
                "square_pixels": "assumed", "principal_point": "assumed_image_center",
                "distortion": "assumed_zero", "DPU_executed": False, "PL_executed": False}


@dataclass(frozen=True, slots=True)
class RectangleObjectPrior:
    height_ratio: float = 1.0
    model_depth_ratio: float = 0.25

    def __post_init__(self) -> None:
        if not math.isfinite(self.height_ratio) or not 0.1 <= self.height_ratio <= 10:
            raise ValueError("marker height/width prior must be within [0.1,10]")
        if not math.isfinite(self.model_depth_ratio) or not 0 <= self.model_depth_ratio <= 10:
            raise ValueError("model depth/width prior must be within [0,10]")

    def vertices(self) -> tuple[tuple[float, float, float], ...]:
        # Local X right, Y down; the observed face is z=0. Thickness is a prior.
        h = self.height_ratio / 2
        face = ((-0.5, -h, 0.0), (0.5, -h, 0.0), (0.5, h, 0.0), (-0.5, h, 0.0))
        return face + tuple((x, y, self.model_depth_ratio) for x, y, _ in face)


@dataclass(frozen=True, slots=True)
class ColoredMarkerFrame:
    view: CameraView
    frame_id: int
    timestamp_ns: int
    width: int
    height: int
    points: tuple[tuple[float, float] | None, ...]  # Source-normalized, fixed color IDs.
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.view, CameraView):
            raise ValueError("marker view must be CameraView")
        for name, value in (("frame_id", self.frame_id), ("timestamp_ns", self.timestamp_ns)):
            if type(value) is not int or not 0 <= value < 2**64:
                raise ValueError(f"invalid marker {name}")
        _dimension(self.width, 4096, "width")
        _dimension(self.height, 2160, "height")
        if type(self.points) is not tuple or len(self.points) != 4:
            raise ValueError("exactly four immutable color correspondences required")
        if type(self.problems) is not tuple or any(type(p) is not str for p in self.problems):
            raise ValueError("immutable diagnostic strings required")
        for point in self.points:
            if point is not None and (type(point) is not tuple or len(point) != 2
                    or not all(math.isfinite(v) and 0 <= v < 1 for v in point)):
                raise ValueError("marker coordinates must be finite source-normalized tuples")


def _validate_bgr_frame(frame: CapturedFrame):
    _, np = _libraries()
    _dimension(frame.width, 4096, "width")
    _dimension(frame.height, 2160, "height")
    image = frame.payload
    if frame.pixel_format != "bgr8" or type(image) is not np.ndarray or (
            image.dtype != np.uint8 or image.shape != (frame.height, frame.width, 3)):
        raise ValueError("object input requires same-frame BGR8 and exact source dimensions")
    _prove_bgr_storage(image, np)
    return image


def _prove_bgr_storage(image, np) -> None:
    """Prove the signed-stride read envelope before any pixel copy/C call.

    This is not a snapshot or a concurrency guard. The caller must keep the
    allocation alive/stable during the synchronous operation. Foreign owners
    and stride-trick proxies are rejected rather than trusting logical nbytes.
    Safe negative/zero strides remain valid and retain their image semantics.
    """
    import ctypes

    pointer_max = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1
    low_offset = high_offset = 0
    for extent, stride in zip(image.shape, image.strides, strict=True):
        offset = (extent - 1) * stride
        low_offset += min(0, offset)
        high_offset += max(0, offset)
    address = int(image.ctypes.data)
    first = address + low_offset
    after_last = address + high_offset + image.dtype.itemsize
    if not 0 < first < after_last <= pointer_max + 1:
        raise ValueError("BGR storage has an invalid or overflowing address span")

    owner = image
    while isinstance(owner.base, np.ndarray):
        owner = owner.base
        if type(owner) is not np.ndarray:
            raise ValueError("BGR storage has an unproven subclass owner")
    if owner.flags.owndata:
        if not (owner.flags.c_contiguous or owner.flags.f_contiguous):
            raise ValueError("BGR storage requires a provably contiguous root allocation")
        allocation, capacity = int(owner.ctypes.data), int(owner.nbytes)
    elif type(owner.base) is bytes and not owner.flags.writeable and not image.flags.writeable:
        # A local callable avoids changing global ctypes.pythonapi signatures.
        bytes_address = ctypes.PYFUNCTYPE(ctypes.c_void_p, ctypes.py_object)(
            ("PyBytes_AsString", ctypes.pythonapi))
        allocation, capacity = int(bytes_address(owner.base)), len(owner.base)
    else:
        raise ValueError("BGR storage requires a NumPy-owned or readonly bytes allocation")
    if not 0 < allocation <= pointer_max or capacity < 1 or capacity > pointer_max + 1 - allocation:
        raise ValueError("BGR owner has an invalid or overflowing capacity")
    if first < allocation or after_last > allocation + capacity:
        raise ValueError("BGR address span exceeds the proven owner allocation")


def detect_rectangle_markers(frame: CapturedFrame, *, minimum_area: int = 12) -> ColoredMarkerFrame:
    """One compact component per fixed HSV color; no guessed corner ordering."""
    _dimension(minimum_area, 65536, "minimum_area")
    image = _validate_bgr_frame(frame)
    cv2, np = _libraries()
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    bands = (((0, 10), (170, 179)), ((45, 85),), ((100, 130),), ((20, 35),))
    points, problems = [], []
    for name, color_bands in zip(MARKER_IDS, bands, strict=True):
        mask = np.zeros((frame.height, frame.width), dtype=np.uint8)
        for low, high in color_bands:
            mask |= cv2.inRange(hsv, (low, 100, 80), (high, 255, 255))
        count, _, stats, centers = cv2.connectedComponentsWithStats(mask, connectivity=8)
        eligible = [i for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] >= minimum_area]
        point = None
        if len(eligible) != 1:
            problems.append(f"{name}:missing" if not eligible else f"{name}:multiple")
        else:
            i = eligible[0]
            x, y, w, h, area = (int(v) for v in stats[i])
            if x == 0 or y == 0 or x + w == frame.width or y + h == frame.height:
                problems.append(f"{name}:clipped")
            elif (area > 0.05 * frame.width * frame.height or area / (w * h) < 0.25
                  or not 0.5 <= w / h <= 2):
                problems.append(f"{name}:not_compact_marker")
            else:
                point = (float(centers[i, 0]) / frame.width, float(centers[i, 1]) / frame.height)
        points.append(point)
    return ColoredMarkerFrame(frame.view, frame.frame_id, frame.timestamp_ns, frame.width,
                              frame.height, tuple(points), tuple(problems))


@dataclass(frozen=True, slots=True)
class RelativeObjectFrame:
    observation: ColoredMarkerFrame
    camera: AssumedObjectCamera
    prior: RectangleObjectPrior
    status: str
    rotation: tuple[float, ...] = ()  # Row-major object-to-camera.
    translation: tuple[float, ...] = ()
    vertices_camera: tuple[tuple[float, float, float], ...] = ()
    reprojection_rms_px: float | None = None
    reprojection_max_px: float | None = None
    candidate_count: int = 0

    def __post_init__(self) -> None:
        if (self.observation.width, self.observation.height) != (self.camera.width, self.camera.height):
            raise ValueError("relative object camera/observation dimensions differ")
        if type(self.candidate_count) is not int or not 0 <= self.candidate_count <= 64:
            raise ValueError("invalid planar candidate count")
        for residual in (self.reprojection_rms_px, self.reprojection_max_px):
            if residual is not None and (not math.isfinite(residual) or residual < 0):
                raise ValueError("invalid reprojection diagnostic")
        if self.accepted:
            if (type(self.rotation) is not tuple or len(self.rotation) != 9
                    or type(self.translation) is not tuple or len(self.translation) != 3
                    or type(self.vertices_camera) is not tuple or len(self.vertices_camera) != 8
                    or not all(math.isfinite(v) for v in self.rotation + self.translation)
                    or any(type(p) is not tuple or len(p) != 3 or not all(math.isfinite(v) for v in p)
                           or p[2] <= 0 for p in self.vertices_camera)):
                raise ValueError("accepted relative pose requires immutable finite positive-depth geometry")
        elif self.rotation or self.translation or self.vertices_camera:
            raise ValueError("rejected relative frames must not carry held geometry")

    @property
    def accepted(self) -> bool:
        return self.status == "accepted_under_assumptions"

    def summary(self) -> dict[str, Any]:
        # Diagnostics only: no image, point coordinates, matrix or translation.
        return {**self.camera.assumptions(), "status": self.status, "accepted": self.accepted,
                "detected_markers": sum(p is not None for p in self.observation.points),
                "candidate_count": self.candidate_count,
                "reprojection_rms_px": self.reprojection_rms_px,
                "reprojection_max_px": self.reprojection_max_px,
                "marker_height_ratio": self.prior.height_ratio,
                "model_depth_ratio": self.prior.model_depth_ratio, "model_depth_is_prior": True}


def estimate_relative_object(observation: ColoredMarkerFrame, camera: AssumedObjectCamera,
        prior: RectangleObjectPrior, *, maximum_reprojection_px: float = 2.0,
        ambiguity_gap_px: float = 0.25) -> RelativeObjectFrame:
    """Reject unusable/ambiguous frames; never return a held previous pose."""
    if (observation.width, observation.height) != (camera.width, camera.height):
        raise ValueError("assumed camera dimensions must match the observation")
    if (not math.isfinite(maximum_reprojection_px) or maximum_reprojection_px <= 0
            or not math.isfinite(ambiguity_gap_px) or ambiguity_gap_px < 0):
        raise ValueError("relative pose reprojection limits must be finite and valid")

    def rejected(status, **kwargs):
        return RelativeObjectFrame(observation, camera, prior, status, **kwargs)

    if observation.problems or any(p is None for p in observation.points):
        return rejected("incomplete_markers")
    cv2, np = _libraries()
    pixels = np.array([(p[0] * camera.width, p[1] * camera.height) for p in observation.points],
                      dtype=np.float64)
    edges = np.roll(pixels, -1, axis=0) - pixels
    lengths = np.linalg.norm(edges, axis=1)
    following = np.roll(edges, -1, axis=0)
    cross = edges[:, 0] * following[:, 1] - edges[:, 1] * following[:, 0]
    if np.any(cross <= 0):
        return rejected("nonconvex_or_mirrored_ids")
    area = 0.5 * float(np.sum(pixels[:, 0] * np.roll(pixels[:, 1], -1)
                             - pixels[:, 1] * np.roll(pixels[:, 0], -1)))
    if np.any(lengths < 8) or area < 64 or np.any(cross / (lengths * np.roll(lengths, -1)) < 0.02):
        return rejected("degenerate_marker_geometry")
    model = np.array(prior.vertices(), dtype=np.float64)
    matrix = camera.matrix()
    try:
        count, rotations, translations, _ = cv2.solvePnPGeneric(
            model[:4], pixels, matrix, None, flags=cv2.SOLVEPNP_IPPE)
    except cv2.error:
        return rejected("planar_solver_failed")
    if count != len(rotations) or count != len(translations):
        raise RuntimeError("planar solver returned inconsistent candidate counts")
    candidates = []
    for rvec, tvec in zip(rotations, translations, strict=True):
        if not np.all(np.isfinite(rvec)) or not np.all(np.isfinite(tvec)):
            continue
        rotation, _ = cv2.Rodrigues(rvec)
        translation = np.asarray(tvec, dtype=np.float64).reshape(3)
        vertices = model @ rotation.T + translation
        if not np.all(np.isfinite(vertices)) or np.any(vertices[:, 2] <= 1e-6):
            continue
        # Independently reproject; do not trust the solver's reported residual.
        projected = vertices[:4] @ matrix.T
        projected = projected[:, :2] / projected[:, 2:3]
        error = np.linalg.norm(projected - pixels, axis=1)
        rms, maximum = float(np.sqrt(np.mean(error**2))), float(np.max(error))
        candidates.append((rms, maximum, rotation, translation, vertices))
    if not candidates:
        return rejected("no_positive_finite_pose", candidate_count=int(count))
    candidates.sort(key=lambda item: item[0])
    best = candidates[0]
    diagnostic = {"reprojection_rms_px": best[0], "reprojection_max_px": best[1],
                  "candidate_count": int(count)}
    if best[1] > maximum_reprojection_px:
        return rejected("reprojection_rejected", **diagnostic)
    if any(other[0] - best[0] <= ambiguity_gap_px
           and float(np.max(np.linalg.norm(other[4] - best[4], axis=1))) > 0.02
           for other in candidates[1:]):
        return rejected("ambiguous_pose", **diagnostic)
    return RelativeObjectFrame(observation, camera, prior, "accepted_under_assumptions",
        rotation=tuple(float(v) for v in best[2].ravel()),
        translation=tuple(float(v) for v in best[3]),
        vertices_camera=tuple(tuple(float(v) for v in point) for point in best[4]), **diagnostic)


def _validate_relative_object_display(frame: CapturedFrame, result: RelativeObjectFrame):
    observation = result.observation
    if (frame.view, frame.frame_id, frame.timestamp_ns, frame.width, frame.height) != (
            observation.view, observation.frame_id, observation.timestamp_ns,
            observation.width, observation.height):
        raise ValueError("relative object display requires the exact source frame")
    return _validate_bgr_frame(frame)


def _draw_relative_object_model(roi, result, cv2, np) -> None:
    if not result.accepted:
        return
    vertices = np.array(result.vertices_camera, dtype=np.float64)
    vertices -= np.mean(vertices, axis=0)
    vertices[:, 1] *= -1  # Camera +Y down -> display +Y up, not a source geometry edit.
    radius = float(np.max(np.linalg.norm(vertices, axis=1)))
    projection = PreparedOrbitProjection.from_camera(OrbitCamera(yaw_degrees=-25, pitch_degrees=15,
        zoom=0.8 / max(radius, 1e-9)), width=roi.shape[1], height=roi.shape[0])
    projected = [projection.project(tuple(point))[:2] for point in vertices]
    for first, second in BOX_EDGES:
        cv2.line(roi, projected[first], projected[second], (100, 220, 180), 2, cv2.LINE_8)


def render_relative_object_model_panel(frame: CapturedFrame, result: RelativeObjectFrame, *,
                                       width: int = 376, height: int = 472):
    """Private model-only BGR region, with the same exact-source/projection contract.

    Width/height describe this region, not a full comparison canvas. No source
    image is resized, no labels are drawn, and no pixels or geometry are cached.
    The source storage is still proven before any drawing call, even though the
    model-only path does not read its pixels. Exceptions never publish a panel.
    """
    _validate_relative_object_display(frame, result)
    _dimension(width, 4096, "model panel width")
    _dimension(height, 2160, "model panel height")
    cv2, np = _libraries()
    panel = np.empty((height, width, 3), dtype=np.uint8)
    cv2.rectangle(panel, (0, 0), (width - 1, height - 1), (18, 26, 40), cv2.FILLED, cv2.LINE_8)
    _draw_relative_object_model(panel, result, cv2, np)
    return panel


def render_relative_object(frame: CapturedFrame, result: RelativeObjectFrame, *,
                           width: int = 800, height: int = 600):
    """Private comparison canvas; image/points/model all have the same identity."""
    image = _validate_relative_object_display(frame, result)
    observation = result.observation
    if type(width) is not int or type(height) is not int or not 640 <= width <= 4096 or not 360 <= height <= 2160:
        raise ValueError("object display canvas must be within 640x360 and 4096x2160")
    cv2, np = _libraries()
    canvas = np.full((height, width, 3), (18, 26, 40), dtype=np.uint8)
    panel_width, panel_height = width // 2 - 24, height - 128
    left_x, top, right_x = 16, 64, width // 2 + 8
    scale = min(panel_width / frame.width, panel_height / frame.height)
    tw, th = max(1, round(frame.width * scale)), max(1, round(frame.height * scale))
    x, y = left_x + (panel_width - tw) // 2, top + (panel_height - th) // 2
    target = canvas[y:y + th, x:x + tw]
    target[:] = cv2.resize(image, (tw, th))
    for point, color in zip(observation.points, MARKER_BGR, strict=True):
        if point is not None:
            center = (min(tw - 1, round(point[0] * tw)), min(th - 1, round(point[1] * th)))
            cv2.circle(target, center, 4, color, 1, cv2.LINE_8)
    _draw_relative_object_model(canvas[top:top + panel_height, right_x:right_x + panel_width],
                                result, cv2, np)
    cv2.putText(canvas, "SOURCE + COLOR IDS", (16, 32), cv2.FONT_HERSHEY_SIMPLEX, .6, (230, 230, 230), 1)
    cv2.putText(canvas, "RELATIVE MODEL / ASSUMED CAMERA", (right_x, 32),
                cv2.FONT_HERSHEY_SIMPLEX, .45, (230, 230, 230), 1)
    cv2.putText(canvas, result.status, (16, height - 34), cv2.FONT_HERSHEY_SIMPLEX,
                .55, (0, 210, 255), 1)
    cv2.putText(canvas, "NOT METRIC / NOT STEREO / THICKNESS IS A PRIOR", (16, height - 12),
                cv2.FONT_HERSHEY_SIMPLEX, .45, (0, 210, 255), 1)
    return canvas
