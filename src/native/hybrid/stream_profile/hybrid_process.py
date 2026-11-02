# SPDX-License-Identifier: GPL-2.0-only
"""Reuse the original decoded arena/image-lease process and JPEG owner."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'pair_worker'))
import leased_stream_process
BaseProcess=leased_stream_process.InferenceProcess


def run_hybrid_owner(cfg,*args):
    sys.path.insert(0,cfg['tail4_memfd_module_dir'])
    import stream_runtime
    import shm_client
    from jpeg_owner import run_stream_owner
    from hybrid_runtime import HybridPreviewModel
    from hybrid_transport import HybridTransport
    original_model=stream_runtime.StreamingPreviewModel
    original_transport=shm_client.StreamCombinedWorker
    # Explicit only in this owned NN child. The original decode/arena/publish/
    # lease/stop loop runs unchanged; no second old DPU transport is constructed.
    stream_runtime.StreamingPreviewModel=HybridPreviewModel
    shm_client.StreamCombinedWorker=lambda command,**kw:HybridTransport(cfg,**kw)
    try:return run_stream_owner(cfg,*args)
    finally:
        stream_runtime.StreamingPreviewModel=original_model
        shm_client.StreamCombinedWorker=original_transport


class HybridInferenceProcess(BaseProcess):
    def __init__(self,cfg,*args,**kwargs):
        cfg=dict(cfg)
        command=list(cfg['tail4_worker_command']);command[0]=cfg['hybrid_pair_worker']
        cfg['tail4_worker_command']=command
        # Existing process-group cleanup first2-argv identity now matches this
        # hybrid worker plus the same Full33 prefix model. All leases are reused.
        super().__init__(cfg,*args,_target=run_hybrid_owner,**kwargs)

    def stats(self):
        value=super().stats()
        value.update(model_owner='hybrid_side_full33_front_lightning_explicit_image_leases',
            scoring_profile='hybrid_side_full33_front_lightning_raw17_v1',scoring_qualified=False)
        return value
