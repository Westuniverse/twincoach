"""Explicit stronger-model preview adapter; raw/action data retain physical roles."""
from dataclasses import replace
from time import monotonic_ns

from .contracts import CameraView

BACKENDS = ("movenet", "rtmpose_m_cpu", "rtmpose_m_remote")


def pose_backend(cfg):
    value = cfg.get("preview_pose_backend", "movenet")
    if type(value) is not str or value not in BACKENDS:
        raise ValueError("unsupported preview_pose_backend")
    return value


def make_stronger_model(cfg):
    backend = pose_backend(cfg)
    if backend == "rtmpose_m_cpu":
        from .stronger_pose_runtime import StrongerPoseRuntime
        return StrongerPoseRuntime(cfg["stronger_model_bundle"],
                                   num_threads=cfg.get("stronger_model_threads", 2),
                                   pair_parallel=cfg.get("stronger_model_pair_parallel", False))
    if backend == "rtmpose_m_remote":
        from .stronger_pose_remote import RemoteStrongerPoseClient
        return RemoteStrongerPoseClient(host="127.0.0.1", port=cfg["stronger_model_port"],
            timeout=cfg.get("stronger_model_timeout", 10.),
            decode_scale=cfg.get("preview_decode_scale", 1))
    raise ValueError("stronger model factory requires an explicit stronger backend")


def estimate_stronger_pair(decoded, model, rotations, backend):
    """Infer on physical frames, before optional render-only role mapping."""
    if backend not in BACKENDS[1:]:
        raise ValueError("stronger inference requires an explicit backend")
    captured = dict(decoded["captured"])
    if set(captured) != {"front", "side"} or set(decoded["selected"]) != set(captured):
        raise ValueError("stronger preview requires both physical views")
    frames = [captured[v][0] for v in ("front", "side")]
    for view, frame in zip(("front", "side"), frames):
        source = decoded["selected"][view]
        if (any(type(n) is not int or n < 0 for n in source[:2])
                or type(frame.frame_id) is not int or type(frame.timestamp_ns) is not int
                or any(type(n) is not int for n in (frame.sync_timestamp_ns, frame.width, frame.height))
                or frame.pixel_format != 'bgr8'
                or frame.view is not CameraView(view)
                or (frame.frame_id, frame.timestamp_ns) != source[:2]
                or frame.sync_timestamp_ns != frame.timestamp_ns
                or frame.payload.shape != (frame.height, frame.width, 3)):
            raise ValueError("stronger captured frame differs from physical source")
    started = monotonic_ns()
    result = (model.infer_preview_pair(frames, decoded["selected"], rotations)
              if backend == "rtmpose_m_remote" else model.infer_pair(frames))
    finished = monotonic_ns()
    observations = result.observations
    poses = {v: result.display[CameraView(v)].pose for v in ("front", "side")}
    raw = {v: observations[CameraView(v)].raw.to_record() for v in poses}
    action = dict(result.action)
    for view, pose in poses.items():
        source = decoded["selected"][view]
        frame = captured[view][0]
        if (type(pose.frame_id) is not int or type(pose.timestamp_ns) is not int
                or any(type(n) is not int for n in (pose.source_width, pose.source_height))
                or pose.view is not CameraView(view)
                or (pose.frame_id, pose.timestamp_ns) != source[:2]
                or (pose.source_width, pose.source_height) != (frame.width, frame.height)):
            raise ValueError("stronger preview pose differs from physical source")
        if (raw[view]["view"] != view
                or any(type(raw[view][k]) is not int for k in ("frame_id", "timestamp_ns", "source_width", "source_height"))
                or (raw[view]["frame_id"], raw[view]["timestamp_ns"]) != source[:2]
                or (raw[view]["source_width"], raw[view]["source_height"]) != (frame.width, frame.height)):
            raise ValueError("stronger raw source mismatch")
    if any(type(action[key]) is not int or action[key] != decoded["selected"][v][column] for v in poses
           for key, column in ((v + "_frame_id", 0), (v + "_timestamp_ns", 1))):
        raise ValueError("stronger action/source mismatch")
    stats = model.stats()
    return {"selected": decoded["selected"], "poses": poses, "raw_poses": raw,
        "images": {v: frame[0].payload for v, frame in captured.items()},
        "stages": {v: {"decode_rotate": frame[1]/1e6,
                        "pose_action_pair_wall": (finished-started)/1e6}
                for v, frame in captured.items()},
        "begin": decoded["begin"], "inferred": finished, "fetch_started": decoded["fetch_started"],
        "dpu_frames": 0, "pose_frames": stats["frames"], "pose_backend": backend,
        "action_prediction": action, "stronger_model_stats": stats,
        "display_confidence_semantics": "binary_display_observation_weight_not_calibrated_probability",
        "scoring_qualified": False}


def map_display_work(work, cfg):
    """One local role alias for display; physical raw/action provenance is intact."""
    if pose_backend(cfg) == "movenet" or work is None:
        return work
    mapping = cfg.get("preview_anatomical_view_mapping", "device_names")
    if type(mapping) is not str or mapping not in ("device_names", "swapped"):
        raise ValueError("unsupported preview_anatomical_view_mapping")
    if "display_view_mapping" in work:
        raise ValueError("display work must not be mapped twice")
    result = dict(work)
    result["physical_source_ids"] = {v: (r[0], r[1]) for v, r in work["selected"].items()}
    result["display_view_mapping"] = mapping
    if mapping == "swapped":
        sources = {"front": "side", "side": "front"}
        result["selected"] = {role: work["selected"][physical] for role, physical in sources.items()}
        result["poses"] = {role: replace(work["poses"][physical], view=CameraView(role))
                           for role, physical in sources.items()}
        result["images"] = {role: work["images"][physical] for role, physical in sources.items()}
        result["stages"] = {role: dict(work["stages"][physical]) for role, physical in sources.items()}
    return result
