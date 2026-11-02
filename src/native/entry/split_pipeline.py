# SPDX-License-Identifier: GPL-2.0-only
"""One bounded skinning owner between geometry RPC and main-thread GPU."""
from posepriv_coach.field_preview_pipeline import LatestInferenceWorker
from posepriv_coach.field_preview_skinning import execute_prepared_skinning


def make_skinning_worker(geometry_worker,backend,stopped,*,execute=execute_prepared_skinning):
    def next_skinning():
        prepared=geometry_worker.take()
        if prepared is None:return None
        return execute(prepared,backend,stopped)
    return LatestInferenceWorker(next_skinning,stopped,
        name='posepriv-skinning',block_when_ready=True)
