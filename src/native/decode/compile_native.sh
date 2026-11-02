#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
cxx=${CXX:-g++}
read -r -a architecture <<< "${ARCH_FLAGS--mcpu=cortex-a53}"
read -r -a flags <<< "${CXXFLAGS:-}"
"$cxx" -std=c++17 -O3 "${flags[@]}" -Wall -Wextra -Werror -fno-fast-math -ffp-contract=off \
  "${architecture[@]}" -shared -fPIC decode_leaf.cpp -o libtail4_decode.so
