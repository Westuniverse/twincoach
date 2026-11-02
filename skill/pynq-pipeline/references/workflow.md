# 使用流程

以下命令相对本技能目录执行。目录可复制到任意位置。

## 软件检查

```bash
python3 scripts/pipeline.py inspect --contract assets/copy.json --vectors assets/copy-vectors.json
python3 scripts/pipeline.py run --contract assets/copy.json --vectors assets/copy-vectors.json --output /tmp/copy-result.json
python3 scripts/pipeline.py run --contract assets/affine.json --vectors assets/affine-vectors.json --output /tmp/affine-result.json
python3 scripts/report.py /tmp/affine-result.json --output /tmp/affine-report.md
```

默认后端为 mock，使用标准库，不导入 PYNQ、不访问设备。数值不符时退出码为 1。输出 JSON 是主要证据，Markdown 是便于阅读的摘要。

## 检查 Overlay 元数据

在安装 PYNQ 的目标板上，提供同目录、同 stem 的 .bit 与 .hwh：

```bash
python3 scripts/pipeline.py inspect --contract /path/to/contract.json --vectors /path/to/vectors.json \
  --backend pynq --bitstream /path/to/design.bit
```

inspect 仅构造 download=False 的 Overlay 并读取元数据，不下载位流，不分配 DMA 缓冲，不写 IP 寄存器。核对 ip_dict 中的 IP 路径、地址范围和类型，再填写自己的契约。

## 运行已加载的设计

```bash
python3 scripts/pipeline.py run --contract /path/to/contract.json --vectors /path/to/vectors.json \
  --backend pynq --bitstream /path/to/design.bit --output /tmp/hardware-result.json
```

设计未加载时会停止。只有已经获授权配置 PL，才在上述 run 命令追加 --program。位流应由自己的工程构建，且具备对应 DMA、TLAST 和控制语义；不得拿两个软件示例的 IP 名称猜测现役硬件。

硬件测试需要独占该 Overlay 和 DMA。该技能不搜索或终止其他程序，不改系统固件选择器，不替换内核模块。

## 输出性能轨迹

```bash
python3 scripts/pipeline.py run --contract assets/copy.json --vectors assets/copy-vectors.json \
  --sample-id input-1 --trace /tmp/completions.csv --output /tmp/result.json
```

每次确认使用新的 sample-id；同一输入重复消费应沿用相同身份。trace 的列为 id,source_ns,done_ns，可由 pipeline-metrics 直接读取。

只对通过黄金比对的事务追加轨迹，文件由单一写者顺序追加。这里的 source_ns 从主机事务开始计量，包含分配与配置开销。更换来源或计量边界时应另建轨迹，不混合软件模型与硬件结果。

## 停止条件

契约、身份、寄存器或数值不符时保留结果并检查相应阶段，不通过自动重试掩盖错误。DMA 故障不能依据进程退出就认定硬件已停止；恢复语义见 [failures.md](failures.md)。
