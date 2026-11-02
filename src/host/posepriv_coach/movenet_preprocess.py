"""OpenCV preprocessing boundary for MoveNet SinglePose Lightning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .capture import CapturedFrame
from .pose_adapter import ResizeWithPadTransform, ResizeWithStretchTransform


class MoveNetPreprocessError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MoveNetInput:
    """RGB uint8 image and the geometry needed to decode its output."""

    pixels: Any
    transform: ResizeWithPadTransform | ResizeWithStretchTransform


@dataclass(frozen=True, slots=True)
class MoveNetInference:
    """Raw model output paired with the exact input geometry used for this frame."""

    output: Any
    transform: ResizeWithPadTransform | ResizeWithStretchTransform


class MoveNetPreprocessor(Protocol):
    def prepare(self, frame: CapturedFrame[Any]) -> MoveNetInput: ...


@dataclass(frozen=True, slots=True)
class PreprocessedMoveNetRunner:
    """Compose pixel preprocessing with a TensorFlow, TFLite or VART callable."""

    preprocessor: MoveNetPreprocessor
    infer_pixels: Callable[[Any], Any]

    def __call__(self, frame: CapturedFrame[Any]) -> MoveNetInference:
        prepared = self.preprocessor.prepare(frame)
        return MoveNetInference(
            output=self.infer_pixels(prepared.pixels),
            transform=prepared.transform,
        )


class OpenCVMoveNetPreprocessor:
    """Convert BGR8 to RGB model input; optional resize-before-color is pad-only.

    The default reference order remains BGR2RGB then resize. Explicit
    resize_before_color=True changes pad mode only; stretch keeps its old order.
    """

    def __init__(
        self,
        *,
        target_width: int = 192,
        target_height: int = 192,
        resize_mode: str = "pad",
        resize_before_color: bool = False,
        cv2_module: Any | None = None,
    ) -> None:
        if target_width < 1 or target_height < 1:
            raise ValueError("MoveNet input dimensions must be positive")
        if resize_mode not in {"pad", "stretch"}:
            raise ValueError("resize_mode must be 'pad' or 'stretch'")
        if not isinstance(resize_before_color, bool):
            raise ValueError("resize_before_color must be boolean")
        if cv2_module is None:
            try:
                import cv2 as cv2_module  # type: ignore[import-not-found, no-redef]
            except ImportError as exc:
                raise MoveNetPreprocessError(
                    "OpenCV preprocessing is optional; install the project capture extra"
                ) from exc
        self.target_width = target_width
        self.target_height = target_height
        self.resize_mode = resize_mode
        self.resize_before_color = resize_before_color
        self._cv2 = cv2_module

    def prepare(self, frame: CapturedFrame[Any]) -> MoveNetInput:
        if frame.pixel_format.lower() != "bgr8":
            raise MoveNetPreprocessError(
                f"MoveNet OpenCV preprocessing requires bgr8, got {frame.pixel_format!r}"
            )
        resize_first = self.resize_before_color and self.resize_mode == "pad"
        rgb = None if resize_first else self._cv2.cvtColor(
            frame.payload, self._cv2.COLOR_BGR2RGB
        )
        if self.resize_mode == "stretch":
            transform = ResizeWithStretchTransform(
                source_width=frame.width,
                source_height=frame.height,
                target_width=self.target_width,
                target_height=self.target_height,
            )
            pixels = self._cv2.resize(
                rgb,
                (self.target_width, self.target_height),
                interpolation=self._cv2.INTER_LINEAR,
            )
            return MoveNetInput(pixels=pixels, transform=transform)
        transform = ResizeWithPadTransform(
            source_width=frame.width,
            source_height=frame.height,
            target_width=self.target_width,
            target_height=self.target_height,
        )
        resized = self._cv2.resize(
            frame.payload if resize_first else rgb,
            (transform.resized_width, transform.resized_height),
            interpolation=self._cv2.INTER_LINEAR,
        )
        if resize_first:
            resized = self._cv2.cvtColor(resized, self._cv2.COLOR_BGR2RGB)
        pixels = self._cv2.copyMakeBorder(
            resized,
            transform.pad_top,
            transform.pad_bottom,
            transform.pad_left,
            transform.pad_right,
            self._cv2.BORDER_CONSTANT,
            value=(0, 0, 0),
        )
        return MoveNetInput(pixels=pixels, transform=transform)
