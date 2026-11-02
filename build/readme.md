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

## 创建 FPGA 工程

准备与平台配置匹配的 Vivado、板卡文件和工具/IP 许可，以及 DPU IP repository：

```bash
python3 build/create.py \
  --output /tmp/twincoach-fpga \
  --vivado /absolute/path/to/Vivado/bin/vivado \
  --dpu-repo /absolute/path/to/DPUCZDX8G_ip_repo_VAI_v3.0
```

入口添加 `src/rtl/` 中 44 个实际 HDL 输入，重建 BD、validate、生成顶层 wrapper 并保存工程。增加 `--generate` 可生成 BD/IP 输出。`fpga/inputs.tcl` 与 `fpga/sources.json` 是实际来源清单，BD 导出脚本为 `fpga/bd.tcl`。

工程创建结果见 sim/project.json，综合资源与时序见 [性能说明](../report/results.md)。工程创建、综合、实现与板端验证分别记录。

## 平台配置

默认平台由 data/platform.json 指定，包含器件、板卡定义和工具兼容要求。通过 --platform 指定其他配置文件。--vivado 和 --dpu-repo 始终由调用者指定，构建不依赖开发者安装路径。

目标平台为 KV260。适配其他硬件还需要调整 BD 中的处理器、时钟、存储、约束和驱动接口；修改平台参数本身不会自动完成硬件移植。

## 导入与装配检查

```bash
python3 -m pip install -r build/requirements.txt
python3 /tmp/twincoach-runtime/native/hybrid/stream_profile/check_source_closure.py --package /tmp/twincoach-runtime
```

运行目录使用 native/actor、native/worker、native/profile/entry 等稳定名称，不含日期或实验后缀。源码、配置和资产目标路径共用 build/sources.json 与 build/assets.json。

## 打包

```bash
python3 build/package.py --output /tmp/twincoach-src.tar.gz
python3 build/runtime.py --output /tmp/twincoach-runtime.tar.gz --assets /absolute/path/to/assets.json
```

package.py 只打包 Git 管理的项目文件。runtime.py 根据源码与显式资产清单装配运行包，无需私有基线归档。

