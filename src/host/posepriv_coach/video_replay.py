"""Timestamped OpenCV video-file source for deterministic dual-view validation."""

from __future__ import annotations

from pathlib import Path
import math
from typing import Any, Iterator, Protocol

from .capture import CapturedFrame, transform_camera_payload
from .contracts import CameraView


class VideoSourceError(RuntimeError):
    pass


class VideoFrameSource(Protocol):
    view: CameraView

    @property
    def frames_read(self) -> int: ...

    def read(self) -> CapturedFrame[Any] | None: ...


class OpenCVVideoSource:
    """Read one local video using media-time timestamps instead of wall time."""

    def __init__(
        self,
        view: CameraView,
        path: str | Path,
        *,
        start_timestamp_ns: int = 0,
        fps_override: float | None = None,
        cv2_module: Any | None = None,
        rotation_degrees: int = 0,
        mirror_horizontal: bool = False,
    ) -> None:
        if start_timestamp_ns < 0:
            raise ValueError("start_timestamp_ns must be non-negative")
        self.view = view
        self.path = Path(path)
        if not self.path.is_file():
            raise VideoSourceError(f"video file does not exist: {self.path}")
        if cv2_module is None:
            try:
                import cv2 as cv2_module
            except ImportError as exc:
                raise VideoSourceError("video replay requires OpenCV") from exc
        self._cv2 = cv2_module
        if rotation_degrees not in (0, 90, 180, 270):
            raise ValueError("video rotation must be 0, 90, 180, or 270 degrees")
        if not isinstance(mirror_horizontal, bool):
            raise ValueError("video mirror_horizontal must be boolean")
        self.rotation_degrees = rotation_degrees
        self.mirror_horizontal = mirror_horizontal
        self._capture = cv2_module.VideoCapture(str(self.path))
        self._closed = False
        if not self._capture.isOpened():
            self._capture.release()
            self._closed = True
            raise VideoSourceError(f"could not open video file: {self.path}")

        metadata_fps = float(self._capture.get(cv2_module.CAP_PROP_FPS))
        self.fps = float(fps_override) if fps_override is not None else metadata_fps
        if not math.isfinite(self.fps) or self.fps <= 0.0:
            self._capture.release()
            self._closed = True
            raise VideoSourceError(
                f"video FPS is unavailable; provide an explicit override: {self.path}"
            )
        self.start_timestamp_ns = start_timestamp_ns
        self._next_frame_id = 0

    @property
    def frames_read(self) -> int:
        return self._next_frame_id

    def read(self) -> CapturedFrame[Any] | None:
        if self._closed:
            raise VideoSourceError("video source is closed")
        ok, payload = self._capture.read()
        if not ok or payload is None:
            return None
        try:
            payload = transform_camera_payload(
                payload,
                cv2_module=self._cv2,
                rotation_degrees=self.rotation_degrees,
                mirror_horizontal=self.mirror_horizontal,
            )
        except Exception as exc:
            raise VideoSourceError(
                f"could not transform video frame: {self.path}"
            ) from exc
        shape = getattr(payload, "shape", None)
        if shape is None or len(shape) < 2:
            raise VideoSourceError(f"video frame has no image dimensions: {self.path}")
        frame_id = self._next_frame_id
        timestamp_ns = self.start_timestamp_ns + round(frame_id * 1_000_000_000 / self.fps)
        frame = CapturedFrame(
            view=self.view,
            frame_id=frame_id,
            timestamp_ns=timestamp_ns,
            width=int(shape[1]),
            height=int(shape[0]),
            pixel_format="bgr8",
            payload=payload,
        )
        self._next_frame_id += 1
        return frame

    def close(self) -> None:
        if not self._closed:
            self._capture.release()
            self._closed = True

    def __enter__(self) -> OpenCVVideoSource:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


def interleave_paired_video_frames(
    front: VideoFrameSource,
    side: VideoFrameSource,
    *,
    max_frames_per_view: int | None = None,
) -> Iterator[CapturedFrame[Any]]:
    """Merge two media timelines and stop after the shorter stream's last pair."""

    if front.view is not CameraView.FRONT or side.view is not CameraView.SIDE:
        raise ValueError("video sources must be assigned to front and side views")
    if max_frames_per_view is not None and max_frames_per_view < 1:
        raise ValueError("max_frames_per_view must be positive")

    def next_frame(source: VideoFrameSource) -> CapturedFrame[Any] | None:
        if max_frames_per_view is not None and source.frames_read >= max_frames_per_view:
            return None
        return source.read()

    front_frame = next_frame(front)
    side_frame = next_frame(side)
    while front_frame is not None or side_frame is not None:
        stop_after_selected = front_frame is None or side_frame is None
        if side_frame is None or (
            front_frame is not None
            and front_frame.timestamp_ns <= side_frame.timestamp_ns
        ):
            assert front_frame is not None
            selected = front_frame
            front_frame = None
        else:
            assert side_frame is not None
            selected = side_frame
            side_frame = None
        yield selected
        if stop_after_selected:
            break
        if selected.view is CameraView.FRONT:
            front_frame = next_frame(front)
        else:
            side_frame = next_frame(side)
