# 工程工具与方法

这些方法适用于 ARM + FPGA 的采集、计算和显示流水系统。

## 新输出吞吐与源龄

为输入保留源身份及时间戳，消费者确认新结果后计数。固定窗口内统计吞吐、源龄 P95 与最大确认间隔，重复源只计一次。

```bash
python3 skill/metrics.py data/examples/metrics.csv --start 0 --end 2
```

CSV 字段为 id、source_ns、done_ns；使用同一时钟域，通过 --origin-ns 指定窗口原点。窗口为左闭右开，P95 采用 sorted[floor(.95*(n-1))]。

GPU 完成与屏幕扫描具有不同计时边界。源身份被重用时应加入 epoch，避免将不同输入合并。

## 固件与驱动匹配

器件、内核、固件标识、MMIO、接口 ID、时钟和驱动 ABI 共同定义运行环境。应用启停和固件切换分别操作；修改内核、ABI、地址或位流时检查相应接口。

数值比对用于确认对应输入与运算路径，不代替人体动作准确率评估。

## 工具失败定位

根据失败命令判断受影响的构建阶段，检查已经保存的产物与报告。只有在产物对应关系明确时才复用结果；保存前的失败需要补齐相应构建步骤。

## 源码与运行目录映射

运行目录由 [sources.json](../build/sources.json) 描述，Vivado 输入由 [HDL 清单](../build/di46/sources.json) 描述。装配工具依据映射收集源码，保留模块、ABI 和运行路径的对应关系。

```bash
python3 build/stage.py --output /tmp/twincoach-runtime
```

同名文件可能属于不同实现，应以实际工程引用为准。模型生成头文件和二进制按对应资产与许可准备。
