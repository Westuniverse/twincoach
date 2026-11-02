# SPDX-License-Identifier: GPL-2.0-only
"""Build with the target interpreter's existing CPython headers and compiler."""
from pathlib import Path
import platform
import subprocess
import sysconfig

here=Path(__file__).resolve().parent
command=['/usr/bin/gcc','-std=c11','-O3','-Wall','-Wextra','-Werror',
         '-fno-fast-math','-ffp-contract=off','-shared','-fPIC',
         '-I'+sysconfig.get_paths()['include'],str(here/'world_filter.c'),
         '-o',str(here/'libposepriv_world_filter.so'),'-lm']
if platform.machine()=='aarch64':command.insert(2,'-mcpu=cortex-a53')
result=subprocess.run(command,capture_output=True,text=True)
(here/'build.log').write_text(' '.join(command)+'\n'+result.stdout+result.stderr)
result.check_returncode()
print(here/'libposepriv_world_filter.so')
