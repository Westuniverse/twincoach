# SPDX-License-Identifier: GPL-2.0-only
"""Explicit private owner; current stream/default process modules are untouched."""
import importlib.util
from pathlib import Path
import sys

from image_lease import LeasedInferenceMixin
from stream_process import reconstruct_world
from jpeg_owner import run_stream_owner

HERE=Path(__file__).resolve().parent
name='posepriv_coach._decoded_lease_process'
spec=importlib.util.spec_from_file_location(name,HERE/'_leased_process_clone.py')
base=importlib.util.module_from_spec(spec)
sys.modules[name]=base
spec.loader.exec_module(base)


class InferenceProcess(LeasedInferenceMixin,base.InferenceProcess):
    def __init__(self,cfg,inputs,rotations,stopped,*,_target=run_stream_owner,shared_dma_fd=None):
        cfg=dict(cfg)
        if cfg.get('preview_decoded_arena') is not True:
            raise ValueError('explicit image lease candidate requires decoded arena')
        command=cfg['tail4_worker_command']
        cfg['worker'],cfg['model']=command[:2]
        if shared_dma_fd is not None:
            from functools import partial
            from roi_shared_fd import run_with_shared_fd
            _target=partial(run_with_shared_fd,shared_dma_fd,_target)
        self._init_image_leases()
        super().__init__(cfg,inputs,rotations,stopped,_target=_target)

    def _reconstruct_leased_work(self,work):
        return reconstruct_world(work)

    def take(self,timeout=.05):
        return reconstruct_world(super().take(timeout))

    def stats(self):
        result=super().stats()
        result.update(model_owner='full33_stream_process_explicit_image_leases',
            decoded_arena_slots=len(self.output.states),
            decoded_active_image_leases=self.active_image_leases(),
            stream_capacity=self.cfg.get('tail4_stream_capacity',4))
        return result
