#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build the already measured exact coordinate split; no HLS/new dependency."""
from pathlib import Path
import argparse,platform,subprocess
p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
if a.output.exists():raise FileExistsError(a.output)
flags=['-std=c11','-O3','-Wall','-Wextra','-Werror','-fPIC','-shared',
       '-fno-fast-math','-fno-math-errno','-ffp-contract=off']
if platform.machine()=='aarch64':flags+=['-mcpu=cortex-a53','-DPOSEPRIV_CROP_NEON','-DPOSEPRIV_CROP_COORD_PAIR','-DPOSEPRIV_CROP_FAST_NEIGHBORS']
subprocess.run(['/usr/bin/gcc',*flags,str(Path(__file__).parent/'crop_map_q5.c'),'-o',str(a.output),'-lm'],check=True)
print(a.output)
