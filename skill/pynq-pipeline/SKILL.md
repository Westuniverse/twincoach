---
name: pynq-pipeline
description: Inspect and verify PYNQ Overlay and simple AXI DMA stream paths using explicit interface contracts, typed golden vectors, and software or hardware evidence. Use for reusable PYNQ host workflows and stream-path bring-up across compatible boards.
license: GPL-2.0-only
---

# PYNQ 数据通路

使用契约定义 Overlay IP、寄存器、数据类型、帧长与 DMA 容量。技能自身不依赖 TwinCoach、特定模型或固定板卡名称。

## 选择入口

| 任务 | 先读取 | 工具 |
| --- | --- | --- |
| 理解架构和控制/数据/证据流 | [architecture.md](references/architecture.md) | 无硬件访问 |
| 接入自己的 IP 与测试向量 | [contract.md](references/contract.md) | scripts/contracts.py |
| 验证接口与数值 | [workflow.md](references/workflow.md) | scripts/pipeline.py |
| 移植到另一设计或板卡 | [porting.md](references/porting.md) | 修改契约与外部 Overlay |
| 处理 DMA、缓存、身份或数值错误 | [failures.md](references/failures.md) | 检查对应阶段，复用未变化证据 |
| 判断已经验证了什么 | [validation.md](references/validation.md) | 软件、API 替身、真实硬件分别记录 |
| 为模型助手提供明确任务 | [prompts.md](references/prompts.md) | 任务模板与证据要求 |

## 默认工作流

1. 读取契约和独立黄金向量，检查类型、长度、容量与寄存器参数。
2. 用 mock 后端确认测试数据和报告链路。两个样例位于 assets/。
3. 在已授权的板端环境检查 `.bit/.hwh` 元数据；`inspect` 不下载位流。
4. 硬件运行要求 Overlay 已加载。需要配置 PL 时，只有明确授权的部署才使用 `--program`。
5. 逐元素比对输出，保留完整 JSON；使用 report.py 生成摘要。
6. 需要吞吐与源龄分析时输出 CSV，交给独立的 pipeline-metrics 技能处理。

## 关键约定

- 支持等长、一维整型数据帧和 simple AXI DMA；不自动适配 SG/cyclic DMA 或任意算子。
- 先准备接收，再触发算子和发送，完成后读取输出。IP 必须正确产生 TLAST。
- PYNQ DMA 驱动管理 TX flush 和 RX invalidate；此层不重复同步缓存。
- 只有完成或确认通道停止后才能释放缓冲。`wait/stop` 遵循 PYNQ 的阻塞语义，不承诺通用硬件超时恢复。
- mock 的通过和墙钟时间不代表板端通过或硬件性能；保留 `hardware_executed` 与计量边界。
- 位流、HWH、DMA 和 IP 必须属于当前获授权独占的设计。身份不符时停止，不尝试写寄存器或自动恢复。

## 独立取用

整个目录可单独复制到其他工程或 Codex 的 skills 目录。运行工具只依赖本目录；mock 使用标准库，PYNQ 仅在显式选择硬件后端时导入。
