#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
exec /usr/bin/gcc -std=c11 -O3 -Wall -Wextra -Werror -fPIC -shared -fno-fast-math -fno-math-errno -ffp-contract=off \
  -mcpu=cortex-a53 -DPOSEPRIV_CROP_NEON -DPOSEPRIV_CROP_COORD_PAIR -DPOSEPRIV_CROP_FAST_NEIGHBORS \
  crop_q7.c -o libposepriv_crop_q7_neon_fast.so -lm
