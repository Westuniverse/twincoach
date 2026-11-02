#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build the precise JPEG leaf against the selected target's existing libjpeg8."""
import argparse
from pathlib import Path
import platform
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler',type=Path,default=Path('/usr/bin/gcc'))
    parser.add_argument('--include',type=Path)
    parser.add_argument('--jpeg-library',type=Path)
    parser.add_argument('--neon-rotation',action='store_true',help='explicit exact AArch64 rotation candidate')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise FileExistsError(args.output)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    command=[str(args.compiler),'-std=c11','-O3','-Wall','-Wextra','-Werror','-fPIC','-shared',
        '-fno-fast-math','-ffp-contract=off']
    if platform.machine()=='aarch64':command+=['-mcpu=cortex-a53']
    if args.neon_rotation:command+=['-DPOSEPRIV_JPEG_NEON_ROTATE=1']
    if args.include:command+=['-I'+str(args.include.resolve())]
    command+=[str(ROOT/'board_tests/jpeg_decode.c'),'-o',str(args.output)]
    command+=[str(args.jpeg_library)]if args.jpeg_library else['-ljpeg']
    subprocess.run(command,check=True)
    print(args.output)

if __name__=='__main__':main()
