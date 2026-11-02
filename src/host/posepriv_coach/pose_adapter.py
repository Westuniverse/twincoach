"""Pose-model output adapters that preserve capture identity and time."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .capture import CapturedFrame
from .contracts import Keypoint, PoseFrame


MOVENET_COCO_KEYPOINTS = (
    "nose",
    "left_eye",
    "right_eye",
    "left_ear",
    "right_ear",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
)


class MoveNetOutputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResizeWithPadTransform:
    """Map normalized points between a source frame and a padded model input."""

    source_width: int
    source_height: int
    target_width: int = 192
    target_height: int = 192

    def __post_init__(self) -> None:
        if min(
            self.source_width,
            self.source_height,
            self.target_width,
            self.target_height,
        ) <= 0:
            raise ValueError("resize-with-pad dimensions must be positive")

    @property
    def scale(self) -> float:
        return min(
            self.target_width / self.source_width,
            self.target_height / self.source_height,
        )

    @property
    def resized_width(self) -> int:
        return max(1, min(self.target_width, round(self.source_width * self.scale)))

    @property
    def resized_height(self) -> int:
        return max(1, min(self.target_height, round(self.source_height * self.scale)))

    @property
    def pad_left(self) -> int:
        return (self.target_width - self.resized_width) // 2

    @property
    def pad_right(self) -> int:
        return self.target_width - self.resized_width - self.pad_left

    @property
    def pad_top(self) -> int:
        return (self.target_height - self.resized_height) // 2

    @property
    def pad_bottom(self) -> int:
        return self.target_height - self.resized_height - self.pad_top

    def source_to_model(self, x: float, y: float) -> tuple[float, float]:
        """Convert source-frame normalized coordinates to model coordinates."""

        return (
            (x * self.resized_width + self.pad_left) / self.target_width,
            (y * self.resized_height + self.pad_top) / self.target_height,
        )

    def model_to_source(self, x: float, y: float) -> tuple[float, float]:
        """Undo padding and return source-frame normalized coordinates."""

        return (
            (x * self.target_width - self.pad_left) / self.resized_width,
            (y * self.target_height - self.pad_top) / self.resized_height,
        )


@dataclass(frozen=True, slots=True)
class ResizeWithStretchTransform:
    """Map normalized points for a model that stretches the full source frame."""

    source_width: int
    source_height: int
    target_width: int = 192
    target_height: int = 192

    def __post_init__(self) -> None:
        if min(
            self.source_width,
            self.source_height,
            self.target_width,
            self.target_height,
        ) <= 0:
            raise ValueError("resize-stretch dimensions must be positive")

    def source_to_model(self, x: float, y: float) -> tuple[float, float]:
        return x, y

    def model_to_source(self, x: float, y: float) -> tuple[float, float]:
        return x, y


def decode_movenet_singlepose(
    output: Any,
    captured: CapturedFrame[Any],
    *,
    resize_transform: ResizeWithPadTransform | None = None,
) -> PoseFrame:
    """Decode MoveNet `[y, x, confidence]` output into the common pose contract."""

    points = output
    try:
        while len(points) == 1:
            points = points[0]
        if len(points) != len(MOVENET_COCO_KEYPOINTS):
            raise MoveNetOutputError("MoveNet output must contain 17 keypoints")
    except (TypeError, IndexError) as exc:
        raise MoveNetOutputError("MoveNet output has an unsupported shape") from exc

    unpad=None
    if type(resize_transform)is ResizeWithPadTransform:
        unpad=(resize_transform.target_width,resize_transform.target_height,
            resize_transform.pad_left,resize_transform.pad_top,
            resize_transform.resized_width,resize_transform.resized_height)
    if unpad is not None:tw,th,left,top,rw,rh=unpad
    decoded: dict[str, Keypoint] = {}
    for name, raw_point in zip(MOVENET_COCO_KEYPOINTS, points, strict=True):
        try:
            if len(raw_point) != 3:
                raise MoveNetOutputError("each MoveNet keypoint must contain y, x and confidence")
            y = float(raw_point[0])
            x = float(raw_point[1])
            confidence = float(raw_point[2])
        except (TypeError, ValueError, IndexError) as exc:
            raise MoveNetOutputError(f"invalid MoveNet keypoint: {name}") from exc
        if not all(math.isfinite(value) for value in (x, y, confidence)):
            raise MoveNetOutputError(f"non-finite MoveNet keypoint: {name}")
        if not 0.0 <= confidence <= 1.0:
            raise MoveNetOutputError(f"MoveNet confidence outside [0, 1]: {name}")
        if resize_transform is not None:
            if unpad is None:x,y=resize_transform.model_to_source(x,y)
            else:
                x=(x*tw-left)/rw;y=(y*th-top)/rh
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                confidence = 0.0
                x = min(1.0, max(0.0, x))
                y = min(1.0, max(0.0, y))
        decoded[name] = Keypoint(x=x, y=y, confidence=confidence)

    return PoseFrame(
        view=captured.view,
        frame_id=captured.frame_id,
        timestamp_ns=captured.sync_timestamp_ns,
        keypoints=decoded,
        source_width=captured.width,
        source_height=captured.height,
    )
