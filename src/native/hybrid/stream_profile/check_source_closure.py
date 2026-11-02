#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Cold import check for the independent package; no model/device/library load."""
import argparse
import importlib
import importlib.util
import json
from pathlib import Path
import sys


def main():
    p=argparse.ArgumentParser();p.add_argument('--package',type=Path,required=True);a=p.parse_args()
    package=a.package.resolve();candidate=package/'candidate';entry=candidate/'tail4_public_profile_20261031/entry'
    profile=candidate/'hybrid_stream_20261031'
    for name in ('src','scripts','board_tests','candidate'):
        if(package/name).is_symlink():raise ValueError('source directory must be a real copy: '+name)
    sys.path[:0]=[str(package/'src'),str(profile/'stream_profile'),str(profile/'pair_worker'),str(entry)]+[
        str(candidate/n)for n in ('tail4_native_gl_submit_20261031','tail4_world_math_20261031',
            'tail4_stream_preview_20261030','tail4_stream_process_20261030','tail4_geometry_process_20261030')]
    import posepriv_coach
    posepriv_coach.__path__.insert(0,str(entry))
    modules={}
    for name in ('posepriv_coach.field_preview_full33_split','leased_stream_process',
                 'hybrid_entry','hybrid_process','hybrid_runtime','hybrid_transport',
                 'posepriv_coach.native_world_math','posepriv_coach._decoded_lease_process'):
        module=importlib.import_module(name);path=Path(module.__file__).resolve()
        if not path.is_relative_to(package):raise ValueError('source escaped independent package: '+name+' '+str(path))
        modules[name]=str(path.relative_to(package))
    # The production package uses this feeder, not the repository's development
    # benchmark script. Its HERE drives candidate imports in both spawn owners.
    script=entry/'run_replay.py'
    if not script.resolve().is_relative_to(package):raise ValueError('replay feeder source escaped package')
    spec=importlib.util.spec_from_file_location('hybrid_closure_feeder',script)
    feeder=importlib.util.module_from_spec(spec);spec.loader.exec_module(feeder)
    if feeder.HERE.resolve()!=entry:raise ValueError('replay feeder source root escaped entry')
    launcher=importlib.import_module('run_hybrid_replay')
    launcher_path=Path(launcher.__file__).resolve()
    if launcher_path.parent!=profile/'stream_profile':raise ValueError('hybrid launcher escaped independent source')
    indexed=importlib.import_module('run_indexed_replay')
    from types import SimpleNamespace
    indexed_preview=indexed.load_preview(SimpleNamespace(package=package,public_entry=entry,hybrid_profile_dir=profile/'stream_profile'))
    if indexed_preview is not importlib.import_module('posepriv_coach.field_preview_full33_split').LivePreview:
        raise ValueError('indexed feeder does not reuse the matched preview class')
    spawn_entry=importlib.import_module('hybrid_entry')
    if spawn_entry.run_hybrid_preview.__module__!='hybrid_entry':raise ValueError('spawn target is not the source-owned hybrid entry')
    print(json.dumps(dict(status='PASS',package=str(package),modules=modules,
        replay_feeder=str(script.relative_to(package)),replay_source_root=str(feeder.HERE),
        hybrid_launcher=str(launcher_path.relative_to(package)),
        spawn_target='hybrid_entry.run_hybrid_preview',indexed_feeder=str(Path(indexed.__file__).resolve().relative_to(package)),device_calls=0,NN_calls=0)))


if __name__=='__main__':main()
