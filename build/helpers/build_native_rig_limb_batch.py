#!/usr/bin/env python3
"""Build opt-in GPL2 rig front-half with existing matching CPython/NumPy APIs."""
import argparse
from pathlib import Path
import platform
import subprocess
import sysconfig

ROOT=Path(__file__).resolve().parents[1]


def build(output,*,compiler):
    import numpy
    output=Path(output)
    if output.exists():raise FileExistsError(output)
    output.parent.mkdir(parents=True,exist_ok=True)
    architecture=['-mcpu=cortex-a53']if platform.machine()=='aarch64'else[]
    subprocess.run([str(compiler),'-std=c11','-O3','-Wall','-Wextra','-Werror','-shared','-fPIC',
        '-fno-fast-math','-ffp-contract=off',*architecture,'-I'+sysconfig.get_path('include'),
        '-I'+numpy.get_include(),str(ROOT/'board_tests/rig_limb_batch.c'),'-lm','-o',str(output)],check=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--compiler',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    build(args.output,compiler=args.compiler);print(args.output)


if __name__=='__main__':main()
