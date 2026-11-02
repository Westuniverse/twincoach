"""Bounded single-flight RTMPose/action RPC over a loopback SSH tunnel.

The wire carries original JPEG bytes, explicit decode/rotation and integer source
identity. The inference host never compares its clock with the capture clock.
No automatic reconnect, retry or fallback is provided. Each accepted connection
owns a fresh canonical runtime/history. Bind/forward only on 127.0.0.1.
"""

from __future__ import annotations

import errno
import json
import math
import socket
import struct
from threading import Event, Lock
from time import monotonic, monotonic_ns
from types import MappingProxyType

from .capture import CapturedFrame
from .contracts import CameraView
from .pose_adapter import MOVENET_COCO_KEYPOINTS
from .rtmpose_onnx_reference import RawRTMKeypoint, RawRTMPoseFrame
from .stronger_pose_runtime import (
    StrongerPoseObservation,
    StrongerPosePairResult,
    StrongerPoseRuntime,
    raw_pose_to_display,
)

VERSION = "posepriv.stronger_pose_rpc.v1"
MAX_JSON_BYTES = 128 * 1024
MAX_JPEG_BYTES = 8 * 1024 * 1024
MAX_SOURCE_PIXELS = 16 * 1024 * 1024
VIEWS = ("front", "side")
_LENGTH = struct.Struct("!I")


class StrongerPoseRemoteError(RuntimeError):
    """RPC failed; this connection and its action history cannot be reused."""


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 300:
        raise ValueError("timeout must be finite, positive and at most 300 seconds")
    return float(value)


def _remaining(sock, deadline):
    left = deadline - monotonic()
    if left <= 0:
        raise TimeoutError("stronger pose RPC deadline expired")
    sock.settimeout(left)


def _receive(sock, size, deadline):
    parts = bytearray()
    while len(parts) < size:
        _remaining(sock, deadline)
        value = sock.recv(min(size - len(parts), 64 * 1024))
        if not value:
            raise EOFError("stronger pose RPC peer closed")
        parts.extend(value)
    return bytes(parts)


def _send(sock, value, deadline):
    _remaining(sock, deadline)
    sock.sendall(value)


def _bad_constant(value):
    raise ValueError(f"Nonfinite JSON constant {value}")


def _read_json(sock, deadline):
    (size,) = _LENGTH.unpack(_receive(sock, _LENGTH.size, deadline))
    if not 0 < size <= MAX_JSON_BYTES:
        raise ValueError("RPC JSON length exceeds cap")
    value = json.loads(_receive(sock, size, deadline), parse_constant=_bad_constant)
    if not isinstance(value, dict):
        raise TypeError("RPC metadata must be an object")
    return value


def _write_json(sock, value, deadline):
    encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if not 0 < len(encoded) <= MAX_JSON_BYTES:
        raise ValueError("RPC JSON length exceeds cap")
    _send(sock, _LENGTH.pack(len(encoded)) + encoded, deadline)


def _plain(value):
    if hasattr(value, "items"):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _shutdown(sock):
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


def _source(record, view):
    fields = ("frame_id", "timestamp_ns", "width", "height")
    if not isinstance(record, dict) or record.get("view") != view:
        raise ValueError("RPC requires physical front then side sources")
    if any(not _integer(record.get(k), 1 if k in ("width", "height") else 0) for k in fields):
        raise ValueError("RPC source identity/dimensions must be exact integers")
    if record["width"] * record["height"] > MAX_SOURCE_PIXELS:
        raise ValueError("RPC decoded dimensions exceed pixel cap")
    return tuple(record[k] for k in fields)


def _validate_pair(records, previous):
    if not isinstance(records, list) or len(records) != 2:
        raise ValueError("RPC requires exactly two physical sources")
    sources = {v: _source(r, v) for v, r in zip(VIEWS, records, strict=True)}
    if abs(sources["front"][1] - sources["side"][1]) > 50_000_000:
        raise ValueError("RPC pair exceeds 50ms source-clock skew")
    for view, source in sources.items():
        old = previous.get(view)
        if old is not None and (
            source[0] <= old[0] or source[1] <= old[1] or source[2:] != old[2:]
        ):
            raise ValueError("Both RPC sources must advance with fixed geometry")
    return sources


