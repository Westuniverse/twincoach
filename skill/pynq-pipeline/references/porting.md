# 跨题目与跨板卡使用

## 保持不变的部分

contracts.py、pipeline.py、report.py 不包含相机、人体姿态、模型路径或板卡型号。相同的 simple DMA 与寄存器语义下，PynqBackend 通过 Overlay 属性解析 IP，allocate 提供缓冲，驱动负责缓存同步。

## 使用者需要提供

| 输入 | 来源 |
| --- | --- |
| bit/HWH | 目标器件的实际工程输出，两者必须匹配 |
| IP 属性路径 | 目标 Overlay 的 ip_dict 和层级 |
| 数据类型、元素数、总线粒度和 DMA 容量 | RTL/HLS 接口与 DMA 配置 |
| 寄存器偏移与身份值 | HWH、驱动或接口契约 |
| 独立黄金向量 | 参考实现或明确的数值推导 |
| PYNQ 环境 | 目标板支持的镜像、驱动、设备和权限 |

## 例子

| 项目 | 测试任务 | IP 配置 | 参考数据 | 复用代码 |
| --- | --- | --- | --- | --- |
| 流通路检查 | uint32 逐元素复制 | axi_dma_0，无控制 IP | 边界整数 | 全部通用工具 |
| 参数化信号处理 | int16 仿射变换 | io/stream_dma、compute/affine | 正负值和参数 | 同一编排、PYNQ 适配和报告 |
| 图像处理 | 以一维帧表示像素流 | 自己的 DMA 与像素核 | 自己的图像黄金输出 | 契约和工具框架；按算子扩展参考模型 |

软件样例更换了任务、IP 层级和数据类型。这是跨配置的软件复用证据，不是跨板卡上板证据。

## 更换板卡

PYNQ-Z2、Kria 等平台使用不同处理器、器件、镜像和 PS/PL 配置，不能共用 bitstream。使用目标板重新生成匹配的 bit/HWH，并确认 PYNQ、DMA 和缓存 API。具备相同接口语义时可以复用主机技能代码。

## 范围边界

- 不支持 SG/cyclic DMA、多通道并发或共享 DMA 所有权。
- 不自动推断 HLS ap_start/ap_done、寄存器偏移或 TLAST。
- 不自动移植时钟、约束、器件或 Linux 驱动。
- 不把寄存器身份检查当成完整 bitstream 密码学认证。
- 不改变 TwinCoach 的 DPU、驱动或主链路，也不要求 TwinCoach 改用 PYNQ。

## 官方 API 依据

- [Overlay、IP 与 register_map](https://pynq.readthedocs.io/en/latest/pynq_package/pynq.overlay.html)
- [Simple DMA 与 TLAST](https://pynq.readthedocs.io/en/latest/pynq_libraries/dma.html)
- [allocate 与缓存同步](https://pynq.readthedocs.io/en/latest/pynq_libraries/allocate.html)
- [DMA 驱动中的同步与停止语义](https://github.com/Xilinx/PYNQ/blob/master/pynq/lib/dma.py)

工具按上述公开 API 自行实现，不复制上游驱动，不分发 PYNQ 或第三方位流。PYNQ 按自己的许可和目标环境安装。
