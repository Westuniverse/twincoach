"""Opt-in CPU reference for the official RTMPose body7 COCO17 ONNX exports.

No runtime imports, model downloads, or production PoseFrame conversion occur on
module import. Raw SimCC responses are not calibrated keypoint probabilities.
Model references and usage scope are documented in report/third_party.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import floor, isfinite
from numbers import Real
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .capture import CapturedFrame
from .contracts import CameraView
from .pose_adapter import MOVENET_COCO_KEYPOINTS


class RTMPoseReferenceError(ValueError):
    """An optional dependency, model, or frame violates the reference contract."""


def round_detector_bbox_to_source_pixels(
    bbox: Any, source_width: int, source_height: int,
) -> tuple[int, int, int, int]:
    """Opt-in nearest-pixel crop geometry; detector/association boxes stay native.

    Round nonnegative source-pixel xyxy endpoints with half-pixel ties upward.
    Coordinates move by at most half a pixel. This can stabilize crops under
    tiny detector variations away from rounding boundaries, not at every input.
    Invalid, outside or collapsed boxes raise; no clipping or fallback occurs.
    prepare/infer never apply this policy automatically. No runtime is imported.
    """
    if (type(source_width) is not int or type(source_height) is not int
            or source_width < 1 or source_height < 1):
        raise RTMPoseReferenceError("source dimensions must be positive integers")
    try:
        values = tuple(bbox)
    except TypeError as exc:
        raise RTMPoseReferenceError("bbox must contain four finite real coordinates") from exc
    if len(values) != 4 or any(isinstance(v, bool) or not isinstance(v, Real) for v in values):
        raise RTMPoseReferenceError("bbox must contain four finite real coordinates")
    try:
        x0, y0, x1, y1 = (float(v) for v in values)
    except (OverflowError, ValueError) as exc:
        raise RTMPoseReferenceError("bbox must contain four finite real coordinates") from exc
    if not all(isfinite(v) for v in (x0, y0, x1, y1)):
        raise RTMPoseReferenceError("bbox must contain four finite real coordinates")
    if not (0 <= x0 < x1 <= source_width and 0 <= y0 < y1 <= source_height):
        raise RTMPoseReferenceError("bbox must have positive area within the source")
    # Avoid addition rounding a value immediately below a half-pixel upward.
    rounded = tuple(floor(v) + int(v - floor(v) >= .5) for v in (x0, y0, x1, y1))
    if rounded[0] >= rounded[2] or rounded[1] >= rounded[3]:
        raise RTMPoseReferenceError("rounded bbox has no positive pixel area")
    return rounded


@dataclass(frozen=True, slots=True)
class RawRTMKeypoint:
    x: float | None
    y: float | None
    raw_response: float
    inside_source: bool

    def __post_init__(self) -> None:
        if (type(self.raw_response) not in (int, float)
                or not isfinite(self.raw_response)):
            raise RTMPoseReferenceError("raw response must be finite")
        if (self.x is None) != (self.y is None):
            raise RTMPoseReferenceError("coordinates must both be present or absent")
        if self.x is not None and (
            type(self.x) not in (int, float) or type(self.y) not in (int, float)
            or not isfinite(self.x) or not isfinite(self.y)
        ):
            raise RTMPoseReferenceError("coordinates must be finite")
        if type(self.inside_source) is not bool:
            raise RTMPoseReferenceError("inside_source must be boolean")
        if self.inside_source and (
            self.x is None or not (0 <= self.x < 1 and 0 <= self.y < 1)
        ):
            raise RTMPoseReferenceError("inside_source contradicts coordinates")


@dataclass(frozen=True, slots=True)
class RawRTMPoseFrame:
    view: CameraView
    frame_id: int
    timestamp_ns: int
    source_width: int
    source_height: int
    keypoints: Mapping[str, RawRTMKeypoint]

    def __post_init__(self) -> None:
        if self.frame_id < 0 or self.timestamp_ns < 0:
            raise RTMPoseReferenceError("negative frame identity")
        if self.source_width < 1 or self.source_height < 1:
            raise RTMPoseReferenceError("nonpositive source dimensions")
        if set(self.keypoints) != set(MOVENET_COCO_KEYPOINTS):
            raise RTMPoseReferenceError("raw frame requires named COCO17 points")
        object.__setattr__(self, "keypoints", MappingProxyType(dict(self.keypoints)))

    def to_record(self) -> dict[str, object]:
        return {
            "schema": "rtmpose_coco17_raw_v1",
            "coordinate_system": "normalized_image_xy_unclipped",
            "response_definition": "min(max_simcc_x,max_simcc_y)_uncalibrated",
            "view": self.view.value,
            "frame_id": self.frame_id,
            "timestamp_ns": self.timestamp_ns,
            "source_width": self.source_width,
            "source_height": self.source_height,
            "keypoints": {
                name: {
                    "x": point.x, "y": point.y,
                    "raw_response": point.raw_response,
                    "inside_source": point.inside_source,
                }
                for name, point in self.keypoints.items()
            },
        }


class RTMPoseONNXReference:
    """Single-person, full-frame or explicit xyxy ROI, batch1 192x256 RGB.

    An injected ONNX-compatible session allows deterministic contract tests.
    NumPy and OpenCV are imported only when constructing this optional reference.
    The session must have input NCHW float32 and named SimCC384/512 outputs.
    """

    def __init__(
        self, session: Any, *, numpy_module: Any | None = None,
        cv2_module: Any | None = None,
    ) -> None:
        try:
            if numpy_module is None:
                import numpy as numpy_module
            if cv2_module is None:
                import cv2 as cv2_module
        except ImportError as exc:
            raise RTMPoseReferenceError("RTMPose reference needs NumPy and OpenCV") from exc
        self.np, self.cv2, self.session = numpy_module, cv2_module, session
        inputs, outputs = session.get_inputs(), session.get_outputs()
        if len(inputs) != 1:
            raise RTMPoseReferenceError("expected one model input")
        inp = inputs[0]
        shape = tuple(inp.shape)
        if (len(shape) != 4 or shape[1:] != (3, 256, 192)
                or (isinstance(shape[0], int) and shape[0] != 1)
                or inp.type != "tensor(float)"):
            raise RTMPoseReferenceError("expected batch1/dynamic NCHW float32 256x192")
        if [o.name for o in outputs] != ["simcc_x", "simcc_y"]:
            raise RTMPoseReferenceError("expected named simcc_x/simcc_y outputs")
        self.input_name = inp.name

    def prepare(self, image: Any, bbox: Any = None) -> tuple[Any, Any, Any]:
        np = self.np
        if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
                or image.ndim != 3 or image.shape[2] != 3
                or min(image.shape[:2]) < 1):
            raise RTMPoseReferenceError("expected nonempty uint8 BGR HWC3")
        h, w = image.shape[:2]
        box = np.asarray([0, 0, w, h] if bbox is None else bbox, dtype=float)
        if (box.shape != (4,) or not np.isfinite(box).all()
                or np.any(box[2:] <= box[:2])):
            raise RTMPoseReferenceError("bbox must be finite positive-area xyxy")
        if np.any(box[:2] < 0) or box[2] > w or box[3] > h:
            raise RTMPoseReferenceError("bbox outside source")
        center = (box[:2] + box[2:]) / 2
        padded = (box[2:] - box[:2]) * 1.25
        scale = np.array([
            max(padded[0], padded[1] * .75),
            max(padded[1], padded[0] / .75),
        ])
        factor = np.array([192., 256.]) / scale
        affine = np.array([
            [factor[0], 0, 96 - center[0] * factor[0]],
            [0, factor[1], 128 - center[1] * factor[1]],
        ])
        warped = self.cv2.warpAffine(
            image, affine, (192, 256), flags=self.cv2.INTER_LINEAR,
        )
        # Archive pipeline.json: Normalize.to_rgb=true. Do not copy RTMLib's
        # current no-channel-swap variant when the source is OpenCV BGR.
        mean = np.array([123.675, 116.28, 103.53])
        std = np.array([58.395, 57.12, 57.375])
        tensor = np.ascontiguousarray(
            ((warped[:, :, ::-1] - mean) / std).transpose(2, 0, 1)[None],
            dtype=np.float32,
        )
        return tensor, center, scale

    def decode(self, x: Any, y: Any, center: Any, scale: Any) -> tuple[Any, Any]:
        np = self.np
        if (not isinstance(x, np.ndarray) or not isinstance(y, np.ndarray)
                or x.shape != (1, 17, 384) or y.shape != (1, 17, 512)):
            raise RTMPoseReferenceError("expected batch1 COCO17 SimCC384/512")
        if (not np.issubdtype(x.dtype, np.floating)
                or not np.issubdtype(y.dtype, np.floating)
                or not np.isfinite(x).all() or not np.isfinite(y).all()):
            raise RTMPoseReferenceError("nonfinite or nonfloating SimCC")
        center, scale = np.asarray(center, dtype=float), np.asarray(scale, dtype=float)
        if (center.shape != (2,) or scale.shape != (2,)
                or not np.isfinite(center).all() or not np.isfinite(scale).all()
                or np.any(scale <= 0)):
            raise RTMPoseReferenceError("invalid inverse geometry")
        loc = np.stack([x.argmax(-1), y.argmax(-1)], axis=-1)[0].astype(float)
        # Tagged MMPose1.1.0/MMDeploy1.2.0 use min, not current RTMLib's mean.
        raw = np.minimum(x.max(-1), y.max(-1))[0]
        xy = loc / 2 / np.array([192., 256.]) * scale + center - scale / 2
        xy[raw <= 0] = np.nan
        return xy, raw

    def infer(self, frame: CapturedFrame[Any], *, bbox: Any = None) -> RawRTMPoseFrame:
        if frame.pixel_format != "bgr8":
            raise RTMPoseReferenceError("reference expects bgr8 capture payload")
        tensor, center, scale = self.prepare(frame.payload, bbox)
        if frame.payload.shape[:2] != (frame.height, frame.width):
            raise RTMPoseReferenceError("capture dimensions disagree with pixels")
        x, y = self.session.run(["simcc_x", "simcc_y"], {self.input_name: tensor})
        xy, raw = self.decode(x, y, center, scale)
        points = {}
        for name, pair, response in zip(MOVENET_COCO_KEYPOINTS, xy, raw):
            px, py = pair
            finite = bool(self.np.isfinite(pair).all())
            inside = finite and 0 <= px < frame.width and 0 <= py < frame.height
            points[name] = RawRTMKeypoint(
                float(px / frame.width) if finite else None,
                float(py / frame.height) if finite else None,
                float(response), bool(inside),
            )
        return RawRTMPoseFrame(
            frame.view, frame.frame_id, int(frame.sync_timestamp_ns),
            frame.width, frame.height, points,
        )


def load_rtmpose_onnx_reference(
    model_path: str | Path, *, num_threads: int = 1, allow_spinning: bool = True,
    worker_cpu_affinity: tuple[int, ...] | None = None,
) -> RTMPoseONNXReference:
    """Load an existing local artifact on CPU; never downloads any asset.

    num_threads controls intra-op parallelism; inter-op stays at one thread.
    allow_spinning=True preserves runtime defaults. False disables intra/inter
    wait spinning explicitly, useful when several CPU sessions are resident.
    worker_cpu_affinity optionally limits every intra-op worker to the same
    nonempty group of zero-based Linux CPU IDs. The calling thread is unchanged.
    None preserves OS scheduling; an explicit group requires num_threads >= 2
    and CPUs allowed for the caller, in ORT's static mask range 0..1023.
    This is a host experiment, not a default recommendation. Runtime application
    must be verified on the target system.
    """
    path = Path(model_path)
    if not path.is_file():
        raise RTMPoseReferenceError(f"model file missing: {path}")
    if type(num_threads) is not int or num_threads < 1:
        raise RTMPoseReferenceError("num_threads must be a positive integer")
    if type(allow_spinning) is not bool:
        raise RTMPoseReferenceError("allow_spinning must be boolean")
    affinity_entry = None
    if worker_cpu_affinity is not None:
        import os
        import sys

        if (type(worker_cpu_affinity) is not tuple or not worker_cpu_affinity
                or any(type(cpu) is not int or not 0 <= cpu < 1024
                       for cpu in worker_cpu_affinity)
                or len(set(worker_cpu_affinity)) != len(worker_cpu_affinity)):
            raise RTMPoseReferenceError(
                "worker_cpu_affinity must be a nonempty tuple of unique CPU integers in 0..1023"
            )
        if num_threads < 2:
            raise RTMPoseReferenceError("worker_cpu_affinity requires at least two threads")
        if sys.platform != "linux" or not hasattr(os, "sched_getaffinity"):
            raise RTMPoseReferenceError("worker_cpu_affinity requires Linux affinity support")
        try:
            allowed = os.sched_getaffinity(0)
        except OSError as exc:
            raise RTMPoseReferenceError("cannot read allowed CPU affinity") from exc
        if not set(worker_cpu_affinity) <= allowed:
            raise RTMPoseReferenceError(
                "worker_cpu_affinity contains CPUs unavailable to the caller"
            )
        # ORT's config is one-based, with one group per worker, excluding the
        # calling thread. See v1.19.2 core/util/thread_utils.cc.
        group = ",".join(str(cpu + 1) for cpu in sorted(worker_cpu_affinity))
        if (len(group) + 1) * (num_threads - 1) - 1 > 1024:
            raise RTMPoseReferenceError("worker_cpu_affinity exceeds ORT config length")
        affinity_entry = ";".join([group] * (num_threads - 1))
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise RTMPoseReferenceError("optional ONNX Runtime is unavailable") from exc
    options = ort.SessionOptions()
    options.intra_op_num_threads = num_threads
    options.inter_op_num_threads = 1
    if not allow_spinning:
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    if affinity_entry is not None:
        options.add_session_config_entry("session.intra_op_thread_affinities", affinity_entry)
    session = ort.InferenceSession(
        str(path), options, providers=["CPUExecutionProvider"],
    )
    return RTMPoseONNXReference(session)
