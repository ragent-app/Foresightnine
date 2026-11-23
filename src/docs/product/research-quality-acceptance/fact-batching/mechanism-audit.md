# 分批事实记录机制：离线结构审计

预定 16 轨；结果 16；started 无结果 0；无 started 标记 0。详见 JSON 的逐轨回执、区分未观测与未开始及输入哈希。

| 轨 | 状态/原因 | 调用 | 已保存事实 | record_facts 每次条数（history 轮次） | 同轮多调用 | B 指令结构判读 | reported / budget tokens |
|---|---|---:|---:|---|---|---|---:|
| D1-A-1 | partial / provider_budget_exhausted | 4 | 4 | 4（2） | [] | not_applicable_A | 72269 / 72269 |
| D1-A-2 | partial / provider_output_truncated | 2 | 0 | 未见或不可用 | [] | not_applicable_A | 34483 / 34483 |
| D1-B-1 | partial / provider_output_truncated | 2 | 0 | 未见或不可用 | [] | not_tested_no_record_facts_in_history | 34212 / 34212 |
| D1-B-2 | partial / provider_budget_exhausted | 4 | 4 | 2（2）, 2（3） | [] | structurally_consistent_inspection_not_provable | 71074 / 71074 |
| D2-A-1 | partial / provider_budget_exhausted | 4 | 3 | 3（2） | [] | not_applicable_A | 73715 / 73715 |
| D2-A-2 | partial / provider_output_truncated | 4 | 3 | 3（2） | [] | not_applicable_A | 75416 / 75416 |
| D2-B-1 | partial / provider_output_truncated | 4 | 3 | 2（2）, 1（3） | [] | structurally_consistent_inspection_not_provable | 75245 / 75245 |
| D2-B-2 | partial / provider_output_truncated | 2 | 0 | 未见或不可用 | [] | not_tested_no_record_facts_in_history | 37057 / 37057 |
| D3-A-1 | partial / provider_budget_exhausted | 4 | 4 | 4（2） | [] | not_applicable_A | 73441 / 73441 |
| D3-A-2 | partial / provider_output_truncated | 1 | 0 | 未见或不可用 | [] | not_applicable_A | 17028 / 17028 |
| D3-B-1 | partial / provider_budget_exhausted | 4 | 4 | 2（2）, 2（3） | [] | structurally_consistent_inspection_not_provable | 69893 / 69893 |
| D3-B-2 | partial / provider_budget_exhausted | 4 | 4 | 2（2）, 2（3） | [] | structurally_consistent_inspection_not_provable | 71806 / 71806 |
| D4-A-1 | partial / provider_output_truncated | 2 | 0 | 未见或不可用 | [] | not_applicable_A | 33881 / 33881 |
| D4-A-2 | partial / provider_output_truncated | 2 | 0 | 未见或不可用 | [] | not_applicable_A | 33457 / 33457 |
| D4-B-1 | partial / provider_budget_exhausted | 4 | 2 | 2（2） | [] | structurally_consistent_inspection_not_provable | 69992 / 69992 |
| D4-B-2 | partial / provider_budget_exhausted | 4 | 2 | 2（2） | [] | structurally_consistent_inspection_not_provable | 68522 / 68522 |

截断逐轮的已见块类型、长度、工具名、块结束及参数 JSON 完整性见同目录 JSON；未见工具只表示已观测块中未见，不断言未到达的部分不存在。观察轮序只有计数一致时才作暂定对应，须核对回执身份。
B 指令包含每次最多两项及结果返回后再记录：同轮多个 record_facts 或单次大于两项为可见违反；结果回执在后续轮可见仅代表结构相容，不能证明模型理解或检查结果。没有调用不计遵循。
重复计数仅比对完整输入对象和已保存 fact_id，不能排除不同表述的语义重复。每臂 reported 合计和缺失数见 JSON；unknown usage 或费用未知不得视为零。
本审计仅保存结构与长度，不保存思维正文或工具参数值；不执行应用、外呼或模型调用。
