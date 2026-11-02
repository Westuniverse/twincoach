# 0231：共享交换与HP0出口200MHz

日期：2026-10-31。状态：实际布线、SD启动、神经黄金及同源45秒完成；
计算阶段耗时下降，整链FPS未提升，保留研究候选。

## 改动与接口

原SmartConnect内部五个交换网络及HP0均100MHz，Tail4入口由200转100。
独立clone只把PS HP0输入接现有200MHz，并明确SW0 `ASSOCIATED_CLK=aclk1`。
DPU AXI/core100、DPU2x200、Tail4200、ROI/DMA/shared100、MMIO/IRQ和算术均保持。
Tail4入口同步转换去除，六个100MHz入口改为到200MHz的同步转换；没有新增IP。

可复现冷配置为`scripts/vivado/configure_tail4_ddr_exit200.tcl`，仅在独立di45/ROI1
clone已打开的BD中source。该入口validate/读回/save，不自动综合、生成位流或部署。
仅改HP0 pin而不改SW0不会提高内部交换能力。

## 实际PPA与启动

SC OOC LUT11474→11278、FF15821→15987，BRAM/DSP不变；实际joint却因布局分散，
CLB14560→14629/14640。最终78404LUT、115916FF、139BRAM等效、655DSP、64URAM。
首次route −0.112ns保留，单次postroute Explore后setup +0.017ns、hold +0.008ns，
TNS/THS0，195423/195423全布线、route errors0、DRC0errors/642同类warnings。
没有放宽XDC、修改RTL或重做模型。

末尾controller在`open_checkpoint`后的in-memory上下文再查project runs时报错，发生在
DCP和全部正式报告保存之后。单独冷查询与导出成功，未因此重跑实现。
最终system.bit与XSA唯一声明的`posepriv_ddr_exit200.bit`逐字节相同；原native sysdef
MODE仍为工具输出的Post-Synthesis，未改写。实际物理实现状态由选中postroute DCP和
对应bit导出链证明。

`di46ddr200_20261031`已通过正常SD重启；沿用di45 DT/FIT、生产驱动和模型，重建匹配
metadata。boot audit PASS，8个工程请求1282960 raw字节全等；不是人体精度验收。

## 完整45秒结果

同一2698输入、45.019937339秒、固定hot15..44s、原owner GPU确认时钟，保持
NEON/cap3/static/axis/4MSAA及CPU裁剪：

| 物理版本 | 更新/秒 | 最低连续1秒 | P95源龄ms | 最大间隔ms |
| --- | ---: | ---: | ---: | ---: |
| di45，交换/出口100 | 23.551724 | 20 | 292.446460 | 111.343805 |
| di46，交换/出口200 | 23.310345 | 20 | 291.764929 | 126.086863 |

新轮首个真实GPU源对确认在输入后13.259秒，startup单列。两轮exit0、forced空、
image leases0。新轮全程worker平均prefix15.739ms、tail5.638ms，旧轮约17.913/7.161ms；
这是处理源子集不同的整轮计数，不当作逐帧同输入加速比。整链仍受ARM与显示流水约束，
没有以局部耗时或理论带宽宣布达成24/150/80。

保留旧di45/di44包与选择器。仅退役pre-t411根备份两文件先流回主机、确认完整后删除，
新tag后firmware仍约149MiB可用。部署包明确含独立stop helper，不再假定Full33包包含该工具。

[原始测量摘要](../../benchmarks/ddr_exit200_20261031.json)。
私有DCP/XSA、冷错误和实际日志分别在`tail4_ddr_exit200_candidate_20261031/`、
`tail4_ddr200_board_20261031/`（均位于现有ignored研究目录）。来源为项目GPL-2.0-only、
既有AMD SmartConnect/IP与BSP，无新依赖、模型或IP。
