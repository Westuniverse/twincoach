#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Thin matched-public-entry launcher; feeder/render/leases/close stay original."""
import argparse
import importlib.util
from pathlib import Path
import sys


def main():
    p=argparse.ArgumentParser(add_help=False);p.add_argument('--public-entry',type=Path,required=True)
    a,remaining=p.parse_known_args()
    if '--package'not in remaining:raise ValueError('original replay --package required')
    package=Path(remaining[remaining.index('--package')+1]);sys.path.insert(0,str(package/'src'))
    script=a.public_entry/'run_replay.py'
    spec=importlib.util.spec_from_file_location('hybrid_matched_public_replay',script)
    replay=importlib.util.module_from_spec(spec);spec.loader.exec_module(replay)
    import posepriv_coach.field_preview_full33_split as entry
    from hybrid_entry import run_hybrid_preview
    entry._run=run_hybrid_preview
    sys.argv=[str(script),*remaining];replay.main()


if __name__=='__main__':main()
