# 工程证据与验证范围

## 结论与入口

| 结论 | 依据 | 复现或检查入口 | 范围 |
| --- | --- | --- | --- |
| 功能与软硬件结构 | [系统设计](design.md)、[接口](interfaces.md) | [HDL 清单](../build/fpga/sources.json)、BD 与驱动源码 | 系统实现和接口定义 |
| 动作计次与反馈规则 | [动作协议](exercises.md) | sim/test_squat.py、sim/test_coach.py | 确定性逻辑，不代表真人准确率 |
| 源码与控制配置可运行 | [主机结果](../sim/public.json) | python3 build/check.py | 无模型、无板卡的主机功能 |
| RTL 数值和反压行为 | [RTL 摘要](../sim/rtl-results.json) | python3 sim/run.py --output /tmp/twincoach-rtl | 三个声明的单元测试 |
| FPGA 工程输入与 BD 可重建 | [工程结果](../sim/project.json) | build/create.py | 已保存的工程创建结果；不代表重新综合通过 |
| 资源及时序 | [资源报告](../build/fpga/reports/utilization.rpt)、[时序报告](../build/fpga/reports/timing.rpt) | python3 build/results.py | 保存的实现报告 |
| 录制输入的实际完成性能 | [汇总](../data/results.json)、[完成轨迹](../data/replay.csv) | python3 skill/metrics.py data/replay.csv --start 15 --end 44 | 保存的真实推理与 GPU 完成记录，无原始视频 |
| 优化对照与取舍 | [优化依据](optimization.md)、[对照数据](../data/comparison.json) | build/results.py 计算差值 | DDR 两配置各一次保存汇总，未附两组逐样本轨迹 |
| 技能可脱离业务复用 | [技能架构](../skill/readme.md)、[样例结果](../sim/skill.json) | sim/test_skill.py | 软件模型、API 替身和独立目录运行 |
| 模型协作与纠错过程 | [协作案例](collaboration.md) | 对照实际工具、向量和测试 | 整理后的协作案例，非完整原始对话转录 |

## 如何阅读结果

主机检查、RTL 单元、FPGA 工程创建、保存的实现报告与板端记录属于不同层次。新执行的检查不改变保存的板端指标；工程创建成功也不等同于重新综合或重新上板。

[结果核对记录](../sim/results.json) 从公开轨迹重新计算吞吐、P95 源龄和最大确认间隔，并从报告解析资源和时序。最低滚动一秒更新数保留原板端汇总口径，本工具没有另行定义滑窗边界来替代它。

公开数据的证据索引见 [evidence.json](../data/evidence.json)，供脚本与其他工程按路径查找。完整步骤见 [复现说明](reproduce.md)。

## 尚待独立测量

持续真人识别更新率、独立标注计次/规范度准确率、整板功耗、物理屏幕延迟，以及通用 PYNQ 技能的真实跨板卡运行，没有在这里用现有结果替代。它们分别需要对应输入、测量边界和实际输出。
