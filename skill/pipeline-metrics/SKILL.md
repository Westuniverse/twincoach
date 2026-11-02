---
name: pipeline-metrics
description: Compute fresh-output throughput, source-age percentiles, and completion gaps from fixed-window timestamp traces. Use to compare embedded or FPGA pipelines without confusing repeated output, input capture rate, and completed work.
license: GPL-2.0-only
---

# 流水线性能分析

适用于摄像头、推理、DMA、信号处理及显示等带源身份和完成确认的流水线。无需 TwinCoach、模型或开发板。

1. 确认 source_ns 和 done_ns 使用同一时钟域，完成点具有明确含义。
2. 用 id 标识输入源；若源编号会重用，CSV 同时提供 epoch。
3. 显式选择统计窗口；先确定窗口再比较方案，不能为了提高结果临时裁剪。
4. 运行 scripts/measure.py，报告更新数、窗口吞吐、P95 源龄与最大确认间隔。
5. 使用者必须区分采集、入队、计算完成、GPU 完成和物理显示扫描的边界。

格式、公式、示例与失效条件见 [protocol.md](references/protocol.md)。

```bash
python3 scripts/measure.py trace.csv --origin-first --start 0 --end 10 --output metrics.json
```

该目录可单独复制到其他工程。根目录 skill/metrics.py 是 TwinCoach 的兼容入口，独立使用不需要它。