def _jpeg_size(jpeg):
    """Read only marker lengths/SOF dimensions before allowing decoder allocation."""
    if not jpeg.startswith(b"\xff\xd8"):
        raise ValueError("Original JPEG payload required")
    offset = 2
    while offset < len(jpeg):
        if jpeg[offset] != 255:
            raise ValueError("Malformed JPEG header")
        while offset < len(jpeg) and jpeg[offset] == 255:
            offset += 1
        if offset >= len(jpeg):
            break
        marker = jpeg[offset]
        offset += 1
        if marker in (0xD9, 0xDA):
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(jpeg):
            break
        length = int.from_bytes(jpeg[offset : offset + 2], "big")
        if length < 2 or offset + length > len(jpeg):
            break
        if marker in (0xC0, 0xC1, 0xC2):
            if length < 8 or jpeg[offset + 2] != 8:
                raise ValueError("Unsupported JPEG sample precision")
            height = int.from_bytes(jpeg[offset + 3 : offset + 5], "big")
            width = int.from_bytes(jpeg[offset + 5 : offset + 7], "big")
            if min(width, height) < 1 or width * height > MAX_SOURCE_PIXELS:
                raise ValueError("JPEG dimensions exceed pixel cap")
            return width, height
        offset += length
    raise ValueError("Missing supported JPEG frame header")


def _decode(record, jpeg, scale):
    import cv2
    import numpy as np

    width, height = _jpeg_size(jpeg)
    size = ((width + scale - 1) // scale, (height + scale - 1) // scale)
    rotation = record.get("rotation")
    if type(rotation) is not int or rotation not in (0, 90, 180, 270):
        raise ValueError("Explicit rotation must be 0/90/180/270 clockwise degrees")
    if rotation in (90, 270):
        size = size[::-1]
    if size != (record["width"], record["height"]):
        raise ValueError("JPEG/decode/rotation dimensions disagree with CapturedFrame")
    flag = {1: cv2.IMREAD_COLOR, 2: cv2.IMREAD_REDUCED_COLOR_2, 4: cv2.IMREAD_REDUCED_COLOR_4}[
        scale
    ]
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), flag)
    if image is None:
        raise ValueError("RPC JPEG decode failed")
    if rotation:
        image = cv2.rotate(
            image,
            {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}[
                rotation
            ],
        )
    if image.shape != (record["height"], record["width"], 3):
        raise ValueError("Decoded JPEG dimensions disagree with CapturedFrame")
    return CapturedFrame(
        CameraView(record["view"]),
        record["frame_id"],
        record["timestamp_ns"],
        record["width"],
        record["height"],
        "bgr8",
        image,
    )


def _encode_result(result):
    snapshots = []
    raw = []
    for view in VIEWS:
        observation = result.observations[CameraView(view)]
        snapshot = observation.action_snapshot
        raw.append(observation.raw.to_record())
        snapshots.append(
            {
                "x97": snapshot.x97.tolist(),
                "metric_ready": snapshot.metric_ready.tolist(),
                "root_ready": snapshot.root_ready,
                "warm": snapshot.warm,
            }
        )
    return {"raw": raw, "snapshots": snapshots, "action": _plain(result.action)}


