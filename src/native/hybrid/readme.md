# Explicit same-PID hybrid candidate

The default Full33 entry and controller stay unchanged. This candidate runs
physical **side: original Full33 first60 on the B3136 DPU plus Tail35 PL**, and
physical **front: existing Google MoveNet Lightning method1+CLE on the same DPU**.
One native process owns both Runners. Side Tail35 overlaps the next front CNN;
there is no second DPU process or copied pose output. PPS4/Mali and image leases
remain the existing owners.

The explicit backend is `hybrid_full33_side_lightning_front`, primary avatar
view is `side`, and the score profile is
`hybrid_side_full33_front_lightning_raw17_v1`. Front contributes its original
COCO17 keypoint scores and **empty world33**; side retains original Full33 raw17
and world33. No physical view is renamed. The raw17/scoring/geometry combination
is a new profile, not old Full33 equality or an accuracy qualification. In
particular a missing front world cannot supply the old other-view 3D arm donor.

Use source capacity4 for the explicit hybrid configuration: one pair owns two
source tickets and the next pair needs both front and side tickets before its
atomic RPC. The two-live-pair/image-lease bound and cooperative cancel/close stay
original. Unknown side ROI uses explicit kind3, which executes real front
Lightning only and reports zero Full33/Tail executions. Native startup still
creates both original Runners and opens the matching Tail runtime.

## Build and external assets

`hybrid_pair_worker.cpp` includes the public sibling
`../tail4_shm_worker_20261030/tail4_shm_worker.cpp`; compile script also uses that
common `tail4_runtime.c` and its headers. There is no duplicated worker/runtime
or model-derived `tail4_program.h` in this directory. After staging the matching
explicit model assets, build on ARM with:

```sh
sh candidate/hybrid_stream_20261031/pair_worker/compile_native.sh
```

The native build emits `pair_worker/hybrid_pair_worker`; its explicit extension artifact is staged at `runtime/hybrid_pair_worker`. The source-only package
can run host mock transport tests without model assets; actual native build
requires the external `tail_program_header` from the normal Full33 asset contract.

`scripts/pack_full33_runtime.py` now includes this source layout and the existing
native MoveNet post builder/C sources. Optional hybrid files are supplied through
`config/full33_runtime/hybrid_extension_artifacts.example.json`: copy its entries
into the explicit asset manifest, supply actual sources and original license/
provenance. They do not become requirements for the original default Full33.
Weights/xmodel/TFLite/mappings/post-contract NPZ/native ELF remain external; no
checkpoint, personal mesh, video, generated executable or model schedule belongs
in Git. Google model/post derivatives retain Apache-2.0; project glue/native post
retain GPL-2.0-only; AMD runtime/tools remain separately licensed. See sources.json
and the existing dependency registrations.

## Explicit launch

Create a separate Lightning path config from
`config/hybrid_lightning_config.example.json` in the staged package, replacing
`PACKAGE_ROOT` with its actual board root and preserving the exact existing
mapping/post/contract files. Generate a separate hybrid cap4 config:

```sh
python3 candidate/hybrid_stream_20261031/stream_profile/make_config.py \
  --baseline config/original-full33.json --p4-config config/hybrid-lightning.json \
  --worker PACKAGE_ROOT/runtime/hybrid_pair_worker \
  --package-root PACKAGE_ROOT --output config/explicit-hybrid.json
```

Use the existing shared-DMA supervisor and exclusive DPU owner, then invoke
`stream_profile/run_hybrid_preview.py --public-entry ... --package ...` for the
original live-camera launcher, or `run_hybrid_replay.py` for the original preloaded
replay. These are explicit alternate entry points; no controller default changes.
The camera dynamic-framerate wrapper remains the normal project's launch wrapper.

`stream_profile/run_indexed_replay.py` additionally accepts `--public-entry`,
`--hybrid-profile-dir` and the same package/config/input/client/port arguments.
It uses the same `LivePreview` and hybrid spawn target, with only index/offset/
length plus two persistent JPEG readers. Default is one entire original sequence;
original hot15..44s is unchanged. `--duration600` cyclically reuses that original
sequence, with original FID/ns and cycle/rebased targets separately logged. It is
not independent600s footage. `--presentation-capacity32768` only when explicit
copies the config and changes its collector capacity; otherwise no config changes.
Each accepted model completion still uses actual NN, not repeated cached points
or draw-count inflation. Original source submissions can be superseded by the
runtime and must not be confused with completed neural pairs.

`stream_profile/check_source_closure.py --package PACKAGE` checks cold imports
from a real source-only staged copy without models/device calls. Tests cover
original image ownership, next-pair credits, empty-side kind3, cooperative cancel,
raw model lengths/roles and complete footer. These are source/mock checks; board
quality, camera placement and end-to-end speed remain separate evidence.
