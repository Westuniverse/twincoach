# SPDX-License-Identifier: GPL-2.0-only
"""Cold child-only selector over the matched public preview entry."""
from pathlib import Path
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'pair_worker'))


def run_hybrid_preview(*args):
    # Unlike the public feeder, this thin main has no package-path side effects
    # when imported by spawn. Restore the new package's own source closure here.
    cfg=json.loads(Path(args[0]).read_text());package=Path(cfg['hybrid_package_root']);candidate=package/'candidate'
    public_entry=cfg['hybrid_public_entry']
    sys.path[:0]=[str(package/'src'),public_entry]+[str(candidate/n)for n in (
        'tail4_native_gl_submit_20261031','tail4_world_math_20261031','tail4_stream_preview_20261030',
        'tail4_stream_process_20261030','tail4_geometry_process_20261030')]
    import posepriv_coach
    posepriv_coach.__path__.insert(0,public_entry)
    import posepriv_coach.field_preview_full33_split as entry
    import stream_runtime
    import leased_stream_process
    from hybrid_runtime import pose_backend,HybridPreviewModel,BACKEND,PROFILE
    from hybrid_process import HybridInferenceProcess
    original=(stream_runtime.pose_backend,stream_runtime.make_stronger_model,
              leased_stream_process.InferenceProcess,entry.PreviewState)
    class HybridPreviewState(entry.PreviewState):
        def publish(self,data,jpeg=None):
            data=dict(data,pose_backend=BACKEND,scoring_profile=PROFILE,scoring_qualified=False,
                model_by_physical_view=dict(side='Full33 first60+Tail35',front='Lightning B3136'),
                display_confidence_semantics='side original Full33 presence/visibility; front original Lightning keypoint score; not calibrated probabilities',
                scope='KV260 ARM plus same-PID Full33/Lightning DPU and side Tail35 PL plus unchanged PPS4/Mali; hybrid raw17 score candidate, not old Full33 equality or physical scanout')
            return super().publish(data,jpeg)
    stream_runtime.pose_backend=pose_backend;stream_runtime.make_stronger_model=HybridPreviewModel
    leased_stream_process.InferenceProcess=HybridInferenceProcess;entry.PreviewState=HybridPreviewState
    try:return entry._run(*args)
    finally:
        stream_runtime.pose_backend,stream_runtime.make_stronger_model,leased_stream_process.InferenceProcess,entry.PreviewState=original
