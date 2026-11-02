"""Read recorded JPEG byte ranges with their original per-frame capture times."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .capture import CapturedFrame, transform_camera_payload
from .contracts import CameraView
from .video_replay import VideoSourceError


@dataclass(frozen=True, slots=True)
class MjpegIndexRow:
    frame_id: int
    timestamp_ns: int
    offset: int
    length: int


def load_mjpeg_index(path: str | Path, *, video_size: int) -> tuple[MjpegIndexRow, ...]:
    rows = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            values = [record[key] for key in ("frame_id", "timestamp_ns", "offset", "length")]
            if any(type(value) is not int for value in values):
                raise ValueError("index fields must be integers")
            row = MjpegIndexRow(*values)
            if (min(row.frame_id, row.timestamp_ns, row.offset) < 0 or row.length < 4
                    or row.offset + row.length > video_size):
                raise ValueError("invalid or truncated byte range")
            if rows and (row.frame_id <= rows[-1].frame_id
                         or row.timestamp_ns <= rows[-1].timestamp_ns
                         or row.offset < rows[-1].offset + rows[-1].length):
                raise ValueError("non-monotonic frame/time or overlapping byte range")
        except (ValueError, KeyError, TypeError) as error:
            raise VideoSourceError(f"Invalid MJPEG index line {number}: {error}") from error
        rows.append(row)
    if not rows:
        raise VideoSourceError("MJPEG index contains no recorded frames")
    return tuple(rows)


class IndexedMjpegVideoSource:
    """Decode only recorded frames; never invent FPS-based timestamps or pixels."""

    def __init__(self, view: CameraView, path: str | Path, timestamps: str | Path, *,
                 timestamp_offset_ns: int = 0, rotation_degrees: int = 0,
                 mirror_horizontal: bool = False, cv2_module: Any | None = None) -> None:
        if rotation_degrees not in (0, 90, 180, 270) or type(mirror_horizontal) is not bool:
            raise ValueError("Invalid MJPEG orientation")
        if type(timestamp_offset_ns) is not int:
            raise ValueError("Timestamp offset must be integer nanoseconds")
        self.view, self.path = view, Path(path)
        self.index = load_mjpeg_index(timestamps, video_size=self.path.stat().st_size)
        if self.index[0].timestamp_ns + timestamp_offset_ns < 0:
            raise ValueError("Shifted capture time must be non-negative")
        if cv2_module is None:
            import cv2 as cv2_module
        self._cv2 = cv2_module
        self._file = self.path.open("rb")
        self._read = 0
        self._closed = False
        self._offset_ns = timestamp_offset_ns
        self.rotation_degrees = rotation_degrees
        self.mirror_horizontal = mirror_horizontal

    @property
    def frames_read(self) -> int:
        return self._read

    def read(self) -> CapturedFrame[Any] | None:
        if self._closed:
            raise VideoSourceError("MJPEG source is closed")
        if self._read == len(self.index):
            return None
        import numpy as np

        row = self.index[self._read]
        self._file.seek(row.offset)
        packet = self._file.read(row.length)
        if (len(packet) != row.length or packet[:2] != b"\xff\xd8"
                or packet[-2:] != b"\xff\xd9"):
            raise VideoSourceError(f"Broken JPEG at frame {row.frame_id}")
        payload = self._cv2.imdecode(np.frombuffer(packet, dtype=np.uint8), self._cv2.IMREAD_COLOR)
        if payload is None:
            raise VideoSourceError(f"Could not decode JPEG at frame {row.frame_id}")
        payload = transform_camera_payload(payload, cv2_module=self._cv2,
                                           rotation_degrees=self.rotation_degrees,
                                           mirror_horizontal=self.mirror_horizontal)
        self._read += 1
        return CapturedFrame(view=self.view, frame_id=row.frame_id,
                             timestamp_ns=row.timestamp_ns + self._offset_ns,
                             width=int(payload.shape[1]), height=int(payload.shape[0]),
                             pixel_format="bgr8", payload=payload)

    def close(self) -> None:
        if not self._closed:
            self._file.close()
            self._closed = True
