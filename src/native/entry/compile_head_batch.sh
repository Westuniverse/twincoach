#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python=${PYTHON:-python3}
include=${PYTHON_INCLUDE:-$("$python" -c 'import sysconfig; print(sysconfig.get_paths()["include"])')}
suffix=${PYTHON_EXT_SUFFIX:-$("$python" -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')}
read -r -a flags <<< "${CFLAGS:-}"
read -r -a architecture <<< "${ARCH_FLAGS:-}"
"${CC:-gcc}" -O3 "${flags[@]}" "${architecture[@]}" -shared -fPIC -std=c11 \
  -fno-fast-math -ffp-contract=off -I"$include" "$here/head_batch.c" \
  -lm -o "$here/_posepriv_head_batch$suffix"
