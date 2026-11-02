# Hybrid inference

The hybrid backend assigns Full33 inference to the physical side view and
MoveNet Lightning to the physical front view. A single native process owns both
DPU runners, while Tail35 PL work overlaps the next front-view inference.

The backend name is `hybrid_full33_side_lightning_front`. The primary avatar
view is `side`, and scoring uses `hybrid_side_full33_front_lightning_raw17_v1`.
Front-view results contain COCO17 keypoints and no world33 output; side-view
results provide Full33 keypoints and world coordinates.

## Scheduling and ownership

Use a source capacity of four: each pair owns two source tickets, and the next
pair requires both tickets before submission. The two-live-pair bound, image
leases and cooperative cancellation keep source ownership explicit.

Unknown side ROI uses kind3, which performs front-view Lightning inference and
reports zero Full33/Tail executions. Every accepted model result comes from an
actual inference request; submitted inputs and completed neural pairs are
separate counters.

## Build

First assemble the runtime layout with `build/stage.py` and provide the assets
listed in `build/assets.json`. The native worker shares the Full33 runtime and
headers, including the model-specific `tail_program_header` asset.

Within the assembled runtime, build on the matching ARM/VART environment:

```sh
sh native/hybrid/pair_worker/compile_native.sh
```

The resulting executable is supplied as `runtime/hybrid_pair_worker`. Weights,
xmodel, TFLite models, postprocessing mappings and generated model headers are
external assets. Project glue uses GPL-2.0-only; upstream model and runtime
components retain their own licenses.

## Configuration

Use the staged hybrid configuration template and replace `PACKAGE_ROOT` with
the actual board directory. Keep model, mapping and postprocessing contract
files consistent. The hybrid entry is optional; the application defaults to
dual Full33.

Indexed replay uses persistent JPEG readers and explicit source identities.
Repeated input sequences carry a separate cycle identifier. Replay duration,
source submission rate and completed pose-pair rate describe different parts
of the pipeline.
