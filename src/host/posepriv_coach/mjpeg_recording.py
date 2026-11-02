"""Versioned raw MJPEG transport and fail-closed host recording storage."""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path
from typing import BinaryIO, TextIO

HEADER = struct.Struct("<4sBBHQQI")
MAGIC = b"PPRJ"
VERSION = 1
METADATA, FRONT, SIDE, FOOTER = range(4)
VIEWS = {FRONT: "front", SIDE: "side"}
MAX_JPEG = 16 * 1024 * 1024
MAX_JSON = 64 * 1024


class MjpegTransportError(ValueError):
    """A recording cannot be finalized because its transport is incomplete."""


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        data = stream.read(count - len(chunks))
        if not data:
            raise MjpegTransportError("Truncated MJPEG transport")
        chunks.extend(data)
    return bytes(chunks)


def _write_all(stream: BinaryIO, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        count = stream.write(remaining)
        if count is None or count <= 0:
            raise OSError("MJPEG transport write made no progress")
        remaining = remaining[count:]


def write_record(stream: BinaryIO, kind: int, frame_id: int, timestamp_ns: int,
                 payload: bytes) -> None:
    limit = MAX_JPEG if kind in VIEWS else MAX_JSON
    if kind not in (METADATA, FRONT, SIDE, FOOTER) or not 0 < len(payload) <= limit:
        raise MjpegTransportError("Invalid transport record kind or length")
    _write_all(stream, HEADER.pack(MAGIC, VERSION, kind, 0, frame_id, timestamp_ns, len(payload)))
    _write_all(stream, payload)


def write_json_record(stream: BinaryIO, kind: int, value: dict) -> None:
    write_record(stream, kind, 0, 0, json.dumps(value, separators=(",", ":")).encode("utf-8"))


def read_record(stream: BinaryIO) -> tuple[int, int, int, bytes]:
    magic, version, kind, flags, frame_id, timestamp, length = HEADER.unpack(
        _read_exact(stream, HEADER.size))
    limit = MAX_JPEG if kind in VIEWS else MAX_JSON
    if (magic != MAGIC or version != VERSION or flags != 0
            or kind not in (METADATA, FRONT, SIDE, FOOTER) or not 0 < length <= limit):
        raise MjpegTransportError("Invalid MJPEG protocol header")
    return kind, frame_id, timestamp, _read_exact(stream, length)


def _json_object(payload: bytes) -> dict:
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError) as error:
        raise MjpegTransportError("Invalid JSON transport record") from error
    if not isinstance(value, dict):
        raise MjpegTransportError("Transport metadata must be an object")
    return value


class MjpegRecordingReceiver:
    """Commit only after receive() AND a successful remote process exit."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.manifest = {"status": "recording", "contains_raw_pixels": True,
                         "transport": "ssh_raw_mjpeg_v1"}
        self._videos: dict[str, BinaryIO] = {}
        self._indexes: dict[str, TextIO] = {}
        self._counts = {view: 0 for view in VIEWS.values()}
        self._bytes = dict.fromkeys(VIEWS.values(), 0)
        self._last_time = dict.fromkeys(VIEWS.values(), -1)
        self._received = False
        self._committed = False
        self._save_manifest()

    def _save_manifest(self) -> None:
        (self.directory / "capture_manifest.json").write_text(
            json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8")

    def __enter__(self) -> MjpegRecordingReceiver:  # noqa: PYI034 -- board Python 3.10 has no typing.Self
        return self

    def receive(self, stream: BinaryIO) -> dict:
        kind, frame_id, timestamp, payload = read_record(stream)
        if kind != METADATA or frame_id != 0 or timestamp != 0:
            raise MjpegTransportError("First record must be metadata")
        metadata = _json_object(payload)
        if (metadata.get("timestamp_semantics") != "board_monotonic_after_raw_mjpeg_read_not_hardware_exposure_sync"
                or metadata.get("includes_pose_inference") is not False
                or any(type(metadata.get(key)) is not int or metadata[key] < 1
                       for key in ("source_width", "source_height"))
                or any(type(metadata.get(key)) is not int or metadata[key] not in (0, 90, 180, 270)
                       for key in ("front_rotation_degrees", "side_rotation_degrees"))):
            raise MjpegTransportError("Unsupported recording time or inference semantics")
        self.manifest.update(metadata)
        self.manifest.update(status="recording", contains_raw_pixels=True, transport="ssh_raw_mjpeg_v1")
        self._save_manifest()
        for view in VIEWS.values():
            self._videos[view] = (self.directory / (view + ".mjpeg.partial")).open("wb")
            self._indexes[view] = (self.directory / (view + ".timestamps.jsonl")).open("w", encoding="utf-8")
        while True:
            kind, frame_id, timestamp, payload = read_record(stream)
            if kind == FOOTER:
                footer = _json_object(payload)
                if frame_id != 0 or timestamp != 0 or footer.get("status") != "complete":
                    raise MjpegTransportError("Missing complete recording footer")
                for view in VIEWS.values():
                    stats = footer.get("streams", {}).get(view, {})
                    if (not self._counts[view] or type(stats.get("frames")) is not int
                            or type(stats.get("bytes")) is not int
                            or stats["frames"] != self._counts[view] or stats["bytes"] != self._bytes[view]):
                        raise MjpegTransportError("Footer frame/byte counts do not match received data")
                if stream.read(1):
                    raise MjpegTransportError("Unexpected bytes after recording footer")
                self.manifest["streams"] = footer["streams"]
                self.manifest["sender_diagnostics"] = footer.get("diagnostics", {})
                self._received = True
                return footer
            if kind not in VIEWS:
                raise MjpegTransportError("Unexpected metadata inside frame stream")
            view = VIEWS[kind]
            if frame_id != self._counts[view] or timestamp <= self._last_time[view]:
                raise MjpegTransportError("Non-contiguous frame ID or non-increasing capture time")
            if payload[:2] != b"\xff\xd8" or payload[-2:] != b"\xff\xd9":
                raise MjpegTransportError("Invalid JPEG packet boundaries")
            self._videos[view].write(payload)
            self._indexes[view].write(json.dumps({"frame_id": frame_id, "timestamp_ns": timestamp,
                                                "offset": self._bytes[view], "length": len(payload)}) + "\n")
            self._counts[view] += 1
            self._bytes[view] += len(payload)
            self._last_time[view] = timestamp

    def _close(self, *, sync: bool = False) -> None:
        first_error = None
        for stream in (*self._videos.values(), *self._indexes.values()):
            if not stream.closed:
                try:
                    if sync:
                        stream.flush()
                        os.fsync(stream.fileno())
                except OSError as error:
                    first_error = first_error or error
                finally:
                    try:
                        stream.close()
                    except OSError as error:
                        first_error = first_error or error
        if first_error is not None:
            raise first_error

    def commit(self) -> None:
        if not self._received or self._committed:
            raise MjpegTransportError("Recording is incomplete or already committed")
        self._close(sync=True)
        for view in VIEWS.values():
            (self.directory / (view + ".mjpeg.partial")).rename(self.directory / (view + ".mjpeg"))
        self.manifest["status"] = "complete"
        self._save_manifest()
        self._committed = True

    def __exit__(self, exc_type, error, traceback) -> None:
        try:
            self._close()
        finally:
            if not self._committed:
                self.manifest.update(status="failed", error=(f"{type(error).__name__}: {error}"
                                     if error is not None else "Remote exit was not accepted"),
                                     received_frames=self._counts, received_bytes=self._bytes)
                self._save_manifest()
