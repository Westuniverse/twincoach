# 外部资产与运行边界

仓库公开项目控制、DPU/PL 胶水、自研运算 RTL、几何/动作逻辑、显示实现、Linux 驱动、通用配置和构建工具。

下列资产在运行时需要另行准备，角色和目标路径以 build/assets.json 为准：

| 资产 | 用途与来源要求 |
| --- | --- |
| DPU IP repository | 来自 AMD/Xilinx 的 DPUCZDX8G IP，遵循原许可；通过 --dpu-repo 指定 |
| prefix.xmodel 与检测模型 | 模型及其编译/转换记录，保留原始来源、版本和许可 |
| Tail4 W/P、program header | 与原模型匹配的生成资产；tail4_program.h 不作为通用源码公开 |
| bit/FIT、xclbin、boot manifest、驱动模块 | 与硬件、内核和 ABI 匹配的生成物，可在对应环境构建或使用已授权产物 |
| ARM 原生库与隔离 Python 依赖 | 按仓库中的 C/C++/shell 构建入口及依赖许可准备 |
| 人物 mesh 与标定 | 使用自己有权使用的角色与实际相机参数，示例不能替代真实标定 |

模型与运行依赖按各自来源及许可准备。公开源码装配默认得到 ready_for_board=false，直到必需资产齐备；它不下载模型、替换权重或自动配置板卡固件。

基础功能测试不依赖这些资产，方便直接验证动作建议、数据结构和通用统计工具。
