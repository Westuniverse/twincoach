#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"${CC:-gcc}" -O3 -fPIC -shared -std=c11 -fno-fast-math -ffp-contract=off -fno-builtin-pow "$here/v2_math.c" -lm -o "$here/libposepriv_v2_math.so"
