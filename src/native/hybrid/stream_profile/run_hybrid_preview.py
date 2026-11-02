#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Use the original live-camera owner with the explicit single-PID hybrid NN."""
import argparse
import importlib.util
from pathlib import Path
import sys

def main():
    p=argparse.ArgumentParser(add_help=False)
    p.add_argument('--public-entry',type=Path,required=True)
    p.add_argument('--package',type=Path,required=True)
    args,remaining=p.parse_known_args()
    sys.path[:0]=[str(args.package),str(args.package/'src')]
    script=args.public_entry/'run_preview.py'
    spec=importlib.util.spec_from_file_location('hybrid_live_camera_entry',script)
    entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)
    import posepriv_coach.field_preview_full33_split as runtime
    from hybrid_entry import run_hybrid_preview
    runtime._run=run_hybrid_preview
    sys.argv=[str(script),*remaining]
    entry.main()

if __name__=='__main__':main()
