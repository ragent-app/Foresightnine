# 第一版选择器集中验收

冻结源码见 candidate-v1/ 与 candidate-code.sha256。相同候选池复用基线 trace 顺序、区间与需求，候选直接调用纯选段函数；该对照无模型和网络。数字为完整核心项/总项，不等同最终答案。

| 题 | 基线 | 同池候选 | 自然候选池 | 首次损失（自然候选） |
|---|---:|---:|---:|---|
| S1 | 2/2 | 1/2 | 1/2 | selection_or_capacity |
| S2 | 1/1 | 1/1 | 1/1 | none_observed |
| S3 | 3/3 | 2/3 | 2/3 | selection_or_capacity |
| S4 | 2/2 | 2/2 | 2/2 | none_observed |
| H1 | 1/1 | 0/1 | 0/1 | selection_or_capacity |
| H2 | 1/1 | 1/1 | 1/1 | none_observed |
| H3 | 2/2 | 2/2 | 2/2 | none_observed |
| H4 | 1/1 | 1/1 | 1/1 | none_observed |
| J1 | 0/1 | 0/1 | 0/1 | selection_or_capacity |
| J2 | 1/2 | 1/2 | 1/2 | selection_or_capacity |
| J3 | 0/1 | 1/1 | 1/1 | none_observed |
| J4 | 1/1 | 0/1 | 0/1 | selection_or_capacity |
| L1 | 0/1 | 0/1 | 0/1 | selection_or_capacity |
| L2 | 1/1 | 1/1 | 1/1 | none_observed |
| L3 | 0/1 | 0/1 | 0/1 | selection_or_capacity |
| L4 | 0/1 | 0/1 | 0/1 | selection_or_capacity |
| L5 | 1/2 | 1/2 | 1/2 | selection_or_capacity |
| L6 | 0/1 | 1/1 | 1/1 | none_observed |
| L7 | 1/1 | 1/1 | 1/1 | none_observed |
| L8 | 1/1 | 1/1 | 1/1 | none_observed |

同池留存 L 整题完整 3/8→4/8；L6 改善。S3 的客户反证被舍弃，J1、J2 仍不完整，J3 改善，J4 回退，H 整题 4/4→3/4。重要反证不退化的门槛未满足，**第一版不采用**。观察结果没有改变默认选择器。

同一组合选择器、查询次数不变，需求查询相对于重复原题查询在该样本上的得分无差异；这不是已读线索驱动补查，当前实现尚无该能力。两臂结果见 candidate-results.json 与 repeat-query-results.json。

S4 的未来和撤权源均未选入；这只是固定夹具中的边界观察，不能代替全部动态撤权路径。候选生成的下一轮模拟序列化见 candidate-input.json，未外呼，最终可重开答案未观察。

1000 个高度同质的合成候选，截到 12 个、裁剪 988；未开启分析器中位 0.081 秒，tracemalloc 下 0.203 秒、Python 峰值 356,313 字节，cProfile 下 0.260 秒；仅选段，不含索引和检索，内存非进程 RSS。压力池真实必要候选未标注，不能据此推断必要候选裁剪次数或语义精度。
