# KV260 裸机自检程序

这里的程序用于把 PS—DDR—AXI DMA—PL 算子—AXI DMA—DDR 链路单独调通，暂时不承载摄像头、姿态模型或最终界面。

`posepriv_dma_test/main.c` 使用轮询模式：

1. 初始化 Simple DMA，并关闭未使用的中断；
2. 刷新发送和接收缓存；
3. 先启动 S2MM 接收，再启动 MM2S 发送；
4. 分别发送 1、2、4 个连续的 128 位数据拍；4 拍模拟同步正面/侧面各两个膝关节；
5. 使接收缓存失效，并与同一 ABI 的整数黄金模型逐位比较。

完成三种长度的首次事务后，程序额外执行 4 次预热和 64 次 64 字节稳态事务。每次仍执行完整缓存维护和逐位比较，串口只输出一次最小、总计和最大 tick 摘要，避免日志本身干扰测量。

每个事务同时输出 DMA 计时器 tick，计时边界从缓存刷新完成后、启动 S2MM 之前开始，到两个 DMA 通道都结束时停止。启动时输出 `COUNTS_PER_SECOND`，可据此换算微秒。该数字包含两次 Simple DMA 启动和轮询等待，不包含缓存刷新、结果失效和软件黄金比较。

构建命令：

```powershell
& 'F:\vivado2026_1\2026.1\Vitis\bin\vitis.bat' `
  -s scripts\vitis\build_standalone_dma_test.py
```

Vitis 2026.1 已禁用 XSCT，因此构建脚本使用 Vitis Python API。为避开 Windows 对象路径限制，生成工作区为仓库根目录下的 `.v`，不提交 Git。

当前已验证生成：

```text
.v/dma_test/build/dma_test.elf
.v/pmufw/build/pmufw.elf
```

生成 SD 卡启动文件：

```powershell
powershell -ExecutionPolicy Bypass `
  -File scripts\vitis\package_standalone_boot.ps1
```

输出为 `build/boot/standalone_dma/BOOT.BIN`，其中依次包含 FSBL、PMUFW、PL bitstream 和 A53 裸机自检应用。

板端串口预期输出：

```text
Timer frequency: 99999001 Hz
PASS: 16-byte transaction, DMA ticks=<实测值>
PASS: 32-byte transaction, DMA ticks=<实测值>
PASS: 64-byte transaction, DMA ticks=<实测值>
STEADY: 64-byte transaction, iterations=64, min_ticks=<实测值>, total_ticks=<实测值>, max_ticks=<实测值>
POSEPRIV_DMA_TEST: PASS
```

解析器使用 `total_ticks / iterations` 计算稳态平均值；不要把最小值单独当成系统可持续性能。工具链问题和解决方法见 [`docs/vitis_2026_1_troubleshooting.md`](../../docs/vitis_2026_1_troubleshooting.md)。

## 头像蒙皮 DMA 自检

`posepriv_avatar_dma_test/main.c` 对应独立的 `kv260_avatar_dma` XSA：A53 控制 AXI
 DMA，把 16 项矩阵前导和 8 个顶点请求送入 `avatar_skinning_packet_axis`，再从 DDR
读取 128 bit 补齐结果（本批次为 640 字节 TX、128 字节 RX）。它与上面的关节指标程序
分开构建，不要求摄像头、DPU 或 HDMI。

构建顺序：

```powershell
& 'F:\vivado2026_1\2026.1\Vitis\bin\vitis.bat' `
  -s scripts\vitis\build_standalone_avatar_dma_test.py

powershell -ExecutionPolicy Bypass `
  -File scripts\vitis\package_avatar_dma_boot.ps1
```

当前离线产物为 `build/vivado/kv260_avatar_dma/posepriv_avatar_dma.xsa`、
`.v_avatar/avatar_dma_test/build/avatar_dma_test.elf`、PMUFW ELF 和
`build/boot/avatar_dma/BOOT.BIN`。JTAG 下载时使用：

```powershell
& 'F:\vivado2026_1\2026.1\Vitis\bin\xsdb.bat' `
  scripts\vitis\run_avatar_dma_jtag.tcl
```

串口接 KV260 当前实测控制台 COM11，115200 8N1；成功标记必须是：

```text
POSEPRIV_AVATAR_DMA_TEST: PASS
```

没有这行板端日志时，只能报告 XSA/ELF/BOOT.BIN 构建通过，不能报告裸机 DMA 或端到端
数字人通过。程序当前使用 8 个确定性顶点验证矩阵平移和输出保留位；真实网格的 8 顶点
软件—RTL 字节桥由 `scripts/verify_avatar_rtl_bridge.py` 验证，二者边界不要混写。

## 运动统计 DMA 自检

`posepriv_motion_stats_dma_test/main.c` 对应 `kv260_motion_stats_dma` 独立端点。它向
`temporal_motion_stats_axis_ip` 发送 3 个 128 bit 统计拍（加速度平方 10、25、7，
joint=5，view=1），PL 聚合后由 S2MM 返回一个摘要拍。软件逐位检查 `sum=42`、
`max=25`、`count=3` 以及 joint/view 元数据。

```powershell
& 'F:\vivado2026_1\2026.1\Vitis\bin\vitis.bat' `
  -s scripts\vitis\build_standalone_motion_stats_test.py

powershell -ExecutionPolicy Bypass `
  -File scripts\vitis\package_motion_stats_boot.ps1
```

输出为 `.v_motion_stats/motion_stats_dma_test/build/motion_stats_dma_test.elf` 和
`build/boot/motion_stats_dma/BOOT.BIN`。JTAG、SD 卡和串口验收步骤见
[`docs/kv260_motion_stats_board_test.md`](../../docs/kv260_motion_stats_board_test.md)。
必须看到 `POSEPRIV_MOTION_STATS_DMA_TEST: PASS` 才算板端闭环通过；单独生成 XSA、
bitstream 或 ELF 只能算离线构建证据。
