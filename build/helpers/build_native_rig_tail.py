#!/usr/bin/env python3
"""Build optional GPL-2.0-only13-bone CPU frame tail using existing GCC/CPythonAPI."""
import argparse
from pathlib import Path
import subprocess,sysconfig
ROOT=Path(__file__).resolve().parents[1]

def build(output,*,compiler,axis_batch=False):
    output=Path(output)
    if output.exists():raise FileExistsError(output)
    include=Path(sysconfig.get_path('include'))
    if not(include/'Python.h').is_file():raise FileNotFoundError('matching CPython development headers required: '+str(include))
    output.parent.mkdir(parents=True,exist_ok=True)
    command=[str(compiler),'-std=c11','-O3','-Wall','-Wextra','-Werror',
        '-Wno-misleading-indentation','-shared','-fPIC','-fno-fast-math','-ffp-contract=off',
        '-fno-builtin-pow','-I'+str(include)]
    if axis_batch:command.append('-DPOSEPRIV_RIG_AXIS_BATCH=1')
    command += [str(ROOT/'board_tests/rig_tail.c'),'-lm','-o',str(output)]
    subprocess.run(command,check=True)

def main():
    p=argparse.ArgumentParser();p.add_argument('--compiler',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--axis-batch',action='store_true');a=p.parse_args();build(a.output,compiler=a.compiler,axis_batch=a.axis_batch);print(a.output)
if __name__=='__main__':main()
