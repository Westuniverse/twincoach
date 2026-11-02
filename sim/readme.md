# 验证

```bash
python3 build/check.py
python3 build/results.py
python3 -m unittest discover -s sim -p 'test_*.py' -v
```

基础功能测试使用 Python 标准库，不需要开发板或模型。

| 文件 | 覆盖范围 |
| --- | --- |
| test_coach.py | 建议保持、观测失效、方向区分与进程传递，共 10 项测试 |
| test_squat.py | 完整动作只计一次、断流不拼接半次、hold 不延长有效期限 |
| test_skill.py | 两种任务、PYNQ API 替身、故障和独立目录复用 |
| test_board.py | 配置覆盖、无连接预览与带空格的远端路径 |
| test_build.py | 运行模式依赖选择、编译器与 SDK 路径适配 |
| test_metrics.py | 重复源、统计窗口边界与源龄计算 |
| run.py、rtl.json、rtl/ | 向量指标、关节指标和仿射变换的 RTL 自检配置与测试平台 |
| results.json | 公开完成轨迹复算、资源及时序核对、DDR 差值 |
| rtl-results.json | 三个 RTL 测试平台的实际退出码与 PASS 摘要 |
| public.json | 源码映射、Python 解析、控制引用、文档链接与主机测试结果 |
| project.json | Vivado 工程创建、BD 校验与 wrapper 创建结果 |

[性能数据](../data/results.json) 和 build/fpga/reports 提供 FPGA 资源及链路测量依据。工程创建成功表示工程输入和 BD 可用，资源及时序以实现报告为准。

## RTL 复现

```bash
python3 sim/run.py --output /tmp/twincoach-rtl
```

需要 Verilator 与 make/g++。输出目录保存每个测试的编译和运行日志、命令及 results.json；默认每阶段超时 120 秒。保存摘要中的日志路径相对这类运行输出目录。测试配置只覆盖三个单元，不代表完整网络通过。

详细测量与各层证据见 [证据索引](../report/evidence.md)。
