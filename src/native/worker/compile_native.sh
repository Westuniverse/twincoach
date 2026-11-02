#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
cc=${CC:-gcc}
cxx=${CXX:-g++}
vart_prefix=${VART_PREFIX:-/opt/vitis-ai-compat}
read -r -a architecture <<< "${ARCH_FLAGS--mcpu=cortex-a53}"
read -r -a cflags <<< "${CFLAGS:-}"
read -r -a cxxflags <<< "${CXXFLAGS:-}"
read -r -a ldflags <<< "${LDFLAGS:-}"
"$cc" -std=gnu11 -O3 "${cflags[@]}" -Wall -Wextra -Werror -fno-fast-math "${architecture[@]}" \
  -c tail4_runtime.c -o tail4_runtime.o
"$cxx" -std=c++17 -O3 "${cxxflags[@]}" -Wall -Wextra -Werror -fno-fast-math "${architecture[@]}" \
  -I"$vart_prefix/include" tail4_shm_worker.cpp tail4_runtime.o -o tail4_shm_worker \
  -L"$vart_prefix/lib" "${ldflags[@]}" \
  -lvart-runner -lvitis_ai_library-runner_helper -lvart-util -lxir -lunilog \
  -lboost_filesystem -lpthread -lm
