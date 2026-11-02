#!/usr/bin/env python3
"""Build an explicit scalar or AArch64 NEON raw-head post library; no models."""
import argparse
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def build(output, *, compiler, neon=False):
    if type(neon)is not bool:raise ValueError('neon must be boolean')
    output=Path(output)
    if output.exists():raise FileExistsError(output)
    flags=['-std=c11','-O3','-Wall','-Wextra','-Werror','-shared','-fPIC','-ffp-contract=off','-fno-fast-math']
    if neon:
        target=subprocess.check_output([str(compiler),'-dumpmachine'],text=True).strip()
        if not target.startswith(('aarch64-','arm64-')):raise ValueError('--neon requires an explicit AArch64 compiler target')
        flags.append('-march=armv8-a+simd')
    source='native_movenet_post_neon.c'if neon else'native_movenet_post.c'
    output.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run([str(compiler),*flags,str(ROOT/'board_tests'/source),'-lm','-o',str(output)],check=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--neon',action='store_true',help='Explicit AArch64-only implementation; never auto-selected')
    args=parser.parse_args();build(args.output,compiler=args.compiler,neon=args.neon)
    print(args.output)


if __name__=='__main__':main()
