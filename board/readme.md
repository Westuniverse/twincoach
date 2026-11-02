# 板端控制

start.ps1、test.ps1、stop.ps1 分别启动、自检和停止应用；start-full.ps1、start-hybrid.ps1 等为内部控制入口。PC 只发出 SSH 控制命令，识别、几何、蒙皮和绘制在板端执行。

先按 build/stage.py 装配源码并准备匹配资产及板端基础环境，再部署到指定 BoardRoot。基础系统需匹配 VART/XRT、图形环境和内核驱动；配套板端内核为 5.15.0-1027-xilinx-zynqmp，硬件为 di46ddr200_20261031。

显式传入 BoardIP、BoardRoot、Front 和 Side 可适配自己的网络与摄像头接线。逻辑源、物理视角、评分映射和显示顺序分别处理，不要仅按画面左右交换评分输入。

默认是双路 full33_dpu_tail4。运行前应检查 boot manifest 与内核，按匹配 ABI 准备 Tail4/shared 驱动。普通 start/stop 不更换位流。

角色 mesh、权重、私人标定与生成固件不随公开源码分发；参见 report/assets.md。
