# 阶段 1 基线：82900b4

冻结文件与摘要见 freeze.json；题目、答案及评估器分离。数据为合成留存材料。模型请求、生成答案和重开版本均未观察。语料 oracle 与候选池 oracle 的数字为可完整覆盖的核心项数；实际选择在段数和字节预算下计数。

| 题 | 语料 oracle | 候选池 oracle | 选择 | 首次损失 |
|---|---:|---:|---:|---|
| S1 | 2/2 | 2/2 | 2/2 | none_observed |
| S2 | 1/1 | 1/1 | 1/1 | none_observed |
| S3 | 3/3 | 3/3 | 3/3 | none_observed |
| S4 | 2/2 | 2/2 | 2/2 | none_observed |
| H1 | 1/1 | 1/1 | 1/1 | none_observed |
| H2 | 1/1 | 1/1 | 1/1 | none_observed |
| H3 | 2/2 | 2/2 | 2/2 | none_observed |
| H4 | 1/1 | 1/1 | 1/1 | none_observed |
| J1 | 1/1 | 1/1 | 0/1 | selection_or_capacity |
| J2 | 2/2 | 2/2 | 1/2 | selection_or_capacity |
| J3 | 1/1 | 1/1 | 0/1 | selection_or_capacity |
| J4 | 1/1 | 1/1 | 1/1 | none_observed |
| L1 | 1/1 | 1/1 | 0/1 | selection_or_capacity |
| L2 | 1/1 | 1/1 | 1/1 | none_observed |
| L3 | 1/1 | 1/1 | 0/1 | selection_or_capacity |
| L4 | 1/1 | 1/1 | 0/1 | selection_or_capacity |
| L5 | 2/2 | 2/2 | 1/2 | selection_or_capacity |
| L6 | 1/1 | 1/1 | 0/1 | selection_or_capacity |
| L7 | 1/1 | 1/1 | 1/1 | none_observed |
| L8 | 1/1 | 1/1 | 1/1 | none_observed |

完整题数：S 4/4，H 4/4，J 1/4，L 3/8。对于这批短文，oracle 要求全文在候选区间中，故不会把部分合同当完整条款。留存材料没有被实现端读取；结果仅用于离线验收。

重建临时基线时，除协议中的 API 目录外，还需执行 `git archive 82900b4 src/packages/contracts | tar -x -C /tmp/foresight-baseline-82900b4`。不得把离线选择结果表述为模型见到或最终答案正确。

历史 H 的 2/5 是旧第一版选择器的完整核心组数；本次基线是后续提交 82900b4 的选择器，在同一 H 输入得 5/5 核心组、4/4 整题。两者分母及代码版本不同；历史结果没有运行第二版，不能将本次 4/4 说成对旧 2/5 的同版本复现，也不能当作模型回答质量。

第三层回放使用固定离线协调策略：真实 search_library 结果按 512 字节选择、通过 `_read_sources` 读取后，构造两次工具交互并调用 `build_model_turn_input` 及 Anthropic Messages 序列化；`baseline-input.json` 保存请求中实际 read tool result 的源版本哈希和原文区间、序列化哈希与字节。所有 20 题请求读取的区间均在构造的下一轮序列化结果中观察到。协调行为为脚本模拟，未向服务商发送，也无真实服务响应；它不证明模型实际收到、理解或交付这些材料。复放：使用基线 PYTHONPATH 和 API venv 运行 `input_replay.py baseline-trace.json baseline-input.json`。
