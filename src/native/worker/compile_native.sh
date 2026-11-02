#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
/usr/bin/gcc -std=gnu11 -O3 -Wall -Wextra -Werror -fno-fast-math -mcpu=cortex-a53 \
  -c tail4_runtime.c -o tail4_runtime.o
/usr/bin/g++ -std=c++17 -O3 -Wall -Wextra -Werror -fno-fast-math -mcpu=cortex-a53 \
  tail4_shm_worker.cpp tail4_runtime.o -o tail4_shm_worker \
  -L/opt/vitis-ai-compat/lib -L/lib \
  -lvart-runner -lvitis_ai_library-runner_helper -lvart-util -lxir -lunilog \
  -lboost_filesystem -lpthread -lm
