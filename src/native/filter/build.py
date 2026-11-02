# SPDX-License-Identifier: GPL-2.0-only
"""Build with the target interpreter's existing CPython headers and compiler."""
from pathlib import Path
import os
import shlex
import platform
import subprocess
import sysconfig

here=Path(__file__).resolve().parent
compiler=shlex.split(os.environ.get('CC','gcc'))
command=compiler+['-std=c11','-O3','-Wall','-Wextra','-Werror',
         '-fno-fast-math','-ffp-contract=off','-shared','-fPIC',
         '-I'+sysconfig.get_paths()['include'],str(here/'world_filter.c'),
         '-o',str(here/'libposepriv_world_filter.so'),'-lm']
architecture=os.environ.get('ARCH_FLAGS','-mcpu=cortex-a53' if platform.machine()=='aarch64' else '')
command[len(compiler):len(compiler)]=shlex.split(architecture)
result=subprocess.run(command,capture_output=True,text=True)
(here/'build.log').write_text(' '.join(command)+'\n'+result.stdout+result.stderr)
result.check_returncode()
print(here/'libposepriv_world_filter.so')