def _decode_result(response, sources):
    import numpy as np

    from .supervised_action import ACTIONS, Snapshot

    records, snapshots, action = (response.get(k) for k in ("raw", "snapshots", "action"))
    if (
        not isinstance(records, list)
        or len(records) != 2
        or not isinstance(snapshots, list)
        or len(snapshots) != 2
    ):
        raise ValueError("RPC result requires two raw observations/snapshots")
    observations = {}
    for view, record, snapshot in zip(VIEWS, records, snapshots, strict=True):
        source = sources[view]
        if (
            not isinstance(record, dict)
            or record.get("schema") != "rtmpose_coco17_raw_v1"
            or record.get("coordinate_system") != "normalized_image_xy_unclipped"
            or record.get("response_definition") != "min(max_simcc_x,max_simcc_y)_uncalibrated"
            or record.get("view") != view
            or any(
                type(record.get(k)) is not int
                for k in ("frame_id", "timestamp_ns", "source_width", "source_height")
            )
            or tuple(
                record.get(k) for k in ("frame_id", "timestamp_ns", "source_width", "source_height")
            )
            != source
        ):
            raise ValueError("RPC raw response changed physical source identity/contract")
        points = record.get("keypoints")
        if not isinstance(points, dict) or set(points) != set(MOVENET_COCO_KEYPOINTS):
            raise ValueError("RPC raw response requires COCO17")
        keypoints = {}
        for name, point in points.items():
            if not isinstance(point, dict) or set(point) != {
                "x",
                "y",
                "raw_response",
                "inside_source",
            }:
                raise ValueError("Invalid RPC raw point")
            p = RawRTMKeypoint(**point)
            inside = p.x is not None and 0 <= p.x < 1 and 0 <= p.y < 1
            if inside != p.inside_source:
                raise ValueError("RPC source-inside flag contradicts coordinates")
            keypoints[name] = p
        raw = RawRTMPoseFrame(CameraView(view), *source, keypoints)
        if not isinstance(snapshot, dict) or set(snapshot) != {
            "x97",
            "metric_ready",
            "root_ready",
            "warm",
        }:
            raise ValueError("Invalid RPC snapshot")
        snap = Snapshot(
            CameraView(view),
            source[0],
            source[1],
            np.asarray(snapshot["x97"]),
            np.asarray(snapshot["metric_ready"]),
            snapshot["root_ready"],
            snapshot["warm"],
        )
        observations[CameraView(view)] = StrongerPoseObservation(raw, snap)
    if not isinstance(action, dict):
        raise TypeError("RPC action metadata must be an object")
    identity = {
        "schema": "posepriv.supervised_action_prediction.v1",
        "front_frame_id": sources["front"][0],
        "front_timestamp_ns": sources["front"][1],
        "side_frame_id": sources["side"][0],
        "side_timestamp_ns": sources["side"][1],
        "timestamp_ns": max(sources[v][1] for v in VIEWS),
        "skew_ns": abs(sources["front"][1] - sources["side"][1]),
        "provider": "rtmpose_m_plus_supervised_paired_mlp_v1",
        "production_qualified": False,
    }
    if any(type(action.get(k)) is not type(v) or action.get(k) != v for k, v in identity.items()):
        raise ValueError("RPC action changed source identity/provider/qualification")
    p, reason, logits = (action.get(k) for k in ("class_id", "reason", "logits"))
    if (
        type(p) is not int
        or p not in (-1, 0, 1, 2, 3)
        or action.get("action") != ("unknown" if p == -1 else ACTIONS[p])
        or reason not in ("warmup", "insufficient_predicted_family_observation", "model_prediction")
        or (p == -1) != (reason != "model_prediction")
    ):
        raise ValueError("RPC action class/reason differs")
    if reason == "warmup":
        if logits is not None:
            raise ValueError("Warmup must not supply model logits")
    elif (
        not isinstance(logits, list)
        or len(logits) != 4
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in logits)
    ):
        raise ValueError("RPC requires four finite uncalibrated logits")
    display = {v: raw_pose_to_display(o.raw) for v, o in observations.items()}
    return StrongerPosePairResult(
        MappingProxyType(observations), MappingProxyType(display), _freeze(action)
    )


