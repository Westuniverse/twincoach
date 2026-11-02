# 模型协作与技能提炼

## 实际任务

用户提出：“skill这一块你再优化一下看看能不能做的更好一些，就是最好能够有非常详细而且清晰的架构”，并要求重新查看评分细则、重点完善技能。

协作范围为整理通用工具、说明架构并执行主机验证。姿态算法、模型权重与已保存的板端测量未因技能整理而改变。

## 判断与实现

| 输入或观察 | 模型给出的处理 | 可检查产物 |
| --- | --- | --- |
| 既有性能统计依赖项目入口，难以独立取用 | 提取自包含的统计技能，保留原入口兼容性 | [pipeline-metrics](../skill/pipeline-metrics/SKILL.md)、[metrics.py](../skill/metrics.py) |
| 通用工具应脱离具体业务和板卡名称 | 以接口契约连接 Overlay、DMA 和黄金向量，按后端隔离设备访问 | [架构](../skill/pynq-pipeline/references/architecture.md)、[契约](../skill/pynq-pipeline/references/contract.md) |
| 直接按缓冲长度比对，不能排除提前 TLAST 的短帧 | 使用驱动完成后的实际接收字节数确认整帧，再做数值比对 | [硬件后端](../skill/pynq-pipeline/scripts/backends.py)、[短帧测试](../sim/test_skill.py) |
| 故障发生在设备执行之前，报告不能称为软件模型执行 | 根据后端与实际阶段标记“PYNQ 设备执行未开始” | [报告生成器](../skill/pynq-pipeline/scripts/report.py) |
| 同一源编号可能在采集重启后重复 | 将 epoch 与 id 共同作为身份；窗口外的旧输出重复消费不计新结果 | [统计协议](../skill/pipeline-metrics/references/protocol.md)、[统计测试](../sim/test_metrics.py) |

这些是工具设计与代码审查中发现的问题和修正，不表示已在实际 PYNQ 板卡上观察到相同故障。

## 模型输出与纠错边界

模型将技能分为入口、参考、脚本与样例，并使用两组独立黄金向量检验数值。实现后运行 API 替身测试，检查 Overlay 不隐式下载、接收先行、缓存职责、短帧拒绝和缓冲释放；再将两个技能复制到临时目录执行，检查对原项目路径的依赖。

报告生成器的环境标签在审查后修正；短帧检查补充到实际接收完成的接口边界。没有通过修改黄金值或隐藏失败来取得通过。测试可由 [主机检查入口](../build/check.py) 重跑，检查结果见 [sim/public.json](../sim/public.json)。

## 如何继续复用

新任务从自己的 IP 接口与独立向量开始，按 [任务模板](../skill/pynq-pipeline/references/prompts.md) 让模型生成契约或定位具体阶段。修改后的工具以可执行结果确认，PYNQ 实板通过、持续吞吐和跨板卡能力分别提供证据。详细范围见 [验证说明](../skill/pynq-pipeline/references/validation.md)。
