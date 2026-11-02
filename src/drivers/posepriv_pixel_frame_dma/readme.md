# 独立 PixelShuffle 整帧 DMA 候选驱动

仅适用于已有的独立 `kv260_pixel_shuffle_dma` 工程，不适用于 di38s 的共享姿态
DMA。GPL-2.0-only；复用项目自有 shared DMA 的 copy-based 所有权模式和
`posepriv_dma_bounds.h`，未引入第三方依赖、模型、软件或 IP。

输入固定 640×360×16 = 3,686,400 字节；每拍低 12 字节为 TL/TR/BL/BR RGB，
高 4 字节为 0。输出连续 1280×720 RGB8 = 2,764,800 字节。两个缓冲均为
16-byte 对齐的完整 DMA API coherent 分配，且分别按整个地址区间检查 HP0
DDR_LOW 的 2 GiB 上界。分配/释放使用实际 DMAengine provider device，不是
用户指针或臆造物理地址。coherent API 负责缓存可见性，另用 dma_wmb/rmb
保证顺序；不对 coherent 缓冲错用 streaming sync，不要求虚假的 dma-coherent。

## 所需独立设备树契约

2026-10-06 已从同一 XSA 生成独立 Linux DT/FIT，启动候选 `psf20261006`，
并在同一 boot 完成 8 帧整帧黄金。详见[板端报告](../../docs/pixel_shuffle_board_golden_2026-10-06.md)
与[原始黄金 JSON](../../benchmarks/pixel_shuffle_board_golden_2026-10-06.json)。
测试后已恢复 di38s 核心选择器和启动会话，独立候选保留；不是集成到 DPU 镜像。

必须由独立候选的真实 HWH、时钟、GIC 和基础 Linux DT 生成并确认，不能将下列
节点贴进正在运行的 di38s。两个 provider 各仅有一个 available channel：

- TX `xlnx,axi-dma-1.00.a`，reg `0xA0000000/0x10000`，MM2S 子节点，dma cell 0。
- RX 同 compatible，reg `0xA0010000/0x10000`，S2MM 子节点，dma cell 1。
- provider `xlnx,addrwidth=32`、`xlnx,sg-length-width=24`，无 include-sg；子节点
  `xlnx,datawidth=128`、无 include-dre；DMA 完成 IRQ 分别为 GIC SPI89/90。
- consumer 节点名 `posepriv_pixel_shuffle_frame_dma`，compatible
  `posepriv,pixel-shuffle-frame-dma-v1`；`dma-names="tx","rx"` 分别引用上述
  provider/channel；`posepriv,input-width=640`、`posepriv,input-height=360`。
- consumer `interrupt-names="protocol_error"`，GIC SPI91、level-high；它对应
  HWH concat In2，硬件 INTID123，不是可硬编码的 Linux IRQ 号。

现有 RTL 没有 ID/protocol_error 可读寄存器；DT 标签不能证明实际 PL 镜像身份。
每次真正加载时仍须保存同 boot 的 FPGA Manager 写入、镜像/DT、provider、中断、
地址和运行日志。驱动通过专用 IRQ 锁存、GIC pending/active 检查协议错误；
不支持该观察接口即失败，不伪造 0。黏性错误只 mask 一次并唤醒等待者。
故障后不会自动 reset/retry，重新打开只能读取缓存诊断。显式 PL 恢复之前，
仅卸载/重载模块不代表错误已清除。

## 编译与验证

使用已存在的目标内核头文件及编译器，不安装工具：

```sh
cc -std=c11 -O2 -Wall -Wextra -Werror board_tests/pixel_frame_dma_contract_test.c -o /tmp/posepriv_pixel_frame_contract_test
/tmp/posepriv_pixel_frame_contract_test
make -C /lib/modules/$(uname -r)/build M=$PWD/board_drivers/posepriv_pixel_frame_dma W=1 modules
python3 scripts/verify_pixel_frame_dma.py --host-reference --frames 8 --output /tmp/posepriv_pixel_host.json
```

上述仅证明 CPU 参考/事务状态机、ABI 和目标内核编译，**不运行 DMA**。生成 ko
留在忽略目录或板端项目 tmp，不进 Git。无需 Vivado 重建。

仅在当前确认为独立镜像且模块正确绑定的启动会话中执行：

```sh
sudo python3 scripts/verify_pixel_frame_dma.py --run-board --frames 8 --output /tmp/posepriv_pixel_dma.json
```

程序不加载模块/PL、不重启。若误在当前 di38s 调用，会在打开设备前拒绝，不应
为绕过拒绝而修改绑定。必须逐字节通过、同 boot、TX/RX 完成中断增长、协议错误
中断不增长且缓存状态 clean，才能报告整帧 DDR/DMA 黄金。timing 明确分为
kernel（准备+等待）与 ioctl（拷贝+等待+拷贝），都不是纯 PL 周期；软件参考、
GET_STATUS 不计入它们。8 帧是黄金起点，不能当充分 P95 性能样本。

需要定位 copy 开销时，在已确认独立候选的会话中显式加载自有模块：

```sh
sudo insmod ./posepriv_pixel_frame_dma.ko copy_timing=1
cat /sys/module/posepriv_pixel_frame_dma/parameters/copy_timing
```

