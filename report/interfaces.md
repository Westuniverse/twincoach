# 系统接口

本页对应 [工程 HDL 清单](../build/fpga/sources.json) 与 [BD 定义](../build/fpga/bd.tcl)。寄存器、设备名和数值格式属于软硬件接口标识，移植时应同时更新对应驱动与调用者。

## 数据链路

| 边界 | 输入与输出 | 完成语义 | 实现依据 |
| --- | --- | --- | --- |
| 采集 → 推理 | 双路图像、源编号、时间戳和视角 | 采集成功不计作推理完成 | src/host 与 src/native/profile |
| DPU → Tail4 | 匹配模型的中间特征、W/P 参数与程序 | 网络后段完成后才消费结果 | src/native/worker 与 Tail4 驱动 |
| 姿态 → 动作 | 原始双视角关键点、有效性与时间 | 完整动作满足状态机条件后计次 | [动作协议](exercises.md) |
| 几何 → 蒙皮 | 骨矩阵与顶点 | DMA 接收完成后才消费结果 | 共享 DMA 与蒙皮契约 |
| 绘制 → 统计 | 源身份与 GPU 完成确认 | 同一源重复显示不增加新姿态数 | [统计协议](../skill/pipeline-metrics/references/protocol.md) |

## 控制地址

以下是当前 BD 中的 PS 控制地址，不是任意板卡的通用地址。

| 模块 | 基址 | 范围 |
| --- | --- | --- |
| shared_pose_accel_0 | 0xA0030000 | 4 KiB |
| tail4_operator_0 | 0xA2030000 | 4 KiB |
| roi_sampler_0 | 0xA2040000 | 4 KiB |

DPU、Tail4、ROI 与 DMA 使用 PS HP0 DDR 通路。计算与互连时钟分别见 [系统设计](design.md)，互连实验见 [DDR 比较](ddr.md)。

## Tail4 用户接口

设备节点为 `/dev/posepriv_tail4`。定义见 [UAPI](../src/drivers/posepriv_tail4/posepriv_tail4_uapi.h) 与 [寄存器表](../src/drivers/posepriv_tail4/tail4_operator_regs.h)。

| 操作 | 用途 |
| --- | --- |
| GET_INFO | 读取驱动 ABI、硬件身份、状态与 arena 信息 |
| SUBMIT | 提交带偏移、长度、量化参数与 job_tag 的阻塞任务 |
| CANCEL | 取消并取得状态、标签和 outcome |

用户偏移仅指向本设备的 4 MiB coherent arena，不接受任意 DDR 地址。完成状态需要结合 job_tag 与 outcome 判断。已接受任务发生错误时，不能把 ioctl 失败解释为硬件从未执行，也不能盲目重复提交。

驱动管理独占使用与取消排空。无法排空时保留资源并标记故障，具体生命周期以 [驱动实现](../src/drivers/posepriv_tail4/posepriv_tail4.c) 为准。

## 共享 DMA 与蒙皮

设备节点为 `/dev/posepriv_shared_pose_dma`。主机结构中的 tx_user/rx_user 是用户虚拟地址，由驱动管理 DMA 缓冲和缓存职责。

| 接口 | 用途 | 输入约束 |
| --- | --- | --- |
| SHARED_XFER | 原共享运算命令 | 按 operator_id 对长度和能力进行检查 |
| SKINNING_WHOLE_XFER | 整帧蒙皮 | op5，512 字节头部与按 16 字节组织的顶点 |
| SKINNING_STATIC_UPLOAD | 上传静态顶点 | 该 fd 持有一份 mesh，顶点区不超过 256 KiB |
| SKINNING_STATIC_RUN | 重用静态顶点 | 传递 512 字节矩阵头，使用该 fd 已上传的顶点 |

原命令和扩展命令各自使用独立的准入规则。具体 ioctl 编码、长度与输出公式见 [共享 UAPI](../src/drivers/posepriv_shared_dma/posepriv_shared_dma_uapi.h)，矩阵与顶点格式见 [蒙皮契约](../src/drivers/posepriv_shared_dma/posepriv_skinning_contract.h)。

## 可选 ROI 路径

ROI 从 BGR8 原图与 packed Q5 映射生成 256×256×3 的 NHWC RGB Q7 数据。源图、映射与目标由 [ROI UAPI](../src/drivers/posepriv_shared_dma/posepriv_roi_uapi.h) 描述，用户不直接提供 DMA 地址。

该路径通过 preview_roi_crop 显式选择，默认关闭。ROI 的独立数值结果不能替代完整链路吞吐评估。

## 配置与视角

[运行配置示例](../data/examples/runtime.json) 和 [深蹲规则](../data/examples/squat.json) 由 [源码映射](../build/sources.json) 装配到运行目录。源设备、图像旋转、正侧面评分映射与显示顺序应分别对应实际布置，不能仅根据画面左右位置确定评分视角。

显示稳定和角色修正不回写原始评分观测。观测失效和断流恢复遵循动作协议，完成建议保留到下一次动作完成。

## 扩展边界

仓库保留的独立运算模块不自动构成当前板端数据路径。判断某模块是否接入应依次查看 HDL 清单、BD 实例、驱动入口和运行配置。新增 PYNQ Overlay 可以复用 [通用技能](../skill/pynq-pipeline/SKILL.md)，其接口与当前 Linux 驱动链路分别管理。
