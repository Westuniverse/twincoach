# 第三方依赖与许可

项目原创实现沿原 GPL-2.0-only；各文件中已有的版权与许可声明保持。Apache-2.0 等独立声明仅适用于相应上游文件或组件，不将其改写为 GPL。

| 组件 | 用途 | 来源/许可记录 |
| --- | --- | --- |
| AMD/Xilinx DPUCZDX8G、Vivado/Vitis | DPU 推理与 FPGA 工具链 | 原厂 IP/工具许可；本仓库不分发 IP 或工具二进制 |
| MediaPipe v0.10.18 算法参考 | ROI、热图与姿态坐标等算法依据 | Apache-2.0，固定来源登记在相关 sources.json 与 licenses/ 中 |
| libjpeg-turbo | 原生 JPEG 解码链接依赖 | 沿已安装库的 IJG/BSD/zlib 等原许可，相关声明在 licenses/ |
| NumPy、OpenCV、LiteRT/VART/XRT | 板端数值、图像、推理运行环境 | 由各自官方来源及其版本许可管理，非本仓库原创 |
| 角色 mesh、模型、标定和数据 | 部署所需资产 | 按各自来源授权，未包含在公开仓库 |

Python 包名 posepriv_coach 与硬件标识属于接口名称，用于连接应用、驱动和固件。
