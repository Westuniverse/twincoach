"""One parent dispatcher owns a complete PPS4 frame; GLES stays on its thread."""
from time import monotonic_ns

from .avatar_skinning import PackedSkinningOutputBatch


def skinning_pipeline_enabled(cfg):
    enabled = cfg.get('preview_skinning_pipeline', False)
    if type(enabled) is not bool:
        raise ValueError('preview_skinning_pipeline must be boolean')
    if enabled and (cfg.get('hdmi_display') is not True or
                    cfg.get('preview_inference_process') is not True or
                    cfg.get('preview_geometry_process') is not True or
                    cfg.get('preview_geometry_pipeline', False) is not False):
        raise ValueError('preview_skinning_pipeline requires HDMI and the existing geometry process')
    return enabled


def execute_prepared_skinning(prepared, backend, stopped):
    """Called only by the existing single READY producer, never by its child.

    Program and exchange remain one owner operation. The kernel's copied bytes
    belong to this item, so the next frame can run after take without borrowing
    the renderer's current output. Stop is checked before, not faked during I/O.
    """
    if prepared is None or stopped.is_set():
        return None
    started = monotonic_ns()
    if started - min(r[1] for r in prepared['work']['selected'].values()) > 500_000_000:
        return None
    plan = prepared['plan']
    outputs = plan.execute(backend, program_matrices=backend.program_matrices)
    finished = monotonic_ns()
    if not isinstance(outputs, PackedSkinningOutputBatch) or type(outputs.padded_data) is not bytes:
        raise TypeError('dispatcher skinning requires an owned immutable packed output')
    return dict(prepared, skinning_outputs=outputs, skin_started_ns=started, skinned_ns=finished)
