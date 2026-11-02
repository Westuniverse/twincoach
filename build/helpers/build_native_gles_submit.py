#!/usr/bin/env python3
"""Build optional GPL-2.0-only GLES submit using existing GCC/libdl/GLES ABI."""
import argparse
from pathlib import Path
import platform
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def build(output,*,compiler):
    output=Path(output)
    if output.exists():raise FileExistsError(output)
    output.parent.mkdir(parents=True,exist_ok=True)
    architecture=['-mcpu=cortex-a53']if platform.machine()=='aarch64'else[]
    subprocess.run([str(compiler),'-std=c11','-O3','-Wall','-Wextra','-Werror','-shared','-fPIC',
        *architecture,str(ROOT/'board_tests/gles_submit.c'),'-o',str(output),'-ldl'],check=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();build(args.output,compiler=args.compiler);print(args.output)


if __name__=='__main__':main()
