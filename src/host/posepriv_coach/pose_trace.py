"""Versioned JSONL recording and replay for keypoints without raw images."""

from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import Iterator, TextIO

from .contracts import CameraView, Keypoint, PoseFrame


SCHEMA_VERSION = 2


class PoseTraceWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._stream: TextIO = self.path.open("w", encoding="utf-8", newline="\n")
        self._last_timestamp: dict[CameraView, int] = {}
        self._closed = False
        self._write_record(
            {
                "record_type": "pose_trace_header",
                "schema_version": SCHEMA_VERSION,
                "coordinate_system": "normalized_image_xy_unclipped",
                "timestamp_unit": "ns_monotonic",
            }
        )

    def write(self, frame: PoseFrame) -> None:
        if self._closed:
            raise ValueError("pose trace is closed")
        previous = self._last_timestamp.get(frame.view)
        if previous is not None and frame.timestamp_ns < previous:
            raise ValueError(f"decreasing timestamp for {frame.view.value} pose trace")
        self._last_timestamp[frame.view] = frame.timestamp_ns
        self._write_record(
            {
                "record_type": "pose_frame",
                "view": frame.view.value,
                "frame_id": frame.frame_id,
                "timestamp_ns": frame.timestamp_ns,
                "source_width": frame.source_width,
                "source_height": frame.source_height,
                "keypoints": {
                    name: [point.x, point.y, point.confidence]
                    for name, point in sorted(frame.keypoints.items())
                },
            }
        )

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        if not self._closed:
            self._stream.close()
            self._closed = True

    def _write_record(self, record: object) -> None:
        self._stream.write(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        )
        self._stream.write("\n")

    def __enter__(self) -> PoseTraceWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def read_pose_trace(path: str | Path) -> Iterator[PoseFrame]:
    with Path(path).open("r", encoding="utf-8") as stream:
        header_line = stream.readline()
        if not header_line:
            raise ValueError("pose trace is empty")
        header = _parse_json_record(header_line, 1)
        schema_version = header.get("schema_version")
        if header.get("record_type") != "pose_trace_header" or schema_version not in (1, 2):
            raise ValueError("unsupported pose trace header or schema version")

        last_timestamp: dict[CameraView, int] = {}
        for line_number, line in enumerate(stream, start=2):
            if not line.strip():
                continue
            raw = _parse_json_record(line, line_number)
            if raw.get("record_type") != "pose_frame":
                raise ValueError(f"unexpected record at pose trace line {line_number}")
            try:
                view = CameraView(raw["view"])
                timestamp_ns = int(raw["timestamp_ns"])
                keypoints = {
                    name: Keypoint(float(values[0]), float(values[1]), float(values[2]))
                    for name, values in raw["keypoints"].items()
                }
                frame = PoseFrame(
                    view=view,
                    frame_id=int(raw["frame_id"]),
                    timestamp_ns=timestamp_ns,
                    keypoints=keypoints,
                    source_width=int(raw["source_width"]) if schema_version >= 2 else 1,
                    source_height=int(raw["source_height"]) if schema_version >= 2 else 1,
                )
            except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
                raise ValueError(f"invalid pose frame at line {line_number}") from exc
            previous = last_timestamp.get(view)
            if previous is not None and timestamp_ns < previous:
                raise ValueError(f"decreasing {view.value} timestamp at line {line_number}")
            last_timestamp[view] = timestamp_ns
            yield frame


def _parse_json_record(line: str, line_number: int) -> dict[str, object]:
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at pose trace line {line_number}") from exc
    if not isinstance(record, dict):
        raise ValueError(f"pose trace line {line_number} is not an object")
    return record