`copy_timing` 默认关闭，0444 只读；开启后仅 XFER 在事务结束打印一行，GET_STATUS
不计时。分段总和从 ioctl 入口到输出复制结束，排除最终打印/解锁/返回；阶段数和
result 保留部分失败，不能当完整成功。core 段直接复用原 elapsed_ns，不改 UAPI、
超时或 RX-first。`scripts/analyze_pixel_frame_copy_timing.py` 以同 boot 实际黄金
JSON 和内核日志核对账目；空日志、部分阶段、错误状态、IRQ 或计时不一致均拒绝。
首次8帧实测 padding 约100.63ms，占该边界约78%；这是诊断而非优化收益，详见
[分段板端报告](../../docs/pixel_frame_copy_timing_2026-10-06.md)。

后续只将已确认4-byte对齐的私有输入padding以memcpy读取__u32再判零；先运行时检查
对齐才声明GCC对齐，不使用类型双关，不对齐CPU调用仍走原字节路径。1152个非法位/
位置与独立字节oracle一致，ASan/UBSan及目标编译通过。实际候选反汇编确认单次ldr w。
同boot A/B/B/A共32帧黄金通过，padding合并均值101.85→25.63ms，完整ioctl
130.71→54.26ms；仅是诊断开启的copy路径收益，不是纯PL、30FPS或完整SR。见
[单因素优化与恢复报告](../../docs/pixel_frame_padding_word_2026-10-06.md)。

后续输入暂存候选改为：copy_from_user → CPU-only私有kvmalloc缓冲 → 完整padding
校验 → memcpy到原coherent TX → 原RX-first事务。普通暂存不能直接交给DMA，
不改变provider分配、低2GiB/16-byte约束、coherent可见性或dma_wmb/rmb；每设备
额外3,686,400字节、每有效帧额外一次完整memcpy，不是零拷贝或DDR流量下降。
分配失败ENOMEM、部分复制EFAULT和非法paddingEINVAL均不启动DMA；remove在
原mutex内释放kvfree并清指针，旧fd返回ENODEV。源契约不冒充真实内核分配失败注入。
`poison_ns`为历史字段，在该候选中还包含校验后TX发布；比较完整ioctl时边界未变，
但不能把阶段4说成单独RX memset。默认copy_timing仍关闭。详情与验收状态见
[输入暂存报告](../../docs/pixel_frame_input_staging_2026-10-06.md)。

显式内核负例由 `board_tests/pixel_frame_input_rejection_test.c` 编译得到，仅传入
`--run-board` 才直接用ioctl提交96个非法padding位、非法地址和末字节跨入
PROT_NONE页的部分复制；逐次检查
errno、缓存状态和输出哨兵不变。它不调用用户态padding验证器、不发送有效DMA、
不配置PL/模块或重试。实际IRQ无增量由具名板端编排另查；仅编译不等于负例板测通过。

已撤回的输出暂存实验在原DMA完成、dma_rmb、错误检查与快照后，将coherent RX完整memcpy
到CPU-only kvmalloc快照，再copy_to_user；两次复制都纳入copy_rx_ns，不改变UAPI或
原kernel计时。额外2,764,800字节，输入/输出暂存合计6,451,200字节普通内存；每帧
新增一次完整输出memcpy，不能称零拷贝。分配失败和remove复用同一清理/互斥路径。
同boot ABBA完整ioctl均值慢5.20%，故生产驱动保持输入暂存+原直接输出复制，未留
输出暂存分支或模块开关。负结果与可复现补丁见
[输出暂存实验](../../docs/pixel_frame_output_staging_2026-10-06.md)。

`board_tests/pixel_frame_output_fault_test.c --run-board` 仅用于已确认的独立镜像，
发送一个合法整帧DMA，但输出末字节越入PROT_NONE页，实际检查copy-out EFAULT、
已完成DMA状态和可见输出黄金前缀；不自动重试/reset。输出复制故障可能部分写入，
不是原子发布保证。IRQ必须另核对TX/RX各+1、协议+0，不以源契约/编译替代板测。

## UAPI 与生命周期

arm64 little-endian v1，32-byte XFER `0x40205041`，64-byte GET_STATUS
`0x80405042`。每次一次固定整帧，S2MM 先启动，同一个绝对超时覆盖两通道；
完成已送达时先检查 completion，不能因等待另一通道而误判超时。返回前校验
DMA_COMPLETE、协议错误和 DMA 状态；失败先快照状态再 terminate 两通道，
不发布输出。GET_STATUS 为最近事务的缓存快照，不是实时寄存器读取。

exclusive open + 一个事务 mutex；remove 先停止 IRQ，再终止 DMA/释放缓冲和
通道。存活 fd 保留 kref 状态，卸载后 ioctl 返回 ENODEV；没有 mmap、/dev/mem
或 32-bit compat ABI。测试中的 fake 和目标板上的 CPU C 自检都不是 AXI 总线仿真。

Linux 5.15 IRQ 查询/同步及 GIC pending/active 语义参考其
[IRQ 实现](https://github.com/torvalds/linux/blob/v5.15/kernel/irq/manage.c)和
[GIC 实现](https://github.com/torvalds/linux/blob/v5.15/drivers/irqchip/irq-gic.c)；
DMASR/DMAengine 行为参考
[Xilinx DMA 驱动](https://github.com/torvalds/linux/blob/v5.15/drivers/dma/xilinx/xilinx_dma.c)。
目标 Ubuntu Xilinx 内核编译通过不替代实际 IRQ/驱动行为验收。

没有卷积；这不是完整超分，也不会进入姿态 DPU 的必经路径或改变评分时间戳。
