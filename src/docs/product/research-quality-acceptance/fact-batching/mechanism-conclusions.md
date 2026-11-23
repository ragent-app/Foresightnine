# 分批事实记录：机制层集中验收

16/16 轨均有结果，51/51 模型调用事件均为 returned/settled。A 有 5/8 截断、3/8 耗尽四次调用预算；B 有 3/8 截断、5/8 耗尽预算。全部结果为 partial。样本小、模型版本与缓存不可锁定，截断数量的差异不能证明分批指令带来稳定改善，也不能代表研究质量或正式交付效果。

## 截断响应结构

以下为失败轮进入冻结 Messages 适配器 `_build_response` 前观察到的解码块，不包含正文。八轮的观察 stop reason 均为 `max_tokens`；轮次按解码序列和调用计数对应，逐条模型事件及源码文件哈希见 [结构索引](mechanism-results.json)。`thinking` 和 `text` 的数值是字符数，不是 token 数。工具名出现表示响应中观察到工具块，不等于工具已执行。各轮均无流式块事件记录，因此无法确定最后一个块的流式停止情况、JSON 生成的截断位置，或仅凭空参数形状判断参数是否完整。

| 运行 | 截断轮 | 解码块，按顺序 | 工具观察及已存事实 |
|---|---:|---|---|
| D1-A-2 | 2 | thinking 6704 | 未见工具块；事实 0 |
| D1-B-1 | 2 | thinking 7618 | 未见工具块；事实 0 |
| D2-A-2 | 4 | thinking 7194，text 117，create_table | 已见 create_table，未见 record_facts；此前已存 3 项 |
| D2-B-1 | 4 | thinking 4850，text 49，checkpoint_report_draft，create_table | 已见两个工具，未见 record_facts；此前已存 3 项 |
| D2-B-2 | 2 | thinking 7196 | 未见工具块；事实 0 |
| D3-A-2 | 1 | thinking 9508 | 未见工具块；事实 0 |
| D4-A-1 | 2 | thinking 7831，text 159，search_source，record_facts | 已见 record_facts，其参数形状为空且条数未知；事实 0 |
| D4-A-2 | 2 | thinking 6727 | 未见工具块；事实 0 |

5/8 截断轮只观察到 thinking 块，另 3/8 观察到工具块。D4-A-1 的 record_facts 确已出现在解码响应，但未进入已执行 checkpoint history；冻结适配器对 max_tokens 在处理工具前抛出异常。未见工具仅指已观察到的解码内容，不能推断服务端未生成或未到达的内容。工具块为空的参数形状不是对原始参数完整性的证明。`thinking` 长度也不能证明截断发生于该块内部，更不能以字符数替代输出 token。

## 分批、重复及资源

B 臂 6/8 轨有进入 checkpoint history 的 record_facts：D1-B-2 和 D3-B-1、D3-B-2 为 2+2，D2-B-1 为 2+1，D4-B-1 和 D4-B-2 各为 2；均每次不超过两项，且没有同一 history 轮发出两个 record_facts。四条多批轨的前次工具结果已保存，下一批在后续 history 轮；这与指令结构相容，但不能证明模型实际理解或检查了结果。另 2/8 在保存任何 record_facts 前截断，无法评价该指令。A 臂有四轨执行事实记录，单次分别 4、3、3、4 项。所有已见 record_facts 调用中，没有完全相同的输入事实对象重复，已保存事实中也没有重复 fact_id；这两个精确检查不能排除语义重复。

| 臂 | 轨数 | 模型调用 | reported tokens | budget-accounted tokens | 未知用量调用 |
|---|---:|---:|---:|---:|---:|
| A | 8 | 23 | 413,690 | 413,690 | 0 |
| B | 8 | 28 | 497,801 | 497,801 | 0 |

两臂 reported 分别为对应 8 轨已报告用量之和，B 比 A 多 84,111 tokens；调用次数及终止原因不同，缓存状态也可能混杂，不能将差额归因于指令。实际费用未知。逐轨用量、工具调用条数、同轮情况、重复检查和来源文件 SHA-256 在 [结构索引](mechanism-results.json) 与 [逐轨表](mechanism-audit.md)；可用 `python3 src/docs/product/research-quality-acceptance/fact-batching/mechanism_analyze.py` 只读重建索引及逐轨表，不外呼，也不输出推理正文。
