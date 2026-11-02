# SPDX-License-Identifier: MIT
"""Explicit display-only DPV2/DPT2 transport; default production v1 is unchanged."""
from __future__ import annotations

from typing import Any
from .contracts import CameraView, PoseFrame
from .comparison_display import OpenCVComparisonRenderer
from .native_camera_preview import NativePreviewPairSource
from .skeleton_display import SkeletonDisplayError

from dataclasses import dataclass, replace
import errno
import mmap
import os
import struct
import sys
from threading import Lock

GLOBAL = struct.Struct("<4sIIIIIIIIIII16x")
SLOT = struct.Struct("<4sIQQIIIIIIII8x")


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"invalid {name}")
    return value


def box(vw, vh):
    width = round((vw - 66) * .48) - 6
    height = ((vh - 142 - 64 - 12 - 46 - 10) // 2) - 22
    return width, height


def fit(sw, sh, vw, vh):
    width, height = box(vw, vh)
    scale = min(width / sw, height / sh)
    return max(1, round(sw * scale)), max(1, round(sh * scale))


@dataclass(frozen=True)
class Layout:
    viewport_width: int = 800
    viewport_height: int = 600
    max_source_width: int = 4096
    max_source_height: int = 2160
    slots: int = 4

    def __post_init__(self):
        integer(self.viewport_width, 800, 4096, "viewport width")
        integer(self.viewport_height, 600, 4096, "viewport height")
        integer(self.slots, 1, 8, "slots")
        integer(self.max_source_width, 1, 4096, "source max width")
        integer(self.max_source_height, 1, 2160, "source max height")
        if self.total_bytes > 256 * 1024 * 1024:
            raise ValueError("ring exceeds 256MiB")

    @property
    def capacity(self):
        width, height = box(self.viewport_width, self.viewport_height)
        return width * height * 3

    @property
    def span(self):
        return (64 + self.capacity + 63) // 64 * 64

    @property
    def total_bytes(self):
        return 64 + 2 * self.slots * self.span

    def header(self):
        return GLOBAL.pack(b"DPV2", 2, self.slots, self.viewport_width, self.viewport_height,
            self.max_source_width, self.max_source_height, self.capacity, self.span,
            self.total_bytes, 1, 2)

    def offset(self, view, frame_id):
        integer(view, 0, 1, "view")
        integer(frame_id, 0, 2**64 - 1, "frame id")
        return 64 + (view * self.slots + frame_id % self.slots) * self.span


@dataclass(frozen=True)
class PrivateDisplayFrame:
    view: int
    frame_id: int
    capture_timestamp_ns: int
    sync_offset_ns: int
    source_width: int
    source_height: int
    payload_width: int
    payload_height: int
    payload: bytes


class PreviewV2Error(RuntimeError):
    pass


class Ring:
    """Own one sealed memfd, one mapping, one process attachment; no mmap views."""
    def __init__(self, layout):
        if not sys.platform.startswith("linux") or not hasattr(os, "memfd_create"):
            raise PreviewV2Error("Linux memfd required")
        import fcntl
        if not isinstance(layout, Layout):
            raise TypeError("Layout required")
        self.layout, self._fcntl = layout, fcntl
        self._lock = Lock()
        self._closed = self._failed = False
        self._attempts = self._matched = self._missing = self._busy = 0
        self._mapping = None
        self._fd = os.memfd_create("posepriv-display-preview-v2", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        try:
            if self._fd < 3:
                raise PreviewV2Error("memfd cannot occupy stdio")
            os.ftruncate(self._fd, layout.total_bytes)
            self._mapping = mmap.mmap(self._fd, layout.total_bytes, flags=mmap.MAP_SHARED,
                                       prot=mmap.PROT_READ | mmap.PROT_WRITE)
            self._mapping[:64] = layout.header()
            fcntl.fcntl(self._fd, fcntl.F_ADD_SEALS,
                       fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        except BaseException:
            if self._mapping is not None:
                self._mapping.close()
            os.close(self._fd)
            raise

    def _require_open(self):
        if self._closed or self._failed:
            raise PreviewV2Error("ring is closed or previously failed")

    def fileno(self):
        with self._lock:
            self._require_open()
            return self._fd

    def read_synced(self, view, frame_id, sync_timestamp_ns, source_width, source_height, *, offset_ns=0):
        offset = self.layout.offset(view, frame_id)
        integer(sync_timestamp_ns, 0, 2**64 - 1, "sync timestamp")
        integer(offset_ns, -(2**63), 2**63 - 1, "sync offset")
        capture_time = sync_timestamp_ns - offset_ns
        integer(capture_time, 0, 2**64 - 1, "restored capture timestamp")
        integer(source_width, 1, self.layout.max_source_width, "source width")
        integer(source_height, 1, self.layout.max_source_height, "source height")
        with self._lock:
            self._require_open()
            self._attempts += 1
            try:
                if self._mapping[:64] != self.layout.header():
                    raise PreviewV2Error("global v2 layout changed")
                try:
                    self._fcntl.lockf(self._fd, self._fcntl.LOCK_SH | self._fcntl.LOCK_NB,
                                     self.layout.span, offset, os.SEEK_SET)
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    self._busy += 1
                    return None
                try:
                    header = self._mapping[offset:offset + 64]
                    if header[:4] == bytes(4):
                        self._missing += 1
                        return None
                    magic, number, actual_id, raw_time, sw, sh, pw, ph, stride, count, vw, vh = SLOT.unpack(header)
                    if (magic != b"DPT2" or number not in (0, 1) or any(header[56:])
                            or not 1 <= sw <= self.layout.max_source_width
                            or not 1 <= sh <= self.layout.max_source_height
                            or (vw, vh) != (self.layout.viewport_width, self.layout.viewport_height)
                            or (pw, ph) != fit(sw, sh, vw, vh)
                            or stride != pw * 3 or count != stride * ph
                            or count > self.layout.capacity):
                        raise PreviewV2Error("invalid published v2 slot")
                    if (number, actual_id, raw_time, sw, sh) != (
                            view, frame_id, capture_time, source_width, source_height):
                        self._missing += 1
                        return None
                    # Exactly one private immutable payload copy under lock.
                    payload = self._mapping[offset + 64:offset + 64 + count]
                finally:
                    self._fcntl.lockf(self._fd, self._fcntl.LOCK_UN, self.layout.span, offset, os.SEEK_SET)
                self._matched += 1
                return PrivateDisplayFrame(view, frame_id, raw_time, offset_ns, sw, sh, pw, ph, payload)
            except BaseException:
                self._failed = True
                raise

    def read(self, view, frame_id, capture_timestamp_ns, source_width, source_height):
        """Return an explicit prepared snapshot for an exact raw capture identity."""
        if not isinstance(view, CameraView):
            raise ValueError("CameraView required")
        frame = self.read_synced(0 if view is CameraView.FRONT else 1, frame_id,
            capture_timestamp_ns, source_width, source_height)
        if frame is None:
            return None
        try:
            return prepare_frame(frame, self.layout)
        except BaseException:
            with self._lock:
                self._failed = True
            raise

    def stats(self):
        with self._lock:
            return {"protocol": "native_preview_ring.v2", "ring_bytes": self.layout.total_bytes,
                    "attempts": self._attempts, "matched": self._matched, "missing": self._missing,
                    "busy": self._busy, "failed": self._failed, "closed": self._closed,
                    "scope": "private_display_snapshots_NOT_DPU_PL_camera_or_HDMI",
                    "pixels_persisted": False}

    def close(self):
        with self._lock:
            if not self._closed:
                try:
                    self._mapping.close()
                finally:
                    os.close(self._fd)
                    self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def content_rect(viewport_width, viewport_height, view):
    integer(viewport_width, 800, 4096, "viewport_width")
    integer(viewport_height, 600, 4096, "viewport_height")
    if not isinstance(view, CameraView):
        raise ValueError("CameraView required")
    # Frozen equal front/side slots from the current renderer. Tests compare
    # the FULL real renderer; this is not independently accepted UI layout.
    live_width = round((viewport_width - 48 - 18) * .48)
    content_height = (viewport_height - 142) - 64 - 12
    slot_height = (content_height - 46 - 10) // 2
    index = 0 if view is CameraView.FRONT else 1
    return (27, 64 + 43 + index * (slot_height + 10) + 22,
            live_width - 6, slot_height - 22)


def fit_geometry(source_width, source_height, rect):
    integer(source_width, 1, 4096, "source_width")
    integer(source_height, 1, 2160, "source_height")
    x, y, width, height = rect
    if width < 1 or height < 1:
        raise ValueError("positive content rect required")
    # Exactly original source geometry, Python binary64 and ties-to-even.
    scale = min(width / source_width, height / source_height)
    target_width = max(1, round(source_width * scale))
    target_height = max(1, round(source_height * scale))
    return (x + (width - target_width) // 2, y + (height - target_height) // 2,
            target_width, target_height)


@dataclass(frozen=True, slots=True)
class PreparedPreview:
    view: CameraView
    frame_id: int
    timestamp_ns: int
    source_width: int
    source_height: int
    viewport_width: int
    viewport_height: int
    pixels: Any
    timestamp_offset_ns: int = 0


def make_slot(source_bytes, pose, viewport_width, viewport_height, *, source_stride=None):
    """Model producer-side resize of private deterministic source, never DPU input."""
    import cv2
    import numpy as np
    if type(source_bytes) is not bytes or not isinstance(pose, PoseFrame):
        raise TypeError("immutable source bytes and PoseFrame required")
    validate_pose(pose)
    integer(pose.frame_id, 0, 2**64 - 1, "frame_id")
    integer(pose.timestamp_ns, 0, 2**64 - 1, "timestamp_ns")
    rect = fit_geometry(pose.source_width, pose.source_height,
                        content_rect(viewport_width, viewport_height, pose.view))
    stride = pose.source_width * 3 if source_stride is None else source_stride
    integer(stride, pose.source_width * 3, 2**32 - 1, "source_stride")
    span = (pose.source_height - 1) * stride + pose.source_width * 3
    if len(source_bytes) < span:
        raise ValueError("source bytes shorter than active span")
    source = np.ndarray((pose.source_height, pose.source_width, 3), dtype=np.uint8,
                        buffer=source_bytes, strides=(stride, 3, 1))
    pixels = cv2.resize(source, rect[2:])
    payload = pixels.tobytes()
    header = SLOT.pack(b"DPT2", 0 if pose.view is CameraView.FRONT else 1,
        pose.frame_id, pose.timestamp_ns, pose.source_width, pose.source_height,
        rect[2], rect[3], rect[2] * 3, len(payload), viewport_width, viewport_height)
    return header, payload


def decode_slot(header, payload, expected_pose, viewport_width, viewport_height, *, timestamp_offset_ns=0):
    """Decode an already PRIVATE immutable copy; not a memfd/record-lock reader.

    Well-formed different identity/source dimensions returns missing, like v1.
    Malformed structure/layout is fatal. No arbitrary latest image fallback.
    """
    import numpy as np
    if type(header) is not bytes or type(payload) is not bytes or len(header) != SLOT.size:
        raise ValueError("exact immutable header/payload required")
    fields = SLOT.unpack(header)
    magic, view, frame, timestamp, sw, sh, pw, ph, stride, count, vw, vh = fields
    if magic != b"DPT2" or view not in (0, 1) or any(header[56:]):
        raise ValueError("invalid v2 magic/view/reserved bytes; v1 is not accepted")
    actual_view = CameraView.FRONT if view == 0 else CameraView.SIDE
    rect = fit_geometry(sw, sh, content_rect(vw, vh, actual_view))
    if (pw, ph) != rect[2:] or stride != pw * 3 or count != stride * ph or len(payload) != count:
        raise ValueError("invalid derived geometry/stride/payload count")
    if (vw, vh) != (viewport_width, viewport_height):
        raise ValueError("unexpected frozen viewport")
    if not isinstance(expected_pose, PoseFrame):
        raise TypeError("PoseFrame required")
    validate_pose(expected_pose)
    integer(timestamp_offset_ns, -(2**63), 2**63 - 1, "timestamp_offset_ns")
    capture_timestamp = expected_pose.timestamp_ns - timestamp_offset_ns
    integer(capture_timestamp, 0, 2**64 - 1, "restored_capture_timestamp")
    if (actual_view, frame, timestamp, sw, sh) != (
            expected_pose.view, expected_pose.frame_id, capture_timestamp,
            expected_pose.source_width, expected_pose.source_height):
        return None
    pixels = np.frombuffer(payload, dtype=np.uint8).reshape(ph, pw, 3)
    return PreparedPreview(actual_view, frame, timestamp, sw, sh, vw, vh, pixels, timestamp_offset_ns)



def validate_pose(pose):
    """Validate metadata before equality (bool is not an integer identity)."""
    if not isinstance(pose, PoseFrame):
        raise TypeError("PoseFrame required")
    if not isinstance(pose.view, CameraView):
        raise ValueError("CameraView required")
    integer(pose.frame_id, 0, 2**64 - 1, "frame_id")
    integer(pose.timestamp_ns, 0, 2**64 - 1, "timestamp_ns")
    integer(pose.source_width, 1, 4096, "source_width")
    integer(pose.source_height, 1, 2160, "source_height")


def prepare_frame(frame, layout):
    """Wrap the locked reader's private bytes; never make a second pixel copy."""
    if type(frame) is not PrivateDisplayFrame or type(frame.payload) is not bytes:
        raise TypeError("private immutable display frame required")
    if not isinstance(layout, Layout):
        raise TypeError("Layout required")
    integer(frame.source_width, 1, layout.max_source_width, "source_width")
    integer(frame.source_height, 1, layout.max_source_height, "source_height")
    actual_view = CameraView.FRONT if frame.view == 0 else CameraView.SIDE
    integer(frame.view, 0, 1, "view")
    pose = PoseFrame(actual_view, frame.frame_id,
        frame.capture_timestamp_ns + frame.sync_offset_ns, {},
        frame.source_width, frame.source_height)
    header = SLOT.pack(b"DPT2", frame.view, frame.frame_id, frame.capture_timestamp_ns,
        frame.source_width, frame.source_height, frame.payload_width, frame.payload_height,
        frame.payload_width * 3, len(frame.payload),
        layout.viewport_width, layout.viewport_height)
    return decode_slot(header, frame.payload, pose,
        layout.viewport_width, layout.viewport_height,
        timestamp_offset_ns=frame.sync_offset_ns)


def validate_prepared(preview, pose, width, height):
    if type(preview) is not PreparedPreview:
        raise SkeletonDisplayError("explicit v2 PreparedPreview required, not implicit v1 ndarray")
    try:
        validate_pose(pose)
        integer(preview.frame_id, 0, 2**64 - 1, "frame_id")
        integer(preview.timestamp_ns, 0, 2**64 - 1, "capture timestamp")
        integer(preview.timestamp_offset_ns, -(2**63), 2**63 - 1, "timestamp offset")
        integer(preview.source_width, 1, 4096, "source_width")
        integer(preview.source_height, 1, 2160, "source_height")
        content_rect(preview.viewport_width, preview.viewport_height, preview.view)
    except (ValueError, TypeError) as exc:
        raise SkeletonDisplayError("invalid prepared preview metadata") from exc
    if (preview.view, preview.frame_id, preview.timestamp_ns + preview.timestamp_offset_ns,
            preview.source_width, preview.source_height) != (
            pose.view, pose.frame_id, pose.timestamp_ns, pose.source_width, pose.source_height):
        raise SkeletonDisplayError("prepared preview does not match exact source identity")
    if (preview.viewport_width, preview.viewport_height) != (width, height):
        raise SkeletonDisplayError("prepared preview frozen viewport changed")


def fit_prepared_image(renderer, canvas, image, rect):
    """Copy exactly the fitted display ROI; use original-source overlay geometry."""
    if type(image) is not PreparedPreview:
        raise SkeletonDisplayError("explicit v2 PreparedPreview required, not implicit v1 ndarray")
    fitted = fit_geometry(image.source_width, image.source_height, rect)
    x, y, width, height = fitted
    pixels = image.pixels
    if (not isinstance(pixels, renderer._numpy.ndarray)
            or pixels.dtype != renderer._numpy.uint8 or pixels.shape != (height, width, 3)
            or pixels.flags.writeable or not pixels.flags.c_contiguous):
        raise SkeletonDisplayError("prepared pixels require exact immutable fitted geometry")
    owner = pixels
    while isinstance(owner.base, renderer._numpy.ndarray):
        owner = owner.base
    if type(owner.base) is not bytes or owner.flags.writeable:
        raise SkeletonDisplayError("prepared pixels require genuine immutable private bytes backing")
    canvas[y:y + height, x:x + width] = pixels
    return fitted


def make_preview_renderer(base):
    """Explicit v2 seam. No default replacement; only live-panel drawing changes."""
    if not isinstance(base, type) or not issubclass(base, OpenCVComparisonRenderer):
        raise TypeError("OpenCVComparisonRenderer subclass required")

    class PreparedRenderer(base):
        def _draw_live_panel(self, canvas, rect, front_pose, side_pose,
                             front_preview, side_preview):
            # Validate both views before any live-panel publication/drawing.
            for pose, preview in ((front_pose, front_preview), (side_pose, side_preview)):
                if preview is not None:
                    validate_prepared(preview, pose, self.width, self.height)
            x, y, width, height = rect
            self._draw_panel_shell(canvas, rect, "CAMERA VIEWS")
            slot_gap, label_height = 10, 22
            slot_height = (height - 46 - slot_gap) // 2
            for index, (label, pose, preview) in enumerate((
                    ("FRONT", front_pose, front_preview), ("SIDE", side_pose, side_preview))):
                slot_top = y + 43 + index * (slot_height + slot_gap)
                content = (x + 3, slot_top + label_height, width - 6,
                           slot_height - label_height)
                if preview is not None:
                    if content != content_rect(self.width, self.height, pose.view):
                        raise SkeletonDisplayError("live layout differs from frozen v2 layout")
                    image_rect = fit_prepared_image(self, canvas, preview, content)
                    self._draw_pose_overlay(canvas, pose, image_rect)
                else:
                    self._draw_preview_placeholder(canvas, content, pose)
                self._cv2.putText(canvas, label, (x + 11, slot_top + 17),
                    self._cv2.FONT_HERSHEY_SIMPLEX, .45, (255, 255, 255), 1, self._cv2.LINE_AA)

    return PreparedRenderer


PreparedPreviewRenderer = make_preview_renderer(OpenCVComparisonRenderer)


class PairSource(NativePreviewPairSource):
    """Reuse v1 bounded timing/lookup accounting, but return explicit v2 metadata."""
    def __init__(self, ring, *, front_offset_ms=0, side_offset_ms=0, **kwargs):
        if not isinstance(ring, Ring):
            raise TypeError("display-v2 Ring required")
        if type(front_offset_ms) not in (int, float) or type(side_offset_ms) not in (int, float):
            raise ValueError("numeric synchronization offsets required")
        super().__init__(ring, front_offset_ms=front_offset_ms,
                         side_offset_ms=side_offset_ms, **kwargs)
        integer(self._front_offset_ns, -(2**63), 2**63 - 1, "front offset")
        integer(self._side_offset_ns, -(2**63), 2**63 - 1, "side offset")

    def read_pair(self, pair):
        # Only scalars: permit rollback if post-lookup metadata fails, without
        # copying the bounded latency window or retaining image payloads.
        previous_total, previous_max = self._total_ns, self._max_ns
        evicted = self._samples[0] if len(self._samples) == self._samples.maxlen else None
        result = super().read_pair(pair)
        if result is None:
            return None
        try:
            front, side = result
            if type(front) is not PreparedPreview or type(side) is not PreparedPreview:
                raise PreviewV2Error("pair source requires exact prepared snapshots")
            prepared = (replace(front, timestamp_offset_ns=self._front_offset_ns),
                        replace(side, timestamp_offset_ns=self._side_offset_ns))
            for preview, pose in zip(prepared, (pair.front, pair.side)):
                validate_prepared(preview, pose, preview.viewport_width, preview.viewport_height)
            return prepared
        except BaseException:
            # Match inherited fault accounting: only successful/missing pairs
            # enter its latency statistics. Stop future reads after the fault.
            self._matched -= 1
            self._faults += 1
            self._samples.pop()
            if evicted is not None:
                self._samples.appendleft(evicted)
            self._total_ns, self._max_ns = previous_total, previous_max
            raise

    def stats(self):
        result = super().stats()
        result["scope"] = "lookups_private_display_copies_before_sync_metadata_wrap_NOT_render_or_scanout"
        return result
