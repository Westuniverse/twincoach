#!/usr/bin/env python3
"""Build the optional world-plan boundary with matching CPython/NumPy headers."""
import argparse
from pathlib import Path
import platform
import subprocess
import sysconfig

ROOT = Path(__file__).resolve().parents[1]


def build(output, *, compiler):
    import numpy
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    architecture = ['-mcpu=cortex-a53'] if platform.machine() == 'aarch64' else []
    subprocess.run([
        str(compiler), '-std=c11', '-O3', '-Wall', '-Wextra', '-Werror',
        '-fno-fast-math', '-ffp-contract=off', '-shared', '-fPIC', *architecture,
        '-I' + sysconfig.get_paths()['include'], '-I' + numpy.get_include(),
        str(ROOT / 'board_tests/world_prepare.c'), '-lm', '-o', str(output),
    ], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.output, compiler=args.compiler)
    print(args.output)


if __name__ == '__main__':
    main()