class RemoteStrongerPoseClient:
    """One ordered pair in flight, with a single bounded wall deadline per call."""

    def __init__(
        self,
        host="127.0.0.1",
        port=None,
        timeout=10.0,
        *,
        decode_scale=1,
        max_jpeg_bytes=MAX_JPEG_BYTES,
    ):
        if host != "127.0.0.1" or not _integer(port, 1) or port > 65535:
            raise ValueError("Explicit 127.0.0.1 TCP port required; use an SSH loopback tunnel")
        if type(decode_scale) is not int or decode_scale not in (1, 2, 4):
            raise ValueError("decode_scale must be 1/2/4 matching preview decoding")
        if not _integer(max_jpeg_bytes, 1) or max_jpeg_bytes > MAX_JPEG_BYTES:
            raise ValueError("JPEG cap exceeds protocol bound")
        self.timeout = _timeout(timeout)
        self.decode_scale, self.max_jpeg_bytes = decode_scale, max_jpeg_bytes
        self._lock = Lock()
        self._closed = self._failed = False
        self._frames = self._pairs = self._bytes_sent = self._bytes_received = 0
        self._last_source = {}
        self._rotations = None
        self.last_timing_ns = {}
        self._socket = socket.create_connection((host, port), timeout=self.timeout)

    def infer_preview_pair(self, frames, selected, rotations):
        if not self._lock.acquire(False):
            raise StrongerPoseRemoteError("Only one remote request may be in flight")
        try:
            if self._closed or self._failed:
                raise StrongerPoseRemoteError(
                    "Remote client is closed or failed; no automatic reconnect"
                )
            started = monotonic_ns()
            frames = tuple(frames)
            if len(frames) != 2 or any(
                not isinstance(frame, CapturedFrame) or type(frame.view) is not CameraView
                for frame in frames
            ):
                raise ValueError("Exactly two physical CapturedFrames required")
            by_view = {frame.view.value: frame for frame in frames}
            if (
                set(by_view) != set(VIEWS)
                or len(by_view) != 2
                or set(selected) != set(VIEWS)
                or set(rotations) != set(VIEWS)
            ):
                raise ValueError("Exactly two physical frames/selected JPEGs/rotations required")
            records, jpegs = [], []
            for view in VIEWS:
                frame = by_view[view]
                identity = selected[view]
                if (
                    not isinstance(frame, CapturedFrame)
                    or type(frame.view) is not CameraView
                    or frame.pixel_format != "bgr8"
                    or frame.sync_timestamp_ns != frame.timestamp_ns
                    or not isinstance(identity, (tuple, list))
                    or len(identity) != 3
                    or type(identity[0]) is not int
                    or type(identity[1]) is not int
                    or tuple(identity[:2]) != (frame.frame_id, frame.timestamp_ns)
                ):
                    raise ValueError("CapturedFrame must match original JPEG source ID/ns")
                jpeg = identity[2]
                if type(jpeg) is not bytes or not 0 < len(jpeg) <= self.max_jpeg_bytes:
                    raise ValueError("Original nonempty JPEG bytes within cap required")
                rotation = rotations[view]
                if type(rotation) is not int or rotation not in (0, 90, 180, 270):
                    raise ValueError("Explicit preview rotation required")
                records.append(
                    {
                        "view": view,
                        "frame_id": frame.frame_id,
                        "timestamp_ns": frame.timestamp_ns,
                        "width": frame.width,
                        "height": frame.height,
                        "rotation": rotation,
                        "jpeg_bytes": len(jpeg),
                    }
                )
                jpegs.append(jpeg)
            sources = _validate_pair(records, self._last_source)
            if self._rotations is not None and rotations != self._rotations:
                raise ValueError("Rotation changed; open a new connection/history")
            sequence = self._pairs + 1
            request = {
                "schema": VERSION,
                "sequence": sequence,
                "operation": "infer_pair",
                "decode_scale": self.decode_scale,
                "sources": records,
            }
            deadline = monotonic() + self.timeout
            _write_json(self._socket, request, deadline)
            for jpeg in jpegs:
                _send(self._socket, jpeg, deadline)
            response = _read_json(self._socket, deadline)
            if (
                response.get("schema") != VERSION
                or type(response.get("sequence")) is not int
                or response["sequence"] != sequence
                or response.get("ok") is not True
            ):
                raise StrongerPoseRemoteError("Remote response failed protocol/request matching")
            result = _decode_result(response, sources)
            if (
                response.get("frames") != self._frames + 2
                or type(response.get("frames")) is not int
            ):
                raise ValueError("Remote runtime frame accounting differs")
            self._last_source = sources
            self._rotations = dict(rotations)
            self._frames += 2
            self._pairs += 1
            self._bytes_sent += sum(map(len, jpegs))
            self._bytes_received += len(json.dumps(response))
            self.last_timing_ns = {"remote_round_trip": monotonic_ns() - started}
            return result
        except BaseException:
            self._failed = True
            _shutdown(self._socket)
            self._socket = None
            raise
        finally:
            self._lock.release()

    def stats(self):
        return {
            "backend": "remote_onnxruntime_cpu_rtmpose_m",
            "frames": self._frames,
            "classified_pairs": self._pairs,
            "dpu_frames": 0,
            "failed": self._failed,
            "closed": self._closed,
            "jpeg_bytes_sent": self._bytes_sent,
            "response_json_bytes": self._bytes_received,
            "production_qualified": False,
        }

    def close(self):
        self._closed = True
        _shutdown(self._socket)
        self._socket = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class StrongerPoseLoopbackServer:
    """Serial loopback listener. No model/history is shared between connections."""

    def __init__(self, bundle_dir, *, port, timeout=30.0, num_threads=2, pair_parallel=False, runtime_factory=None):
        if not _integer(port) or port > 65535:
            raise ValueError("Explicit TCP port required (0 selects an ephemeral test port)")
        if type(num_threads) is not int or num_threads < 1:
            raise ValueError("num_threads must be a positive integer")
        if type(pair_parallel) is not bool:
            raise ValueError("pair_parallel must be boolean")
        self.timeout = _timeout(timeout)
        self.bundle_dir, self.num_threads = bundle_dir, num_threads
        self._factory = runtime_factory or (
            lambda: StrongerPoseRuntime(bundle_dir, num_threads=num_threads, allow_spinning=False,
                                       pair_parallel=pair_parallel)
        )
        if runtime_factory is None:
            # Validate the explicit local config/artifact before advertising a
            # listener. This creates no ORT session and retains no history.
            probe = self._factory()
            probe.close()
        self._stop = Event()
        self._connection = None
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._socket.bind(("127.0.0.1", port))
            self._socket.listen(1)
            self._socket.settimeout(0.1)
        except BaseException:
            self._socket.close()
            raise
        self.port = self._socket.getsockname()[1]
        self._connections = self._frames = self._errors = 0
        self._last_error = None

    def _serve_connection(self, connection):
        runtime = self._factory()
        previous = {}
        transform = None
        sequence = 0
        try:
            while not self._stop.is_set():
                deadline = monotonic() + self.timeout
                try:
                    request = _read_json(connection, deadline)
                except EOFError:
                    return
                if (
                    request.get("schema") != VERSION
                    or request.get("operation") != "infer_pair"
                    or type(request.get("sequence")) is not int
                    or request["sequence"] != sequence + 1
                    or type(request.get("decode_scale")) is not int
                    or request["decode_scale"] not in (1, 2, 4)
                ):
                    raise ValueError("Invalid ordered RPC request")
                sources = _validate_pair(request.get("sources"), previous)
                current_transform = (
                    request["decode_scale"],
                    tuple(r.get("rotation") for r in request["sources"]),
                )
                if transform is not None and current_transform != transform:
                    raise ValueError("Decode/rotation changed within a connection/history")
                frames = []
                for record in request["sources"]:
                    size = record.get("jpeg_bytes")
                    if not _integer(size, 1) or size > MAX_JPEG_BYTES:
                        raise ValueError("JPEG payload length exceeds cap")
                    jpeg = _receive(connection, size, deadline)
                    frames.append(_decode(record, jpeg, request["decode_scale"]))
                result = runtime.infer_pair(frames)
                response = dict(
                    schema=VERSION,
                    sequence=request["sequence"],
                    ok=True,
                    frames=runtime.stats()["frames"],
                    **_encode_result(result),
                )
                _write_json(connection, response, deadline)
                previous = sources
                transform = current_transform
                sequence += 1
                self._frames += 2
        finally:
            runtime.close()

    def serve_forever(self):
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                raise
            self._connection = connection
            self._connections += 1
            try:
                self._serve_connection(connection)
            except Exception as exc:  # noqa: BLE001 - isolate failed connection; retain error in stats
                if (
                    self._stop.is_set()
                    and isinstance(exc, OSError)
                    and exc.errno
                    in (
                        errno.EBADF,
                        errno.ENOTCONN,
                        errno.EPIPE,
                        errno.ECONNRESET,
                        errno.ECONNABORTED,
                        getattr(errno, "ESHUTDOWN", -1),
                        # Winsock reports its own closed/not-connected codes.
                        10038,
                        10057,
                        10058,
                        10054,
                        10053,
                    )
                ):
                    # close() intentionally interrupts the currently owned
                    # socket. Protocol/model exceptions remain failures even
                    # if shutdown was requested at the same time.
                    continue
                self._errors += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                # Closing this connection is the explicit failed response. Never
                # continue a half-consumed request or reuse its runtime/history.
            finally:
                _shutdown(connection)
                self._connection = None

    def stats(self):
        return {
            "connections": self._connections,
            "frames": self._frames,
            "dpu_frames": 0,
            "connection_errors": self._errors,
            "closed": self._stop.is_set(),
            "last_error": self._last_error,
        }

    def close(self):
        self._stop.set()
        _shutdown(self._connection)
        _shutdown(self._socket)
