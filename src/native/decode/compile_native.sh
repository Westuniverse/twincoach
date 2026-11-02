#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
/usr/bin/g++ -std=c++17 -O3 -Wall -Wextra -Werror -fno-fast-math -ffp-contract=off \
  -mcpu=cortex-a53 -shared -fPIC decode_leaf.cpp -o libtail4_decode.so
