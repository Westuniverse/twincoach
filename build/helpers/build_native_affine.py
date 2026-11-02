#!/usr/bin/env python3
"""Build the optional CPU affine extension for the Python running this script."""
import argparse
from pathlib import Path
import subprocess
import sysconfig

ROOT=Path(__file__).resolve().parents[1]


def build(output, *, compiler):
    output=Path(output)
    if output.exists():raise FileExistsError(output)
    include=Path(sysconfig.get_path('include'))
    if not (include/'Python.h').is_file():
        raise FileNotFoundError('matching CPython development headers required: '+str(include))
    output.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run([str(compiler),'-std=c11','-O3','-Wall','-Wextra','-Werror','-shared','-fPIC',
        '-I'+str(include),str(ROOT/'board_tests/affine_compose.c'),'-o',str(output)],check=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    build(args.output,compiler=args.compiler)
    print(args.output)


if __name__=='__main__':main()
