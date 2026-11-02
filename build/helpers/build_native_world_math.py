#!/usr/bin/env python3
"""Build optional GPL2 Full33 math with existing GCC/CPython API (PSF)/NumPy."""
import argparse
from pathlib import Path
import platform
import subprocess
import sysconfig

ROOT=Path(__file__).resolve().parents[1]


def build(output,*,compiler):
    output=Path(output)
    if output.exists():raise FileExistsError(output)
    include=Path(sysconfig.get_path('include'))
    if not(include/'Python.h').is_file():
        raise FileNotFoundError('matching CPython development headers required: '+str(include))
    output.parent.mkdir(parents=True,exist_ok=True)
    command=[str(compiler),'-std=c11','-O3','-Wall','-Wextra','-Werror','-shared','-fPIC',
        '-fno-fast-math','-ffp-contract=off','-I'+str(include),str(ROOT/'board_tests/world_math.c'),
        '-lm','-o',str(output)]
    if platform.machine()=='aarch64':command.insert(1,'-mcpu=cortex-a53')
    subprocess.run(command,check=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();build(args.output,compiler=args.compiler);print(args.output)


if __name__=='__main__':main()
