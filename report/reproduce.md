# 从源码到运行

## 选择复现范围

| 目标 | 环境 | 能取得的结果 |
| --- | --- | --- |
| 主机功能与保存结果复算 | Python 3.12、标准库 | 动作逻辑、控制配置、独立技能及测量汇总核对 |
| RTL 单元自检 | Linux、Verilator、C++ 工具链 | 三个算术/握手测试平台的实际 PASS 与日志 |
| FPGA 工程创建 | 匹配 Vivado、KV260 板卡文件、DPU IP 与许可 | 工程、BD 校验与 wrapper |
| 板端运行 | KV260、匹配系统与外部资产、双摄、显示器 | 实际采集、推理、计次与显示 |

下列主机命令以 Linux 路径为例。控制脚本也支持 Windows 和 macOS，路径按本机调整。

## 获取与主机检查

```bash
git clone https://github.com/Westuniverse/twincoach.git
cd twincoach
python3 build/check.py
python3 build/results.py --output /tmp/twincoach-results.json
```

check.py 的结果应为 passed=true。results.py 从公开的 [replay.csv](../data/replay.csv) 重算保存的真实完成轨迹，并核对实现报告；它不启动模型或连接板卡。数据只含源身份、epoch 和相对纳秒时间，不含原始画面、关键点或标定。

单独复算原窗口：

```bash
python3 skill/metrics.py data/replay.csv --start 15 --end 44
```

预期新输出为 668、更新率为 23.034483 次/s。完整输入条件见 [性能说明](results.md)。

## RTL 自检

准备 Verilator 与 make/g++ 后，在一个尚不存在的输出目录运行：

```bash
python3 sim/run.py --output /tmp/twincoach-rtl
```

入口固定采用现有配置声明的 Verilator 后端。每个测试保留 compile.log、run.log、完整命令和 results.json，要求编译成功、运行退出码为零且出现对应 TB PASS；默认单阶段超时为 120 秒，可用 --timeout 指定。

三个测试只验证向量指标、关节指标与仿射变换单元，不覆盖整网、真实 DDR、布局布线或板端系统。[RTL 结果](../sim/rtl-results.json) 保存已有执行摘要，重新运行的日志位于调用者指定目录。

## 装配源码与资产

```bash
python3 build/stage.py --output /tmp/twincoach-source --mode full33
```

默认模式列出 28 个必需外部资产；仅有源码时 ready_for_board=false。按 [资产要求](assets.md) 与 build/assets.json 准备模型、固件、原生库、角色及标定，并填写自己的清单。源文件路径可以相对清单目录，每项填写实际 origin 和 license。

```bash
python3 build/stage.py --output /tmp/twincoach-board --mode full33 --assets /path/to/assets.json
python3 -m pip install -r build/requirements.txt
python3 /tmp/twincoach-board/native/hybrid/stream_profile/check_source_closure.py --package /tmp/twincoach-board
```

资产齐备只表示装配完成，不证明 ABI、标定或实板性能。原生编译与 FPGA 工程创建见 [构建说明](../build/readme.md)。公开源码不包含模型导出全过程或受许可约束的 DPU IP，不能仅凭此仓库重建这些外部依赖。

## 打包与部署

```bash
python3 build/runtime.py --output /tmp/twincoach-runtime.tar.gz --mode full33 --assets /path/to/assets.json
scp /tmp/twincoach-runtime.tar.gz ubuntu@BOARD:~/twincoach-runtime.tar.gz
```

将 BOARD 换为实际地址。在板端为新部署准备空目录，再解包：

```bash
mkdir ~/twincoach
tar -xzf ~/twincoach-runtime.tar.gz -C ~/twincoach
```

在控制端复制 board/config.example.json 为 board/config.json，填写地址、目录、摄像头、旋转和显示会话。使用稳定设备名，确认物理正侧面与评分映射一致。

```bash
python3 board/control.py check --config board/config.json
python3 board/control.py start --config board/config.json
python3 board/control.py status --config board/config.json
python3 board/control.py stop --config board/config.json
```

check 检查源码入口、基础依赖、设备节点与架构，不代替完整推理测试。启停使用匹配的已部署硬件，不自动替换启动固件。首次运行应观察双路画面、角色跟随、完整计次和反馈更新；现场结果与保存的 JPEG 回放指标分开记录。

## 结果定位

[证据索引](evidence.md) 对照每项结论与文件。[优化依据](optimization.md) 说明架构取舍、DDR 对照和资源约束。[技能包](../skill/readme.md) 可复制到其他项目独立使用。
