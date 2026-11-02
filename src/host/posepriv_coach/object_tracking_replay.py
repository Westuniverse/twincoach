# SPDX-License-Identifier: MIT
"""Bounded owned-RAM replay; CPU diagnostics, never capture or calibrated truth.

Inputs must be private, stable snapshots for the entire synchronous call. The
returned canvas is temporary viewing data; only the separate scalar record is
appropriate for evidence storage. No camera, GUI, file or global thread setting
is opened/changed here, and loss never causes automatic reseeding.
"""
from __future__ import annotations

from collections import Counter
import math
from pathlib import Path
import sys
import time

from .capture import CapturedFrame
from .relative_object import (
    AssumedObjectCamera, ColoredMarkerFrame, RectangleObjectPrior,
    render_relative_object,
)
from .relative_object_tracking import RelativeRectangleTracker, _uint64


def _inputs(frames, observation, camera, prior, native_library):
    import numpy as np
    if type(frames) not in (list, tuple) or not 2 <= len(frames) <= 32:
        raise ValueError("replay requires a finite list/tuple of 2..32 owned frames")
    if (not isinstance(camera, AssumedObjectCamera)
            or not isinstance(prior, RectangleObjectPrior)
            or not isinstance(observation, ColoredMarkerFrame)):
        raise ValueError("explicit camera/prior and exact first-frame observation required")
    if (not isinstance(native_library, (str, Path))
            or isinstance(native_library, str) and not native_library.strip()):
        raise ValueError("replay requires an explicit nonempty native library path")
    sequence = tuple(frames)  # References only: never copy/read all BGR pixels.
    first = sequence[0]
    if type(first) is not CapturedFrame:
        raise ValueError("replay requires exact CapturedFrame snapshots")
    previous = None
    for frame in sequence:
        if type(frame) is not CapturedFrame:
            raise ValueError("replay requires exact CapturedFrame snapshots")
        for name in ("frame_id", "timestamp_ns", "sync_timestamp_ns"):
            _uint64(getattr(frame, name), name)
        # This new entry deliberately accepts only proven owned snapshots, not
        # arbitrary legacy BGR views. Check ownership before any C conversion.
        image = frame.payload
        if (frame.pixel_format != "bgr8" or type(image) is not np.ndarray
                or image.dtype != np.dtype(np.uint8) or not image.dtype.isnative
                or not image.flags.c_contiguous or not image.flags.owndata
                or image.base is not None or not image.flags.aligned
                or image.shape != (camera.height, camera.width, 3)):
            raise ValueError("replay requires exact owned native uint8 C-contiguous BGR snapshots")
        if (type(frame.width) is not int or type(frame.height) is not int
                or (frame.width, frame.height) != (camera.width, camera.height)
                or frame.view != first.view):
            raise ValueError("replay source view and dimensions must remain fixed")
        # Reuse the correspondence contract for view and dimension validation.
        ColoredMarkerFrame(frame.view, frame.frame_id, frame.timestamp_ns,
                           frame.width, frame.height, (None,) * 4)
        if previous is not None and (frame.frame_id <= previous.frame_id
                                     or frame.timestamp_ns <= previous.timestamp_ns):
            raise ValueError("replay frame ID and raw timestamp must strictly increase")
        previous = frame
    if (first.view, first.frame_id, first.timestamp_ns, first.width, first.height) != (
            observation.view, observation.frame_id, observation.timestamp_ns,
            observation.width, observation.height):
        raise ValueError("replay seed must bind the exact first snapshot identity")
    return sequence


def _conditional_fit(step):
    error = step.result.reprojection_max_px
    return (None if not step.result.accepted or error is None
            else 100 * max(0.0, 1.0 - error / 2.0))


def _scalars(step):
    return {**step.summary(),
            "conditional_reprojection_fit_score": _conditional_fit(step)}


def _describe(values):
    if not values:
        return {"count": 0, "mean_ms": None, "p95_ms": None, "maximum_ms": None}
    ordered = sorted(values)
    return {"count": len(values), "mean_ms": sum(values) / len(values) / 1e6,
            "p95_ms": ordered[math.ceil(.95 * len(values)) - 1] / 1e6,
            "maximum_ms": ordered[-1] / 1e6}


