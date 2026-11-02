#!/usr/bin/env python3
"""Send original dual-camera JPEG packets to an SSH receiver; never write video to SD."""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from posepriv_coach.mjpeg_recording import (
    FOOTER,
    FRONT,
    MAX_JPEG,
    METADATA,
    SIDE,
    write_json_record,
    write_record,
)


def jpeg_packet(frame) -> bytes:
    if (frame is None or frame.size == 0 or str(frame.dtype) != "uint8"
            or frame.ndim not in (1, 2) or (frame.ndim == 2 and 1 not in frame.shape)):
        raise ValueError("Camera did not return a raw MJPEG packet")
    payload = frame.tobytes()
    end = payload.rfind(b"\xff\xd9")
    if not payload.startswith(b"\xff\xd8") or end < 2 or end + 2 > MAX_JPEG:
        raise ValueError("Invalid or oversized camera JPEG")
    return payload[:end + 2]


def enqueue_frame(pending: queue.Queue, record: tuple) -> None:
    """Do not make capture wait for a slow receiver or silently drop a frame."""
    try:
        pending.put_nowait(record)
    except queue.Full as error:
        raise RuntimeError("Recording queue full: host storage or link cannot keep up") from error


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--front", required=True)
    parser.add_argument("--side", required=True)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--countdown", type=int, default=10)
    parser.add_argument("--queue-frames", type=int, default=16)
    parser.add_argument("--front-rotation", type=int, choices=(0, 90, 180, 270), default=0)
    parser.add_argument("--side-rotation", type=int, choices=(0, 90, 180, 270), default=0)
    parser.add_argument("--live-preview-config", type=Path)
    parser.add_argument("--preview-port", type=int, default=8765)
    parser.add_argument("--output-fd", type=int, choices=(1, 3), default=1,
                        help="Separate raw transport from supervisor diagnostics")
    args = parser.parse_args(argv)
    if (args.front == args.side or not 5 <= args.seconds <= 600
            or not 0 <= args.countdown <= 30 or not 2 <= args.queue_frames <= 64
            or not 1024 <= args.preview_port <= 65535):
        parser.error("Use distinct cameras, 5..600 seconds, countdown 0..30 and queue 2..64")
    import cv2

    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"Signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    caps = {}
    workers = []
    owned_views = set()
    stop = threading.Event()
    start = threading.Event()
    finished = {view: threading.Event() for view in ("front", "side")}
    pending = queue.Queue(maxsize=args.queue_frames)
    errors = []
    results = {}
    settings = {}
    preview = None
    origin = {}
    output = os.fdopen(os.dup(args.output_fd), "wb")

    def read(view):
        ok, raw = caps[view].read()
        timestamp = time.monotonic_ns()
        if not ok:
            raise ValueError("Camera read failed")
        return jpeg_packet(raw), timestamp

    def capture(view, kind):
        frames = total = discarded = consecutive_bad = 0
        try:
            start.wait()
            while not stop.is_set() and time.monotonic() < origin["deadline"]:
                try:
                    payload, timestamp = read(view)
                except ValueError:
                    discarded += 1
                    consecutive_bad += 1
                    if consecutive_bad >= 30:
                        raise RuntimeError(f"{view}: 30 consecutive unusable MJPEG frames")
                    continue
                consecutive_bad = 0
                enqueue_frame(pending, (kind, frames, timestamp, payload))
                if preview is not None:
                    preview.submit(view, frames, timestamp, payload)
                frames += 1
                total += len(payload)
            results[view] = {"frames": frames, "bytes": total, "discarded_frames": discarded}
        except BaseException as error:  # noqa: BLE001 -- handed to the owner thread and re-raised
            errors.append(error)
            stop.set()
        finally:
            try:
                caps[view].release()
            except BaseException as error:  # noqa: BLE001 -- release errors also fail the owner thread
                errors.append(error)
                stop.set()
            finally:
                finished[view].set()

    try:
        if args.live_preview_config is not None:
            from posepriv_coach.field_preview import LivePreview
            preview = LivePreview(args.live_preview_config, args.preview_port,
                                  args.front_rotation, args.side_rotation)
        print(f"Recording starts after camera setup and {args.countdown}s countdown", file=sys.stderr, flush=True)
        for view, device in (("front", args.front), ("side", args.side)):
            cap = caps[view] = cv2.VideoCapture(device, cv2.CAP_V4L2)
            if not cap.isOpened():
                raise RuntimeError(f"Cannot open {view}: {device}")
            for prop, value in ((cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG")),
                                (cv2.CAP_PROP_FRAME_WIDTH, 1280), (cv2.CAP_PROP_FRAME_HEIGHT, 720),
                                (cv2.CAP_PROP_FPS, 30), (cv2.CAP_PROP_CONVERT_RGB, 0)):
                cap.set(prop, value)
            settings[view] = {key: cap.get(prop) for key, prop in (
                ("width", cv2.CAP_PROP_FRAME_WIDTH), ("height", cv2.CAP_PROP_FRAME_HEIGHT),
                ("fps", cv2.CAP_PROP_FPS), ("buffers", cv2.CAP_PROP_BUFFERSIZE),
                ("fourcc", cv2.CAP_PROP_FOURCC))}
            if (settings[view]["width"] != 1280 or settings[view]["height"] != 720
                    or settings[view]["fourcc"] != cv2.VideoWriter_fourcc(*"MJPG")
                    or settings[view]["fps"] != 30):
                raise RuntimeError(f"{view}: camera did not accept 1280x720@30 MJPG")
            good = 0
            for _ in range(35):
                try:
                    read(view)
                    good += 1
                except ValueError:
                    pass
                if good == 5:
                    break
            if good != 5:
                raise RuntimeError(f"{view}: MJPEG warmup failed")
        time.sleep(args.countdown)
        metadata = {"source_width": 1280, "source_height": 720,
                    "duration_requested_seconds": args.seconds,
                    "front_device": args.front, "side_device": args.side,
                    "front_rotation_degrees": args.front_rotation,
                    "side_rotation_degrees": args.side_rotation,
                    "camera_settings": settings, "includes_pose_inference": False,
                    "source_boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                    "timestamp_semantics": "board_monotonic_after_raw_mjpeg_read_not_hardware_exposure_sync"}
        write_json_record(output, METADATA, metadata)
        output.flush()
        for view, kind in (("front", FRONT), ("side", SIDE)):
            worker = threading.Thread(target=capture, args=(view, kind), daemon=True)
            worker.start()
            workers.append(worker)
            owned_views.add(view)
        origin["deadline"] = time.monotonic() + args.seconds
        start.set()
        print("RECORDING STARTED", file=sys.stderr, flush=True)
        high_water = 0
        while not all(event.is_set() for event in finished.values()) or not pending.empty():
            if errors:
                raise errors[0]
            if time.monotonic() > origin["deadline"] + 10:
                raise TimeoutError("Camera did not finish and release within 10 seconds")
            high_water = max(high_water, pending.qsize())
            try:
                record = pending.get(timeout=0.1)
            except queue.Empty:
                continue
            write_record(output, *record)
        if errors:
            raise errors[0]
        if any(not results[view]["frames"] for view in finished):
            raise RuntimeError("A camera produced no frames")
        preview_result = preview.close() if preview is not None else None
        preview = None
        if preview_result is not None:
            print("LIVE PREVIEW " + json.dumps(preview_result), file=sys.stderr, flush=True)
        write_json_record(output, FOOTER, {"status": "complete", "streams": results,
                          "diagnostics": {"queue_capacity_frames": args.queue_frames,
                                          "queue_high_water_frames": high_water,
                                          "buffer_policy": "backend_default",
                                          "live_preview": preview_result}})
        output.flush()
        print(json.dumps(results), file=sys.stderr, flush=True)
        return 0
    finally:
        stop.set()
        start.set()
        for worker in workers:
            worker.join(timeout=2)
        for view, cap in caps.items():
            if view not in owned_views:
                cap.release()
        if preview is not None:
            preview.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as error:
        print(f"RECORDING FAILED: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error
