#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
set -eu
cd -- "$(dirname -- "$0")"
common=../../worker
/usr/bin/gcc -std=gnu11 -O3 -Wall -Wextra -Werror -fno-fast-math -mcpu=cortex-a53 -c "$common/tail4_runtime.c" -o tail4_runtime.o
/usr/bin/g++ -std=c++17 -O3 -Wall -Wextra -Werror -fno-fast-math -mcpu=cortex-a53 \
  ../hybrid_pair_worker.cpp tail4_runtime.o -o hybrid_pair_worker \
  -L/opt/vitis-ai-compat/lib -L/lib -lvart-runner -lvitis_ai_library-runner_helper \
  -lvart-util -lxir -lunilog -lboost_filesystem -lpthread -lm
