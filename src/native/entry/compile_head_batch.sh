#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
py=${PYTHON:-python3}
ext=$($py-config --extension-suffix)
${CC:-gcc} -O3 -shared -fPIC -std=c11 -fno-fast-math -ffp-contract=off $($py-config --includes) "$here/head_batch.c" -lm -o "$here/_posepriv_head_batch$ext"
