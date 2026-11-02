#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
cc=${CC:-gcc}
read -r -a architecture <<< "${ARCH_FLAGS--mcpu=cortex-a53}"
read -r -a flags <<< "${CFLAGS:-}"
exec "$cc" -std=c11 -O3 "${flags[@]}" -Wall -Wextra -Werror -fPIC -shared -fno-fast-math -fno-math-errno -ffp-contract=off \
  "${architecture[@]}" -DPOSEPRIV_CROP_NEON -DPOSEPRIV_CROP_COORD_PAIR -DPOSEPRIV_CROP_FAST_NEIGHBORS \
  crop_q7.c -o libposepriv_crop_q7_neon_fast.so -lm
