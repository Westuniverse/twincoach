"""Host camera boundary with an optional OpenCV implementation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from time import monotonic_ns
from threading import Event, Lock, Thread
from types import TracebackType
from typing import Any, Callable, Generic, Protocol, TypeVar

from .contracts import CameraView


FramePayload = TypeVar("FramePayload")
CameraDevice = int | str
OPENCV_BACKEND_NAMES = ("auto", "v4l2", "dshow", "msmf")


class CameraError(RuntimeError):
    """Base class for capture failures that must not reuse stale frames."""


class CameraDependencyError(CameraError):
    pass


class CameraOpenError(CameraError):
    pass


class CameraReadError(CameraError):
    pass


class CapturePumpError(CameraError):
    pass


class CameraSource(Protocol):
    view: CameraView

    def read(self) -> CapturedFrame[Any]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CapturedFrame(Generic[FramePayload]):
    view: CameraView
    frame_id: int
    timestamp_ns: int
    width: int
    height: int
    pixel_format: str
    payload: FramePayload
    sync_timestamp_ns: int | None = None

    def __post_init__(self) -> None:
        if self.frame_id < 0:
            raise ValueError("frame_id must be non-negative")
        if self.timestamp_ns < 0:
            raise ValueError("timestamp_ns must be non-negative")
        if self.sync_timestamp_ns is None:
            object.__setattr__(self, "sync_timestamp_ns", self.timestamp_ns)
        elif self.sync_timestamp_ns < 0:
            raise ValueError("sync_timestamp_ns must be non-negative")
        if self.width < 1 or self.height < 1:
            raise ValueError("captured frame dimensions must be positive")
        if not self.pixel_format:
            raise ValueError("pixel_format must not be empty")


@dataclass(frozen=True, slots=True)
class CaptureTimingStats:
    """一条摄像头读取边界的耗时摘要（包含 UVC 解码返回时间）。"""

    frames: int
    total_ns: int
    max_ns: int
    latency_window_size: int = 0
    latency_sample_count: int = 0
    p95_ns: int | None = None

    @property
    def mean_ms(self) -> float | None:
        return None if self.frames == 0 else self.total_ns / self.frames / 1_000_000

    @property
    def p95_ms(self) -> float | None:
        return None if self.p95_ns is None else self.p95_ns / 1_000_000

    @property
    def max_ms(self) -> float | None:
        return None if self.frames == 0 else self.max_ns / 1_000_000

    def to_record(self) -> dict[str, int | float | None]:
        return {
            "frames": self.frames,
            "total_ms": self.total_ns / 1_000_000,
            "mean_ms": self.mean_ms,
            "max_ms": self.max_ms,
            "p95_ms": self.p95_ms,
            "latency_window_size": self.latency_window_size,
            "latency_sample_count": self.latency_sample_count,
        }


@dataclass(frozen=True, slots=True)
class CapturePumpStats:
    front_frames: int
    side_frames: int
    running: bool
    failed: bool
    front_read_timing: CaptureTimingStats | None = None
    side_read_timing: CaptureTimingStats | None = None


@dataclass(frozen=True, slots=True)
class CameraDiscoveryResult:
    device_index: int
    width: int
    height: int
    channels: int
    backend_name: str


@dataclass(frozen=True, slots=True)
class CameraNegotiatedMode:
    """OpenCV 后端在设置请求后实际公开的摄像头模式。"""

    width: int | None
    height: int | None
    fps: float | None
    fourcc: str | None
    backend_name: str

    def to_record(self) -> dict[str, int | float | str | None]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "fourcc": self.fourcc,
            "backend_name": self.backend_name,
        }

    def transformed(self, rotation_degrees: int) -> "CameraNegotiatedMode":
        """Return the frame dimensions after the optional quarter-turn."""

        _validate_camera_transform(rotation_degrees, False)
        if rotation_degrees in (90, 270):
            return CameraNegotiatedMode(
                self.height,
                self.width,
                self.fps,
                self.fourcc,
                self.backend_name,
            )
        return self


def resolve_opencv_backend(
    name: str,
    *,
    cv2_module: Any | None = None,
) -> int | None:
    """Resolve a stable CLI backend name without importing OpenCV at module load."""

    if name not in OPENCV_BACKEND_NAMES:
        raise ValueError(f"unsupported OpenCV backend: {name}")
    if name == "auto":
        return None
    if cv2_module is None:
        try:
            import cv2 as cv2_module  # type: ignore[import-not-found, no-redef]
        except ImportError as exc:
            raise CameraDependencyError(
                "OpenCV backend selection requires the project capture extra"
            ) from exc
    attribute = {
        "v4l2": "CAP_V4L2",
        "dshow": "CAP_DSHOW",
        "msmf": "CAP_MSMF",
    }[name]
    try:
        return int(getattr(cv2_module, attribute))
    except AttributeError as exc:
        raise CameraDependencyError(
            f"当前 OpenCV 构建不提供 {name} 采集后端"
        ) from exc


def parse_camera_device(value: str) -> CameraDevice:
    """Parse a CLI camera selector while preserving stable Linux device paths."""

    selector = value.strip()
    if not selector:
        raise ValueError("camera device must not be empty")
    try:
        device_index = int(selector, 10)
    except ValueError:
        return selector
    if device_index < 0:
        raise ValueError("camera index must be non-negative")
    return device_index


def discover_opencv_cameras(
    *,
    max_index: int = 8,
    backend: int | None = None,
    cv2_module: Any | None = None,
) -> tuple[CameraDiscoveryResult, ...]:
    """Return indices that can be opened and produce one valid frame."""

    if max_index < 1:
        raise ValueError("max_index must be positive")
    if cv2_module is None:
        try:
            import cv2 as cv2_module  # type: ignore[import-not-found, no-redef]
        except ImportError as exc:
            raise CameraDependencyError(
                "OpenCV camera discovery requires the project capture extra"
            ) from exc

    discovered = []
    for device_index in range(max_index):
        capture = (
            cv2_module.VideoCapture(device_index)
            if backend is None
            else cv2_module.VideoCapture(device_index, backend)
        )
        try:
            if not capture.isOpened():
                continue
            ok, payload = capture.read()
            shape = getattr(payload, "shape", ()) if ok else ()
            if len(shape) < 2:
                continue
            backend_name = "unknown"
            get_backend_name = getattr(capture, "getBackendName", None)
            if callable(get_backend_name):
                try:
                    backend_name = str(get_backend_name())
                except Exception:
                    pass
            discovered.append(
                CameraDiscoveryResult(
                    device_index=device_index,
                    width=int(shape[1]),
                    height=int(shape[0]),
                    channels=int(shape[2]) if len(shape) >= 3 else 1,
                    backend_name=backend_name,
                )
            )
        finally:
            capture.release()
    return tuple(discovered)


class DualCameraCapturePump:
    """Continuously read one source per view and submit frames without buffering."""

    def __init__(
        self,
        front_source: CameraSource,
        side_source: CameraSource,
        submit: Callable[[CapturedFrame[Any]], None],
    ) -> None:
        if front_source.view is not CameraView.FRONT:
            raise ValueError("front_source must identify the front view")
        if side_source.view is not CameraView.SIDE:
            raise ValueError("side_source must identify the side view")
        self._sources = (front_source, side_source)
        self._submit = submit
        self._stop_event = Event()
        self._state_lock = Lock()
        self._counts = {CameraView.FRONT: 0, CameraView.SIDE: 0}
        self._read_total_ns = {CameraView.FRONT: 0, CameraView.SIDE: 0}
        self._read_max_ns = {CameraView.FRONT: 0, CameraView.SIDE: 0}
        self._read_samples_ns = {
            CameraView.FRONT: deque(maxlen=1024),
            CameraView.SIDE: deque(maxlen=1024),
        }
        self._failure: Exception | None = None
        self._threads: list[Thread] = []
        self._started = False
        self._closed = False

    def start(self) -> None:
        with self._state_lock:
            if self._started:
                raise RuntimeError("dual-camera capture pump can only be started once")
            if self._closed:
                raise RuntimeError("dual-camera capture pump is closed")
            self._started = True
            self._threads = [
                Thread(
                    target=self._capture_loop,
                    args=(source,),
                    name=f"capture-{source.view.value}",
                    daemon=True,
                )
                for source in self._sources
            ]
        for thread in self._threads:
            thread.start()

    def wait_stopped(self, timeout: float | None = None) -> bool:
        return self._stop_event.wait(timeout)

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._stop_event.set()

        # Do not release an OpenCV ``VideoCapture`` while its worker thread may
        # still be inside ``read()``.  On the KV260 V4L2 backend that ordering
        # can race inside the native driver and terminate the process with a
        # segmentation fault.  A normal V4L2 read returns within one frame
        # period after the stop flag is set, so joining first is both safe and
        # fast.  The fallback release below is only for a backend whose read
        # call remains blocked beyond the bounded join window.
        self._join_threads_with_deadline(timeout=2.0)
        if any(thread.is_alive() for thread in self._threads):
            for source in self._sources:
                source.close()
            self._join_threads_with_deadline(timeout=2.0)
        else:
            for source in self._sources:
                source.close()

    def _join_threads_with_deadline(self, *, timeout: float) -> None:
        """Join all workers within one shared timeout window.

        Joining each worker for the full timeout in sequence can add two
        teardown windows to the measured capture interval when both V4L2
        reads are slow to return.  A shared deadline keeps shutdown bounded
        without releasing a live ``VideoCapture`` prematurely.
        """

        deadline_ns = monotonic_ns() + max(0.0, timeout) * 1_000_000_000
        for thread in self._threads:
            # A failed/partial start can leave one or both workers unstarted.
            # Such a worker owns no read call and cannot legally be joined.
            # Started (including already-finished) workers retain an ident.
            if thread.ident is None:
                continue
            remaining_ns = deadline_ns - monotonic_ns()
            if remaining_ns <= 0:
                break
            thread.join(timeout=remaining_ns / 1_000_000_000)

    def raise_if_failed(self) -> None:
        with self._state_lock:
            failure = self._failure
        if failure is not None:
            raise CapturePumpError("dual-camera capture worker failed") from failure

    def stats(self) -> CapturePumpStats:
        with self._state_lock:
            return CapturePumpStats(
                front_frames=self._counts[CameraView.FRONT],
                side_frames=self._counts[CameraView.SIDE],
                running=any(thread.is_alive() for thread in self._threads)
                and not self._stop_event.is_set(),
                failed=self._failure is not None,
                front_read_timing=self._read_timing(CameraView.FRONT),
                side_read_timing=self._read_timing(CameraView.SIDE),
            )

    def _read_timing(self, view: CameraView) -> CaptureTimingStats | None:
        frames = self._counts[view]
        samples = self._read_samples_ns[view]
        if frames == 0:
            return None
        return CaptureTimingStats(
            frames=frames,
            total_ns=self._read_total_ns[view],
            max_ns=self._read_max_ns[view],
            latency_window_size=samples.maxlen or 0,
            latency_sample_count=len(samples),
            p95_ns=_p95_nearest_rank(samples),
        )

    def _record_read_timing(self, view: CameraView, elapsed_ns: int) -> None:
        if elapsed_ns < 0:
            return
        with self._state_lock:
            self._read_total_ns[view] += elapsed_ns
            self._read_max_ns[view] = max(self._read_max_ns[view], elapsed_ns)
            self._read_samples_ns[view].append(elapsed_ns)

    def _capture_loop(self, source: CameraSource) -> None:
        try:
            while not self._stop_event.is_set():
                read_start_ns = monotonic_ns()
                frame = source.read()
                # Only successful reads enter the latency window.  A close()
                # that unblocks a pending read must not be mistaken for a
                # camera frame and inflate the live p95.
                self._record_read_timing(
                    source.view,
                    monotonic_ns() - read_start_ns,
                )
                if self._stop_event.is_set():
                    break
                if frame.view is not source.view:
                    raise ValueError(
                        f"{source.view.value} source returned a {frame.view.value} frame"
                    )
                self._submit(frame)
                with self._state_lock:
                    self._counts[frame.view] += 1
        except Exception as exc:
            if not self._stop_event.is_set():
                with self._state_lock:
                    if self._failure is None:
                        self._failure = exc
                self._stop_event.set()

    def __enter__(self) -> DualCameraCapturePump:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
        if exc_type is None:
            self.raise_if_failed()


class LatestFrameScheduler(Generic[FramePayload]):
    """Keep one frame per view and alternate views when both are ready."""

    def __init__(self) -> None:
        self._pending: dict[CameraView, CapturedFrame[FramePayload] | None] = {
            CameraView.FRONT: None,
            CameraView.SIDE: None,
        }
        self._last_timestamp: dict[CameraView, int] = {}
        self._next_preferred = CameraView.FRONT
        self._dropped: dict[CameraView, int] = {
            CameraView.FRONT: 0,
            CameraView.SIDE: 0,
        }
        self._lock = Lock()

    def push(self, frame: CapturedFrame[FramePayload]) -> None:
        with self._lock:
            previous_timestamp = self._last_timestamp.get(frame.view)
            if previous_timestamp is not None and frame.timestamp_ns < previous_timestamp:
                raise ValueError(f"non-monotonic {frame.view.value} capture timestamp")
            self._last_timestamp[frame.view] = frame.timestamp_ns
            if self._pending[frame.view] is not None:
                self._dropped[frame.view] += 1
            self._pending[frame.view] = frame

    def pop_next(self) -> CapturedFrame[FramePayload] | None:
        with self._lock:
            front = self._pending[CameraView.FRONT]
            side = self._pending[CameraView.SIDE]
            if front is None and side is None:
                return None
            if front is not None and side is not None:
                selected_view = self._next_preferred
            elif front is not None:
                selected_view = CameraView.FRONT
            else:
                selected_view = CameraView.SIDE
            selected = self._pending[selected_view]
            self._pending[selected_view] = None
            self._next_preferred = (
                CameraView.SIDE
                if selected_view is CameraView.FRONT
                else CameraView.FRONT
            )
            return selected

    def dropped(self, view: CameraView) -> int:
        with self._lock:
            return self._dropped[view]

    def pending(self, view: CameraView) -> bool:
        with self._lock:
            return self._pending[view] is not None


class OpenCVCameraSource:
    """Read BGR8 frames and attach host monotonic receive timestamps."""

    def __init__(
        self,
        view: CameraView,
        device: CameraDevice,
        *,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
        fourcc: str | None = None,
        buffer_size: int | None = None,
        backend: int | None = None,
        cv2_module: Any | None = None,
        clock_ns: Callable[[], int] = monotonic_ns,
        synchronization_offset_ns: int = 0,
        rotation_degrees: int = 0,
        mirror_horizontal: bool = False,
    ) -> None:
        if width is not None and width < 1:
            raise ValueError("requested width must be positive")
        if height is not None and height < 1:
            raise ValueError("requested height must be positive")
        if fps is not None and fps <= 0.0:
            raise ValueError("requested fps must be positive")
        if fourcc is not None and len(fourcc) != 4:
            raise ValueError("requested camera fourcc must contain exactly 4 characters")
        if buffer_size is not None and buffer_size < 1:
            raise ValueError("requested camera buffer size must be positive")
        _validate_camera_transform(rotation_degrees, mirror_horizontal)

        if cv2_module is None:
            try:
                import cv2 as cv2_module  # type: ignore[import-not-found, no-redef]
            except ImportError as exc:
                raise CameraDependencyError(
                    "OpenCV capture is optional; install the project capture extra"
                ) from exc

        self.view = view
        self.device = device
        self._cv2 = cv2_module
        self._clock_ns = clock_ns
        self.synchronization_offset_ns = synchronization_offset_ns
        self.rotation_degrees = rotation_degrees
        self.mirror_horizontal = mirror_horizontal
        self._next_frame_id = 0
        self._closed = False
        self._capture = (
            cv2_module.VideoCapture(device)
            if backend is None
            else cv2_module.VideoCapture(device, backend)
        )
        if not self._capture.isOpened():
            self._capture.release()
            self._closed = True
            raise CameraOpenError(
                f"could not open {view.value} camera at device {device}"
            )

        if fourcc is not None:
            self._capture.set(
                cv2_module.CAP_PROP_FOURCC,
                float(cv2_module.VideoWriter_fourcc(*fourcc)),
            )
        # Do not force V4L2 to a single buffer by default.  On dual C920e MJPG
        # capture that disables useful capture/decode overlap and nearly halves
        # throughput.  LatestFrameScheduler remains responsible for dropping
        # stale frames.  Keep an explicit override for controlled experiments.
        if buffer_size is not None:
            buffer_size_property = getattr(cv2_module, "CAP_PROP_BUFFERSIZE", None)
            if buffer_size_property is not None:
                self._capture.set(buffer_size_property, float(buffer_size))
        settings = (
            (cv2_module.CAP_PROP_FRAME_WIDTH, width),
            (cv2_module.CAP_PROP_FRAME_HEIGHT, height),
            (cv2_module.CAP_PROP_FPS, fps),
        )
        for property_id, value in settings:
            if value is not None:
                self._capture.set(property_id, float(value))
        self.negotiated_mode = read_opencv_negotiated_mode(
            self._capture, cv2_module=cv2_module
        )

    @property
    def output_mode(self) -> CameraNegotiatedMode:
        """实际交给推理层的画面模式（包含旋转后的宽高）。"""

        return self.negotiated_mode.transformed(self.rotation_degrees)

    def read(self) -> CapturedFrame[Any]:
        if self._closed:
            raise CameraReadError("camera source is closed")
        ok, payload = self._capture.read()
        timestamp_ns = self._clock_ns()
        sync_timestamp_ns = timestamp_ns + self.synchronization_offset_ns
        if sync_timestamp_ns < 0:
            raise CameraReadError("camera synchronization offset produced a negative timestamp")
        if not ok or payload is None:
            raise CameraReadError(
                f"failed to read {self.view.value} camera at device {self.device}"
            )
        try:
            payload = transform_camera_payload(
                payload,
                cv2_module=self._cv2,
                rotation_degrees=self.rotation_degrees,
                mirror_horizontal=self.mirror_horizontal,
            )
        except Exception as exc:
            if isinstance(exc, CameraReadError):
                raise
            raise CameraReadError(
                f"failed to transform {self.view.value} camera frame"
            ) from exc
        shape = getattr(payload, "shape", None)
        if shape is None or len(shape) < 2:
            raise CameraReadError("OpenCV returned a frame without image dimensions")
        height = int(shape[0])
        width = int(shape[1])
        frame = CapturedFrame(
            view=self.view,
            frame_id=self._next_frame_id,
            timestamp_ns=timestamp_ns,
            width=width,
            height=height,
            pixel_format="bgr8",
            payload=payload,
            sync_timestamp_ns=sync_timestamp_ns,
        )
        self._next_frame_id += 1
        return frame

    def close(self) -> None:
        if not self._closed:
            self._capture.release()
            self._closed = True

    def __enter__(self) -> OpenCVCameraSource:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def read_opencv_negotiated_mode(
    capture: Any,
    *,
    cv2_module: Any,
) -> CameraNegotiatedMode:
    """读取后端实际公开模式；未知属性用 ``None`` 表示。"""

    def property_value(name: str) -> float | None:
        property_id = getattr(cv2_module, name, None)
        get = getattr(capture, "get", None)
        if property_id is None or not callable(get):
            return None
        value = float(get(property_id))
        return value if value > 0.0 else None

    width = property_value("CAP_PROP_FRAME_WIDTH")
    height = property_value("CAP_PROP_FRAME_HEIGHT")
    fps = property_value("CAP_PROP_FPS")
    fourcc_value = property_value("CAP_PROP_FOURCC")
    backend_name = "unknown"
    get_backend_name = getattr(capture, "getBackendName", None)
    if callable(get_backend_name):
        try:
            backend_name = str(get_backend_name())
        except Exception:
            pass
    return CameraNegotiatedMode(
        width=round(width) if width is not None else None,
        height=round(height) if height is not None else None,
        fps=fps,
        fourcc=(
            _decode_fourcc(round(fourcc_value))
            if fourcc_value is not None
            else None
        ),
        backend_name=backend_name,
    )


def transform_camera_payload(
    payload: Any,
    *,
    cv2_module: Any,
    rotation_degrees: int = 0,
    mirror_horizontal: bool = False,
) -> Any:
    """Apply the deployment orientation before the frame enters inference."""

    _validate_camera_transform(rotation_degrees, mirror_horizontal)
    transformed = payload
    if rotation_degrees:
        rotate = getattr(cv2_module, "rotate", None)
        if not callable(rotate):
            raise CameraReadError("OpenCV backend does not provide rotate()")
        rotate_codes = {
            90: "ROTATE_90_CLOCKWISE",
            180: "ROTATE_180",
            270: "ROTATE_90_COUNTERCLOCKWISE",
        }
        code_name = rotate_codes[rotation_degrees]
        if not hasattr(cv2_module, code_name):
            raise CameraReadError(f"OpenCV backend does not provide {code_name}")
        transformed = rotate(transformed, getattr(cv2_module, code_name))
    if mirror_horizontal:
        flip = getattr(cv2_module, "flip", None)
        if not callable(flip):
            raise CameraReadError("OpenCV backend does not provide flip()")
        transformed = flip(transformed, 1)
    return transformed


def _validate_camera_transform(rotation_degrees: int, mirror_horizontal: bool) -> None:
    if rotation_degrees not in (0, 90, 180, 270):
        raise ValueError("camera rotation must be 0, 90, 180, or 270 degrees")
    if not isinstance(mirror_horizontal, bool):
        raise ValueError("camera mirror_horizontal must be boolean")


def _decode_fourcc(value: int) -> str | None:
    characters = "".join(chr((value >> (8 * index)) & 0xFF) for index in range(4))
    return (
        characters
        if all(32 <= ord(character) <= 126 for character in characters)
        else None
    )


def _p95_nearest_rank(samples: deque[int]) -> int | None:
    """Return the nearest-rank p95 from the bounded capture-latency window."""

    if not samples:
        return None
    ordered = sorted(samples)
    rank = max(1, (95 * len(ordered) + 99) // 100)
    return ordered[rank - 1]
