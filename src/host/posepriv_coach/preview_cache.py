"""Bounded in-memory pixel cache for explicit comparison-display mode."""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from typing import Any

from .capture import CapturedFrame
from .contracts import CameraView


class PreviewFrameCache:
    """Retain only a few recent frames so a pose can recover its matching pixels."""

    def __init__(self, *, frames_per_view: int = 4) -> None:
        if frames_per_view < 1:
            raise ValueError("frames_per_view must be positive")
        self._capacity = frames_per_view
        self._frames: dict[CameraView, OrderedDict[int, Any]] = {
            CameraView.FRONT: OrderedDict(),
            CameraView.SIDE: OrderedDict(),
        }
        self._lock = Lock()

    def put(self, frame: CapturedFrame[Any]) -> None:
        with self._lock:
            view_frames = self._frames[frame.view]
            view_frames[frame.frame_id] = frame.payload
            view_frames.move_to_end(frame.frame_id)
            while len(view_frames) > self._capacity:
                view_frames.popitem(last=False)

    def get(self, view: CameraView, frame_id: int) -> Any | None:
        with self._lock:
            return self._frames[view].get(frame_id)

    def clear(self) -> None:
        with self._lock:
            for frames in self._frames.values():
                frames.clear()

