#!/usr/bin/env python3
"""Show dual-camera inference without recording; speech output is explicit opt-in."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import queue
import signal
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from scripts.board.stream_dual_mjpeg import jpeg_packet


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--front", required=True, help="Stable /dev/v4l/by-path device")
    result.add_argument("--side", required=True, help="Stable /dev/v4l/by-path device")
    result.add_argument("--config", required=True, type=Path,
                        help="Existing LivePreview JSON with hdmi_display=true")
    result.add_argument("--seconds", type=float, default=0,
                        help="0 runs until interrupted; a positive value limits capture duration")
    result.add_argument("--front-rotation", type=int, choices=(0, 90, 180, 270), default=0)
    result.add_argument("--side-rotation", type=int, choices=(0, 90, 180, 270), default=0)
    result.add_argument("--port", type=int, default=None,
                        help="Optional loopback HTTP diagnostics; omitted means no HTTP server")
    result.add_argument("--report", type=Path, help="Write capture and preview results as JSON")
    result.add_argument("--speak-feedback", action="store_true", help="Enable local Chinese output; no microphone capture")
    result.add_argument("--speech-volume", type=int, default=-80,
                        help="Speech Dispatcher relative volume -100..100; default -80, 0 is normal (not hardware percent)")
    return result


def _validate(args) -> None:
    devices = (Path(args.front), Path(args.side))
    if any(p.parent != Path('/dev/v4l/by-path') or not p.name for p in devices):
        raise ValueError('Use two stable /dev/v4l/by-path camera paths')
    if devices[0].resolve() == devices[1].resolve():
        raise ValueError('Front and side must be distinct cameras')
    if not math.isfinite(args.seconds) or args.seconds < 0:
        raise ValueError('seconds must be 0 or a finite positive duration')
    if args.port is not None and not 1024 <= args.port <= 65535:
        raise ValueError('port must be 1024..65535 or omitted')
    if not -100<=args.speech_volume<=100:
        raise ValueError('speech-volume must be -100..100')
    config = json.loads(args.config.read_text(encoding='utf-8'))
    if config.get('hdmi_display') is not True:
        raise ValueError('HDMI camera preview requires hdmi_display=true in --config')
    if config.get('recorded_input', False):
        raise ValueError('HDMI camera preview requires a live-input config')


def _latest(pending, record) -> int:
    """One producer per view replaces only an unconsumed preview frame."""
    try:
        pending.put_nowait(record)
        return 0
    except queue.Full:
        try:
            pending.get_nowait()
            dropped = 1
        except queue.Empty:
            dropped = 0
        pending.put_nowait(record)
        return dropped


def run(args, *, cv2_module=None, capture_factory=None, preview_factory=None,
        stopped=None) -> dict:
    """Camera threads own captures; this calling thread alone submits to preview."""
    _validate(args)
    if cv2_module is None:
        import cv2 as cv2_module
    if capture_factory is None:
        capture_factory = cv2_module.VideoCapture
    if preview_factory is None:
        from posepriv_coach.field_preview import LivePreview
        preview_factory = LivePreview
    stop = stopped if stopped is not None else threading.Event()
    start = threading.Event()
    changed = threading.Event()
    ready = {v: threading.Event() for v in ('front', 'side')}
    pending = {v: queue.Queue(maxsize=1) for v in ready}
    errors = queue.Queue()
    streams = {v: dict(device=device, frames=0, source_reads=0, bytes=0, preview_replaced=0,
                       discarded_frames=0, discarded_reasons={}, consecutive_discarded=0,
                       max_consecutive_discarded=0, last_source_frame_id=None,
                       first_source_timestamp_ns=None, last_source_timestamp_ns=None,
                       released=False, first_timestamp_ns=None, last_timestamp_ns=None,
                       last_frame_id=None, max_retrieve_interval_ns=0, settings={})
               for v, device in (('front', args.front), ('side', args.side))}
    submitted = dict(front=0, side=0)
    threads = []
    failures = []
    preview = None
    preview_result = None
    preview_error = None
    started_ns = None
    completed_by_duration = False
    interrupted = False

    def capture(view):
        cap = None
        stats = streams[view]
        try:
            cv2 = cv2_module
            cap = capture_factory(stats['device'], cv2.CAP_V4L2)
            if not cap.isOpened():
                raise RuntimeError(f'Cannot open {view}: {stats["device"]}')
            fourcc = cv2.VideoWriter_fourcc(*'MJPG')
            for prop, value in ((cv2.CAP_PROP_FOURCC, fourcc),
                                (cv2.CAP_PROP_FRAME_WIDTH, 1280),
                                (cv2.CAP_PROP_FRAME_HEIGHT, 720),
                                (cv2.CAP_PROP_FPS, 30),
                                (cv2.CAP_PROP_CONVERT_RGB, 0)):
                cap.set(prop, value)
            # C920/V4L2 can return raw JPEG with FORMAT=16 and reject FORMAT=-1.
            # The per-frame jpeg_packet check is authoritative for raw delivery.
            stats['raw_format_request'] = dict(value=-1, accepted=False)
            try:
                stats['raw_format_request']['accepted'] = bool(cap.set(cv2.CAP_PROP_FORMAT, -1))
            except Exception as exc:
                stats['raw_format_request']['error'] = f'{type(exc).__name__}: {exc}'
            stats['settings'] = {key: cap.get(prop) for key, prop in (
                ('width', cv2.CAP_PROP_FRAME_WIDTH), ('height', cv2.CAP_PROP_FRAME_HEIGHT),
                ('fps', cv2.CAP_PROP_FPS), ('fourcc', cv2.CAP_PROP_FOURCC),
                ('format', cv2.CAP_PROP_FORMAT), ('convert_rgb', cv2.CAP_PROP_CONVERT_RGB))}
            expected = dict(width=1280, height=720, fps=30, fourcc=fourcc, convert_rgb=0)
            if any(stats['settings'][key] != value for key, value in expected.items()):
                raise RuntimeError(f'{view}: camera did not accept raw 1280x720@30 MJPG')
            ready[view].set()
            start.wait()
            while not stop.is_set():
                if not cap.grab():
                    if stop.is_set():
                        break
                    raise RuntimeError(f'{view}: camera grab failed')
                ok, raw = cap.retrieve()
                timestamp = time.monotonic_ns()
                if not ok:
                    if stop.is_set():
                        break
                    raise RuntimeError(f'{view}: camera retrieve failed')
                # A successful source read always occupies an ID, even when its JPEG is bad.
                frame_id = stats['source_reads']
                stats['source_reads'] += 1
                stats['last_source_frame_id'] = frame_id
                if stats['last_source_timestamp_ns'] is not None:
                    stats['max_retrieve_interval_ns'] = max(stats['max_retrieve_interval_ns'],
                        timestamp-stats['last_source_timestamp_ns'])
                stats['last_source_timestamp_ns'] = timestamp
                if stats['first_source_timestamp_ns'] is None:
                    stats['first_source_timestamp_ns'] = timestamp
                try:
                    payload = jpeg_packet(raw)
                except ValueError as exc:
                    stats['discarded_frames'] += 1
                    stats['consecutive_discarded'] += 1
                    stats['max_consecutive_discarded'] = max(stats['max_consecutive_discarded'],
                                                             stats['consecutive_discarded'])
                    reason = str(exc)
                    stats['discarded_reasons'][reason] = stats['discarded_reasons'].get(reason, 0) + 1
                    if stats['consecutive_discarded'] >= 30:
                        raise RuntimeError(f'{view}: 30 consecutive unusable MJPEG frames: {exc}') from exc
                    continue
                stats['consecutive_discarded'] = 0
                stats['frames'] += 1
                stats['bytes'] += len(payload)
                stats['last_frame_id'] = frame_id
                stats['last_timestamp_ns'] = timestamp
                if stats['first_timestamp_ns'] is None:
                    stats['first_timestamp_ns'] = timestamp
                stats['preview_replaced'] += _latest(pending[view], (frame_id, timestamp, payload))
                changed.set()
        except BaseException as exc:
            stats['error'] = f'{type(exc).__name__}: {exc}'
            errors.put(stats['error'])
            stop.set()
        finally:
            if cap is not None:
                try:
                    cap.release()
                    stats['released'] = True
                except BaseException as exc:
                    message = f'{view} release: {type(exc).__name__}: {exc}'
                    stats['release_error'] = message
                    errors.put(message)
                    stop.set()
            ready[view].set()
            changed.set()

    try:
        speech_options=dict(speech_volume=args.speech_volume)if args.speak_feedback else{}
        preview = preview_factory(args.config, args.port,
                                  args.front_rotation, args.side_rotation,**speech_options)
        for view in ready:
            worker = threading.Thread(target=capture, args=(view,),
                                      name=f'posepriv-camera-{view}', daemon=True)
            threads.append(worker)
            worker.start()
        while not all(event.is_set() for event in ready.values()) and not stop.is_set():
            changed.wait(.05)
            changed.clear()
        if not stop.is_set():
            started_ns = time.monotonic_ns()
            deadline = None if args.seconds == 0 else time.monotonic() + args.seconds
            start.set()
            while not stop.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    completed_by_duration = True
                    break
                process = getattr(preview, 'process', None)
                if process is not None and not process.is_alive():
                    preview_error = f'Preview child exited {process.exitcode}'
                    break
                changed.clear()
                for view in ready:
                    try:
                        record = pending[view].get_nowait()
                    except queue.Empty:
                        continue
                    preview.submit(view, *record)
                    submitted[view] += 1
                changed.wait(.05 if deadline is None else min(.05, max(0, deadline-time.monotonic())))
    except KeyboardInterrupt:
        interrupted = True
    except Exception as exc:
        preview_error = f'{type(exc).__name__}: {exc}'
    finally:
        stop.set()
        start.set()
        join_deadline = time.monotonic() + 10
        for worker in threads:
            if worker.ident is not None:
                worker.join(timeout=max(0, join_deadline - time.monotonic()))
                if worker.is_alive():
                    failures.append(f'{worker.name}: camera owner did not stop within 10 seconds')
        while not errors.empty():
            failures.append(errors.get_nowait())
        if preview is not None:
            try:
                preview_result = preview.close()
            except Exception as exc:
                preview_error = f'{type(exc).__name__}: {exc}'
    finished_ns = time.monotonic_ns()
    for stats in streams.values():
        span = ((stats['last_timestamp_ns'] - stats['first_timestamp_ns'])
                if stats['frames'] > 1 else 0)
        stats['capture_timestamp_fps'] = (stats['frames'] - 1) * 1e9 / span if span > 0 else None
        source_span = ((stats['last_source_timestamp_ns'] - stats['first_source_timestamp_ns'])
                       if stats['source_reads'] > 1 else 0)
        stats['source_timestamp_fps'] = ((stats['source_reads'] - 1) * 1e9 / source_span
                                         if source_span > 0 else None)
    capture_status = 'failed' if failures else 'complete' if completed_by_duration else 'stopped'
    preview_failed = (preview_error is not None or
                      preview_result is None or preview_result.get('status') != 'complete' or
                      preview_result.get('child_exit_code', 0) != 0)
    status = 'failed' if failures or preview_failed else 'interrupted' if interrupted else capture_status
    return dict(schema=1, mode='hdmi_live_camera', status=status,
                duration_requested_seconds=args.seconds, started_ns=started_ns, finished_ns=finished_ns,
                timestamp_semantics='board_monotonic_after_raw_mjpeg_retrieve_not_hardware_exposure_sync',
                capture=dict(status=capture_status, streams=streams, errors=failures,
                    frame_id_semantics='zero_based_successful_grab_retrieve_including_invalid_jpeg_reads',
                    frames_semantics='valid_JPEG_count; last_frame_id_is_last_valid_source_ID'),
                preview=dict(result=preview_result, error=preview_error,
                             submitted_inputs=submitted, queue_capacity_per_view=1,
                             pending_on_close={v: q.qsize() for v, q in pending.items()}),
                http_diagnostics_port=args.port, recording=False, audio=args.speak_feedback,
                audio_recording=False,speech_feedback_requested=args.speak_feedback,
                audibility_verified=False)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    stop = threading.Event()
    received_signals = []

    def interrupted(signum, _frame):
        received_signals.append(signum)
        stop.set()

    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        for number in previous:
            signal.signal(number, interrupted)
        report = run(args, stopped=stop)
    except (Exception, KeyboardInterrupt) as exc:
        report = dict(schema=1, mode='hdmi_live_camera', status='failed',
                      error=f'{type(exc).__name__}: {exc}')
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    if received_signals:
        report['termination_signal'] = received_signals[0]
        if report['status'] == 'stopped':
            report['status'] = 'interrupted'
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding='utf-8')
    print(encoded, file=sys.stderr, flush=True)
    if report['status'] == 'failed':
        return 1
    if report['status'] == 'interrupted':
        return 128 + (received_signals[0] if received_signals else signal.SIGINT)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
