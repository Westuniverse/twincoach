#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Package-local camera shim; laptop controls only, board owns all computation."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3]


def main():
    parser=argparse.ArgumentParser(add_help=False);parser.add_argument('--config',type=Path,required=True)
    args,_=parser.parse_known_args();cfg=json.loads(args.config.read_text())
    from run_hybrid_preview import main as run
    sys.argv=[sys.argv[0],'--package',str(ROOT),'--public-entry',cfg['hybrid_public_entry'],*sys.argv[1:]]
    return run()


if __name__=='__main__':main()
