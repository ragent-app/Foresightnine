# 最终候选：冻结对照与决定

最终源码见 candidate-final/ 和 candidate-final.sha256。固定池选择器获得基线实际命中的下标及顺序，池、许可、需求和容量相同；自然列为候选完整检索。每格为完整核心项/可得核心项。

| 题 | 82900b4 | 同池候选 | 自然候选 | 首次损失（自然） |
|---|---:|---:|---:|---|
| S1 | 2/2 | 2/2 | 2/2 | none_observed |
| S2 | 1/1 | 1/1 | 1/1 | none_observed |
| S3 | 3/3 | 3/3 | 3/3 | none_observed |
| S4 | 2/2 | 2/2 | 2/2 | none_observed |
| H1 | 1/1 | 1/1 | 1/1 | none_observed |
| H2 | 1/1 | 1/1 | 1/1 | none_observed |
| H3 | 2/2 | 2/2 | 2/2 | none_observed |
| H4 | 1/1 | 1/1 | 1/1 | none_observed |
| J1 | 0/1 | 1/1 | 1/1 | none_observed |
| J2 | 1/2 | 2/2 | 2/2 | none_observed |
| J3 | 0/1 | 1/1 | 1/1 | none_observed |
| J4 | 1/1 | 1/1 | 1/1 | none_observed |
| L1 | 0/1 | 0/1 | 0/1 | selection_or_capacity |
| L2 | 1/1 | 1/1 | 1/1 | none_observed |
| L3 | 0/1 | 0/1 | 0/1 | selection_or_capacity |
| L4 | 0/1 | 0/1 | 0/1 | selection_or_capacity |
| L5 | 1/2 | 1/2 | 1/2 | selection_or_capacity |
| L6 | 0/1 | 0/1 | 0/1 | selection_or_capacity |
| L7 | 1/1 | 1/1 | 1/1 | none_observed |
| L8 | 1/1 | 1/1 | 1/1 | none_observed |
| N1 | 0/1 | 0/1 | 0/1 | selection_or_capacity |
| N2 | 1/2 | 1/2 | 1/2 | selection_or_capacity |
| N3 | 0/1 | 0/1 | 1/1 | none_observed |
| N4 | 1/1 | 1/1 | 1/1 | none_observed |
| N5 | 1/1 | 1/1 | 1/1 | none_observed |
| N6 | 1/1 | 1/1 | 1/1 | none_observed |
| N7 | 1/1 | 1/1 | 1/1 | none_observed |
| N8 | 1/1 | 1/1 | 1/1 | none_observed |
| P1 | 0/1 | 0/1 | 0/1 | selection_or_capacity |
| P2 | 1/2 | 1/2 | 1/2 | selection_or_capacity |
| P3 | 0/1 | 0/1 | 0/1 | selection_or_capacity |
| P4 | 1/1 | 1/1 | 1/1 | none_observed |
| P5 | 1/1 | 1/1 | 1/1 | none_observed |
| P6 | 0/1 | 0/1 | 0/1 | selection_or_capacity |
| P7 | 1/1 | 1/1 | 1/1 | none_observed |
| P8 | 1/1 | 1/1 | 1/1 | none_observed |

**决定：不采用。** 最后一批独立盲题同池整题 4/8→4/8，未达到预登记的严格改善；没有继续调参。前两批留存分别 3/8→3/8、5/8→5/8。S3 的主表及反证保留；公开 J1–J3 找齐，J4 保留。N3 在自然检索改善但同池不变；不能把不同候选池的改善归因于选择器。

自然路径的 36 题所选来源、时间、区间、段数及逐 hit 原文加表头 UTF-8 字节无越界。fixture 中无真实表头；单独合成边界验证 UTF-8 表头累计 512 字节允许、515 字节拒绝。选择器把同源相同表头计一次，但多条 hit 可能各自返回表头；该种实际请求的重复开销未被此夹具覆盖，存在越过实际输入容量的风险。

固定策略经真实读工具与请求构造模拟，73/73 所请求区间进入序列化读工具结果；并无真实模型外呼、服务回执、回答或重开交付。固定池与自然结果及各自代码、输入、版本哈希均可检查。

一千条高度同质合成候选的最终选择器，三次未分析中位 0.113 秒，tracemalloc 下 0.247 秒、Python 峰值 388,012 字节，cProfile 下 0.133 秒；不含检索，非 RSS，不能推断真实任务延迟或必要材料裁剪。


盲评冻结后修正了一项容量记账：多个返回片段各自携带的表头按实际重复次数计入。最终盲题无表头，因此已冻结的选段决定不受此修正影响。另以 64 份干扰资料、中英文各一题做等查询次数对照。重复原题两次均未召回目标；改用同版本已读原文的附注号，两题均找回目标并提供可展开的区间。两题的目标仍未进入默认两段。修改版本或从来源范围中排除指针时不得使用该原文补查。[补查结果](read-cues.json)
