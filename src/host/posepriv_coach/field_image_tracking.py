"""Explicit experimental current-image display adapter; CNN scores stay separate."""
from pathlib import Path
from time import monotonic_ns
import os

from .contracts import CameraView, Keypoint, PoseFrame


def image_tracking_enabled(cfg):
    enabled = cfg.get('preview_image_tracking', False)
    if type(enabled) is not bool:
        raise ValueError('preview_image_tracking must be boolean')
    if not enabled:
        return False
    required = ('preview_inference_process', 'preview_geometry_process', 'hdmi_display',
                'preview_paired_requests', 'preview_decode_prefetch', 'preview_demand_pipeline')
    if any(cfg.get(name) is not True for name in required):
        raise ValueError('image tracking requires owned paired inference/geometry, HDMI and demand prefetch')
    if cfg.get('preview_pose_backend', 'movenet') != 'movenet':
        raise ValueError('image tracking requires the original MoveNet backend')
    if cfg.get('preview_model_ready_overlap', False) is not False or cfg.get('preview_geometry_pipeline', False) is not False:
        raise ValueError('image tracking cannot mix model READY overlap or geometry thread')
    library = cfg.get('preview_image_tracking_library')
    if type(library) is not str or not library:
        raise ValueError('image tracking requires an explicit BODY17 CPU library')
    mapping = cfg.get('preview_image_tracking_physical_mapping')
    if (type(mapping) is not dict or set(mapping) != {'front', 'side'} or
            any(type(v) is not str for v in mapping.values()) or set(mapping.values()) != {'front', 'side'}):
        raise ValueError('image tracking requires an explicit physical source permutation')
    return True


class PreviewImageTrackingModel:
    """Own the original CNN and independent image contexts; expose true CNN stats."""
    def __init__(self, base, cfg):
        if not image_tracking_enabled(cfg):
            raise ValueError('image tracking must be explicitly enabled')
        from .current_image_tracking import CurrentImageTrackingCore
        self.epoch = 'preview-owner-' + str(os.getpid())
        try:
            self.core = CurrentImageTrackingCore(base,
                native_library=Path(cfg['preview_image_tracking_library']),
                physical_by_view=cfg['preview_image_tracking_physical_mapping'])
        except BaseException:
            base.close()
            raise

    def start(self):
        return self.core.base.start()

    def stats(self):
        stats = self.core.stats()
        base = stats.pop('base')
        return dict(base, image_tracking=stats)

    def close(self):
        return self.core.close()


def display_pose_mask(pose):
    """Only a rig input mask, never the raw neural observation or confidence."""
    source = pose.source
    points = {}
    for name, point in pose.points.items():
        weight = point.display_weight
        if type(weight) is not int or weight not in (0, 1) or point.inferred is not True:
            raise ValueError('display point requires an explicit inferred mask')
        if weight:
            if point.x is None or point.y is None:
                raise ValueError('valid image-derived point has no current coordinates')
            points[name] = Keypoint(point.x, point.y, 1.)
        else:
            if point.x is not None or point.y is not None:
                raise ValueError('invalid image-derived point must not carry held coordinates')
            points[name] = Keypoint(0., 0., 0.)
    return PoseFrame(CameraView(source.model_view), source.frame_id, source.sync_timestamp_ns,
                     points, source.width, source.height)


def estimate_image_tracked_pair(decoded, model, executor):
    """Publish one current source pair and explicit optional original CNN score pair."""
    from .current_image_tracking import RuntimeFrame
    captured = decoded['captured']
    if not isinstance(captured, dict) or set(captured) != {'front', 'side'}:
        raise ValueError('image tracking requires a complete already-decoded source pair')
    started = monotonic_ns()
    result = model.core.process_pair(
        tuple(RuntimeFrame(model.epoch, row[0]) for row in captured.values()), executor)
    elapsed = monotonic_ns() - started
    poses = {p.source.model_view: display_pose_mask(p) for p in result.display_poses}
    raw = None if result.raw_cnn_poses is None else {p.view.value: p for p in result.raw_cnn_poses}
    if result.cnn_performed != (raw is not None):
        raise RuntimeError('CNN score observation availability differs from actual execution')
    selected = decoded['selected']
    for view, pose in poses.items():
        if (pose.frame_id, pose.timestamp_ns) != selected[view][:2]:
            raise RuntimeError('current tracked display source differs from its current image')
    stages = {view: dict(decode_rotate=row[1] / 1e6, current_image_pair_wall=elapsed / 1e6,
                        **{name:value/1e6 for name,value in timing.items()})
              for (view,row),timing in zip(captured.items(),result.role_timing_ns,strict=True)}
    measured = {p.source.model_view: bool(result.cnn_performed or any(
        point.display_weight and point.provenance == 'current_image_LK_display'
        for point in p.points.values())) for p in result.display_poses}
    return dict(dpu_frames=model.stats()['frames'], selected=selected, poses=poses,
        neural_score_poses=raw, display_pose_semantics='current_image_inferred_mask_v1',
        images={view: row[0].payload for view, row in captured.items()}, stages=stages,
        begin=decoded['begin'], inferred=monotonic_ns(), fetch_started=decoded['fetch_started'],
        decode_ready_ns=max(row[2] for row in captured.values()), model_begin_ns=started,
        current_image_tracking=dict(cnn_performed=result.cnn_performed, cnn_reason=result.cnn_reason,
            cnn_pose_images=0 if raw is None else len(raw), all_display_slots_inferred=True,
            current_measurement_by_view=measured, fresh_pose_source_pair=all(measured.values()),
            physical_by_model_view={s.model_view: s.physical_view for s in result.sources}))
