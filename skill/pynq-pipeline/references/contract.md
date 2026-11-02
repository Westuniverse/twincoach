# 接口契约

## 字段

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| schema | 字符串 | stream-contract-v1 |
| name | 字符串 | 样例名称，用于报告 |
| operation | 字符串 | identity 或 affine 的软件参考 |
| dma | 字符串 | PYNQ IP 属性路径，层级用 / 分隔 |
| dtype | 字符串 | uint8、int16、uint16、int32 或 uint32 |
| count | 正整数 | 输入和输出元素数，相同 |
| word_bytes | 正的二次幂 | 声明的数据通路字节粒度 |
| max_bytes | 正整数 | 对应设计允许的单次 DMA 字节上限 |
| parameters | 对象 | 软件参考与参数寄存器共用的值 |
| control | 可选对象 | IP、身份、参数和启动寄存器 |

总传输字节数为 count×dtype 字节数，须不超过 max_bytes 且整除 word_bytes。容量来自实际 DMA 配置，不能从 Python 缓冲长度反推硬件容量。工具不直接编程物理地址。

## 控制寄存器

```json
{
  "ip": "compute/affine",
  "identity": {"offset": 60, "value": 1095124558},
  "writes": [{"offset": 16, "parameter": "scale"}, {"offset": 24, "parameter": "bias"}],
  "start": {"offset": 0, "value": 1}
}
```

- offset 是字节偏移，按 4 字节对齐。
- identity 可选；检查发生在参数写入和缓冲分配之前，支持 mask。
- writes 可使用 parameter 引用 parameters，或用 value 指定常量。
- 参数写入按 32 位表示编码，负数使用补码。
- start 可选；存在时在 RX 提交后、TX 提交前写入。

示例偏移与身份值只是示例核的接口定义，不是任意 HLS/RTL 核的默认寄存器。应从自己的 HWH、驱动或接口说明填写。

## 黄金向量

```json
{"input": [-2, -1, 0, 1, 100, -100], "expected": [-8, -5, -2, 1, 298, -302]}
```

input 和 expected 都是显式整型数组，长度与 count 相同，数值落在 dtype 范围内。expected 应来自独立推导、软件黄金模型或已有合格数据，不能用被测后端的输出生成。

identity 为等值复制；affine 为 scale×x+bias，附带样例只覆盖不溢出的整型值。需要饱和、舍入、定点缩放或溢出截断时，应扩展软件参考和独立向量，不把它们隐含在 dtype 中。

## 向量为何这样选择

copy 向量覆盖零、字节边界、16 位边界、最高符号位和最大 uint32 值，可暴露符号或宽度解释问题。affine 向量包含负数、零和正数，并通过带层级的另一组 IP 名称验证配置解耦。

两个样例不提供 bitstream，也不声称实现了相应硬件。它们定义了可供软件模型、API 替身和使用者自己的 Overlay 共同遵守的事务。

## 结果

硬件后端还会核对 DMA 驱动报告的实际接收字节数。提前 TLAST 形成的短帧会失败，避免未写入的缓冲尾部恰好与黄金值相同而误报通过。

结果包含 backend、hardware_executed、passed、actual/expected、差异索引、长度匹配、事件顺序和计量边界。软件模型的 hardware_executed 固定为 false。

wall_ns 是主机事务边界，包含后端分配、配置、完成等待和释放，不是 PL 算子周期。持续性能与资源数据需要另外采集。