def _population(samples):
    return {
        "sample_count": len(samples),
        "status_counts": dict(sorted(Counter(s["status"] for s in samples).items())),
        "accepted_count": sum(s["accepted"] for s in samples),
        "visual_valid_count": sum(s["visual_valid"] for s in samples),
        "wall": _describe([s["wall_ns"] for s in samples]),
        "owner_thread_cpu": _describe([s["owner_thread_cpu_ns"] for s in samples]),
    }


def _signature(samples):
    return [(s["frame_id"], s["timestamp_ns"], s["sync_timestamp_ns"],
             s["status"], s["accepted"], s["visual_valid"]) for s in samples]


def _change(old, new, comparable):
    if not comparable or old is None or new is None or old <= 0:
        return None
    return 100.0 * (new - old) / old


def replay_object_sequence(frames, observation, camera, prior, *, native_library):
    """Return (scalar report, candidate last canvas) for the same 2..32 frames.

    Complete TrackingStep and full 800x600 canvas goldens precede timing. Every
    normal rejected/lost/needs-seed row stays in all six phases; only an explicit
    first-frame seed is performed. No coordinates or model geometry enter the
    report. Caller must not mutate snapshots concurrently with this operation.
    """
    import cv2
    import numpy as np
    sequence = _inputs(frames, observation, camera, prior, native_library)
    cases = []
    last_canvas = None
    with RelativeRectangleTracker(camera, prior) as baseline, RelativeRectangleTracker(
            camera, prior, native_library=native_library) as candidate:
        for index, frame in enumerate(sequence):
            if index == 0:
                old = baseline.seed(frame, observation)
                new = candidate.seed(frame, observation)
            else:
                old = baseline.update(frame)
                new = candidate.update(frame)
            if old != new:
                raise AssertionError(f"complete replay TrackingStep differs at index {index}")
            old_canvas = render_relative_object(frame, old.result)
            new_canvas = render_relative_object(frame, new.result)
            if not np.array_equal(old_canvas, new_canvas):
                raise AssertionError(f"complete replay canvas differs at index {index}")
            cases.append({"index": index, **_scalars(new),
                          "complete_step_equal": True, "full_canvas_equal": True})
            last_canvas = new_canvas
            del old, new, old_canvas, new_canvas

    phases = []
    for label, native in (("A1", False), ("B1", True), ("B2", True),
                          ("A2", False), ("C1", False), ("C2", False)):
        context_start = time.perf_counter_ns()
        tracker = RelativeRectangleTracker(camera, prior,
            **({"native_library": native_library} if native else {}))
        context_ns = time.perf_counter_ns() - context_start
        try:
            with tracker:
                start = time.perf_counter_ns()
                seeded = tracker.seed(sequence[0], observation)
                seed_ns = time.perf_counter_ns() - start
                seed_summary = _scalars(seeded)
                summaries_match_golden = seed_summary == {
                    key: cases[0][key] for key in seed_summary}
                del seeded
                samples = []
                for index, frame in enumerate(sequence[1:]):
                    start = time.perf_counter_ns()
                    cpu_start = time.thread_time_ns()
                    step = tracker.update(frame)
                    cpu_ns = time.thread_time_ns() - cpu_start
                    wall_ns = time.perf_counter_ns() - start
                    summary = _scalars(step)
                    row_matches_golden = summary == {
                        key: cases[index + 1][key] for key in summary}
                    summaries_match_golden = summaries_match_golden and row_matches_golden
                    samples.append({"index": index, **summary,
                                    "wall_ns": wall_ns, "owner_thread_cpu_ns": cpu_ns})
                    del step  # Outside timing, before the next timer starts.
                retained = tracker.retained_bytes
                phases.append({"label": label,
                    "backend": "private_pyramid" if native else "python_gray",
                    "context_create_ns": context_ns,
                    "seed_ns": seed_ns, "seed_status": seed_summary["status"],
                    "seed_summary": seed_summary, "samples": samples,
                    "timed_summaries_match_golden": summaries_match_golden,
                    "retained_bytes": retained,
                    "all_updates": _population(samples),
                    "tracked_updates": _population([s for s in samples if s["status"] == "tracked"])})
        finally:
            # __enter__ can fail before __exit__; preserve an existing exception.
            if sys.exc_info()[0] is None:
                tracker.close()
            else:
                try:
                    tracker.close()
                except BaseException:
                    pass

    merged = {}
    for name, labels in (("baseline", ("A1", "A2")), ("candidate", ("B1", "B2")),
                         ("control_first", ("C1",)), ("control_second", ("C2",))):
        samples = [sample for phase in phases if phase["label"] in labels
                   for sample in phase["samples"]]
        merged[name] = {"all_updates": _population(samples),
                       "tracked_updates": _population([s for s in samples if s["status"] == "tracked"])}
    signatures_equal = all(_signature(p["samples"]) == _signature(phases[0]["samples"])
                           for p in phases)
    seeds_equal = all(p["seed_summary"] == phases[0]["seed_summary"] for p in phases)
    golden_status_equal = (_signature(cases[1:]) == _signature(phases[0]["samples"])
                           and cases[0]["status"] == phases[0]["seed_status"])
    a, b = merged["baseline"], merged["candidate"]
    counts_equal = (a["all_updates"]["status_counts"] == b["all_updates"]["status_counts"]
                    and a["all_updates"]["accepted_count"] == b["all_updates"]["accepted_count"])
    summaries_equal = all(p["timed_summaries_match_golden"] for p in phases)
    all_comparable = (signatures_equal and seeds_equal and golden_status_equal
                      and counts_equal and summaries_equal)
    tracked_comparable = all_comparable and a["tracked_updates"]["sample_count"] > 0
    comparison = {"status_sequence_equal": signatures_equal,
        "seed_summaries_equal": seeds_equal, "timed_status_matches_golden": golden_status_equal,
        "timed_summaries_match_golden": summaries_equal,
        "status_and_acceptance_counts_equal": counts_equal,
        "all_updates_comparable": all_comparable,
        "tracked_subset_comparable": tracked_comparable,
        "all_update_p95_change_percent": _change(a["all_updates"]["wall"]["p95_ms"],
            b["all_updates"]["wall"]["p95_ms"], all_comparable),
        "tracked_update_p95_change_percent": _change(a["tracked_updates"]["wall"]["p95_ms"],
            b["tracked_updates"]["wall"]["p95_ms"], tracked_comparable)}
    record = {
        "schema": "posepriv.object_tracking_replay.v1",
        "environment": {"opencv": cv2.__version__, "numpy": np.__version__,
                        "opencv_threads": cv2.getNumThreads()},
        "input": {**camera.assumptions(), "frame_count": len(sequence),
                  "marker_height_ratio": prior.height_ratio,
                  "model_depth_ratio": prior.model_depth_ratio,
                  "model_depth_is_prior": True,
                  "storage": "caller_private_owned_stable_RAM_snapshots",
                  "new_first_frame_manual_seed": True},
        "golden": {"case_count": len(cases), "cases": cases,
                   "complete_step_equal": True, "full_canvas_equal": True},
        "performance": {"phases": phases, "merged": merged, "comparison": comparison,
            "p95_method": "nearest_rank_on_merged_raw_samples",
            "tracked_subset": "only status=tracked; no needs_seed/seed/lost/model_rejected",
            "included": "update:validate+BGR_to_gray+FFI_private_copy+LK+qualify+PnP+retain",
            "excluded": "capture/input_preparation,context/load/create,seed,summary/fit/JSON,render/golden,result_release,GUI/HDMI,DPU/PL",
            "seed_is_segment_initialization_after_golden_not_process_cold": True,
            "retained_is_backing_plus_private_points_not_peak_or_RSS": True},
        "evidence_boundary": {"CPU_only": True, "DPU_executed": False,
            "PL_executed": False, "capture_executed": False, "window_or_hdmi": False,
            "memory_canvas_not_display_presentation": True,
            "conditional_fit_not_three_dimensional_accuracy": True,
            "three_dimensional_accuracy": None, "human_accuracy": None,
            "automatic_object_detection": False, "automatic_reseed": False,
            "default_changed": False},
    }
    return record, last_canvas
