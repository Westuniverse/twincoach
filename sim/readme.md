# 验证

```bash
python3 build/check.py
python3 -m unittest discover -s sim -p 'test_*.py' -v
```

基础功能测试使用 Python 标准库，不需要开发板或模型。

| 文件 | 覆盖范围 |
| --- | --- |
| test_coach.py | 建议保持、观测失效、方向区分与进程传递，共 10 项测试 |
| test_metrics.py | 重复源、统计窗口边界与源龄计算 |
| rtl.json、rtl/ | 向量指标、关节指标和仿射变换的 RTL 自检配置与测试平台 |
| public.json | 源码映射、Python 解析、控制引用、文档链接与主机测试结果 |
| project.json | Vivado 工程创建、BD 校验与 wrapper 创建结果 |

[性能数据](../data/results.json) 和 build/di46/reports 提供 FPGA 资源及链路测量依据。工程创建成功表示工程输入和 BD 可用，资源及时序以实现报告为准。
