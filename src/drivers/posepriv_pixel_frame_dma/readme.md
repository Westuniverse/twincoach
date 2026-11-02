# PixelShuffle 整帧 DMA 驱动

该驱动服务于独立 kv260_pixel_shuffle_dma 硬件，采用 GPL-2.0-only。它与姿态 DPU 的共享 DMA 路径分离。

## 数据格式

输入固定为 640×360×16，共 3,686,400 字节；每拍低 12 字节为 TL/TR/BL/BR RGB，高 4 字节为 0。输出为连续的 1280×720 RGB8，共 2,764,800 字节。

TX/RX 使用 16-byte 对齐的 DMA coherent 缓冲，地址区间位于 HP0 DDR_LOW 的 2 GiB 范围内。缓冲通过实际 DMAengine provider device 分配，dma_wmb/rmb 负责发布和完成顺序。

## 设备树

- TX：xlnx,axi-dma-1.00.a，0xA0000000/0x10000，MM2S，dma cell 0。
- RX：相同 compatible，0xA0010000/0x10000，S2MM，dma cell 1。
- provider：32 位地址、24 位长度、128 位数据，无 SG 和 DRE。
- 完成中断：GIC SPI89/90；protocol_error 为 SPI91、level-high。
- consumer compatible：posepriv,pixel-shuffle-frame-dma-v1，dma-names 为 tx/rx。

设备树、中断和时钟必须与实际硬件匹配，设备树标签本身不能确认 PL 镜像身份。

## UAPI 与事务

arm64 little-endian ABI v1：32-byte XFER 为 0x40205041，64-byte GET_STATUS 为 0x80405042。

每次事务处理一帧，S2MM 先于 MM2S 启动，同一个绝对超时覆盖两通道。返回前检查 DMA_COMPLETE、协议错误和 DMA 状态；失败时保存状态并终止通道，不发布输出。GET_STATUS 返回最近事务的缓存状态。

输入先复制到 CPU-only kvmalloc 缓冲，完整检查 padding，再写入 coherent TX。用户地址错误、部分复制和非法 padding 不启动 DMA。输出复制可能部分写入，调用者应检查返回状态。

驱动使用 exclusive open、事务 mutex 和 kref 管理生命周期。remove 停止中断与 DMA 并释放资源；存活 fd 的 ioctl 返回 ENODEV。不提供 mmap、/dev/mem 或 32-bit compat ABI。

## 诊断

copy_timing 为只读模块参数，默认关闭。开启后报告输入复制、padding、DMA 准备/等待和输出复制的分段耗时；这些时间不是纯 PL 周期。

协议错误为黏性状态。恢复 PL 之前不自动重试；卸载模块不能代替硬件错误恢复。驱动代码见同目录 posepriv_pixel_frame_dma.c 与 UAPI 头文件。
