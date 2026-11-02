# 构建与复现

## 源码与主机验证

Python 3.12 可运行装配工具和基础测试：

```bash
python3 build/check.py
python3 -m unittest discover -s sim -p 'test_*.py' -v
python3 build/stage.py --output /tmp/twincoach-runtime
```

`stage.py` 根据 `sources.json` 恢复运行时实际使用的目录，收集公开 Python/C/C++/驱动/控制源码与通用配置。`src/native/` 中短目录名由该表映射回运行路径。默认仅装配源码；`staging.json` 如实列出外部资产需求。

## 提供板端资产

按 `build/assets.json` 的 role 提供带来源和许可的显式清单：

```json
{
  "artifacts": {
    "prefix_xmodel": {
      "source": "/absolute/path/to/prefix.xmodel",
      "origin": "Your licensed model and conversion record",
      "license": "License recorded for that artifact"
    }
  }
}
```

```bash
python3 build/stage.py --output /tmp/twincoach-board --assets /absolute/path/to/assets.json
```

完整资产要求及作用见 [资产说明](../report/assets.md)。需要编译 ARM 原生模块时，在匹配的 AArch64/VART/XRT 环境中使用装配目录各模块的 compile/build 脚本；Cortex-A53 参数保留。模型生成的 tail4_program.h 必须与 W/P、xmodel 和固件匹配。

## 创建 di46 Vivado 工程

准备 Vivado 2026.1、KV260 board files 和合法可用的工具/IP 许可；从原来源准备 DPU IP repository：

```bash
python3 build/create.py \
  --output /tmp/twincoach-di46 \
  --vivado /absolute/path/to/Vivado/2026.1/bin/vivado \
  --dpu-repo /absolute/path/to/DPUCZDX8G_ip_repo_VAI_v3.0
```

入口添加 `src/rtl/` 中 44 个实际 HDL 输入，重建 BD、validate、生成顶层 wrapper 并保存工程。增加 `--generate` 可生成 BD/IP 输出。`di46/inputs.tcl` 与 `di46/sources.json` 是实际来源清单，BD 导出脚本为 `di46/bd.tcl`。

工程创建结果见 sim/project.json，综合资源与时序见 [性能说明](../report/results.md)。工程创建、综合、实现与板端验证分别记录。

## 可选运行产物

runtime.py、restore.py 和 package.py 用于处理配套的运行产物与源码归档。使用这些工具需要自行准备对应输入；公开源码的基础测试和 stage.py 不依赖运行产物。
