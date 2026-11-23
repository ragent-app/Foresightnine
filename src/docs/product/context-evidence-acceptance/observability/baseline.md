# 阶段 0：native 调用离线观测基线

2026-11-23；代码基线 HEAD `3e1ca4d458d9a6ccc479c2d7123d5a81ba998d70`，增加观测，不改变选择策略。输入是 `../evidence/frozen-cases.json` 的 F1 和 M1，SHA-256 `78588ccf7251da1cdaf1201434766d3cabc7abea6bc7233c10d763398b2f770c`。两题是合成资料，Messages 离线虚拟传输固定检索、读取、提交或截断三轮，每臂三次虚拟模型调用、零次外部搜索、输出上限 256。未调用真实模型，模型和搜索费用为零；回包中的零 token 是模拟回执，真实 token、缓存、延迟和供应商费用未知。执行 `PYTHONPATH=src/apps/api src/apps/api/.venv/bin/python src/docs/product/context-evidence-acceptance/observability/baseline.py` 可重新运行并检查，call ID 每次随机变化；`baseline.json` 保存阶段 1 接入前记录，`after-selection.json` 保存当前新增搜索字段和 partial 交付关联后的记录，工具 schema 改变后字节可能不同。

| 题 | 检索候选与成功读取 | 三次请求实际字节 | 提交臂结果 |
| --- | --- | --- | --- |
| F1 财报 | statement-v1、note-increment-v1，均有版本哈希与位置 | 51,716 / 54,337 / 55,492 | partial；已提交并定位主表 claim，但两个分项仍 unresolved |
| M1 行情 | prices-v1，有版本哈希与位置 | 54,306 / 56,635 / 57,312 | partial；一个分项引用已提交 claim 并显示 answered，另一项 unresolved |

同一题的提交和截断臂前两轮及第三次请求相同；两臂均记录 search-1 候选和 read-1 成功范围，第三轮的输入包含同一 read-1 成功范围。提交臂的诊断按提交模型 call、提交工具 call、服务端 claim ID、分项、需求与引用连接；截断臂无提交工具调用，因此提交确实未发生；运行时返回 partial 结果，当前版本把该交付对象关联至最后观测调用，明确标记为顺序关联，不能推定该调用产生全部内容。诊断中的结构化引用仅证明出现和服务端关联，不证明模型理解或语义支持。特别是 M1 的 answered 是现有服务端结构判断；脚本只提交原文，未进行对齐收益计算，不能视作行情题已正确回答。

每次 tools 值 43,302 字节、system 值 6,976 字节；messages、其他值与 JSON 开销见原始记录。分类总和等于实际请求体，预算估算与供应商回执分别保存，不能把字节或估算当作实测 token。基线检索工具未提供 requirement ID，候选与需求的关系未知；后续版本在 `after-selection.json` 中保存检索器显式提供的 `selection_trace.requirement_ids`、选入原因和查询哈希，不能将词面命中当作证据满足。其他未发生的事实或计算也不能推断成失败的模型分析。

注意：初次运行漏设资料发布时间，被时点策略拒绝；当时回执仍提到 source ID，但并非成功读取。正式基线已按冻结材料设置发布时间，断言 `status=read` 的范围与下轮入模范围。此前的初次数值不应使用。

Messages API 的离线双轮集成验证实际请求哈希、分类总数、私有日志、成功读取及入模、提交 claim 到分项的连接和公开操作不暴露诊断；运行时及 OpenAI native 测试一起共 82 项通过。另运行 Messages provider 全套时一项原有失败：`test_mid_stream_timeout_after_zero_message_start_reports_none_and_stream_metrics` 完整字典断言未包含 HEAD 既有的 `reported_tokens` 与 `reserved_estimate_tokens`，不将全套称为通过。

正式阶段 0 出口仍缺通用需求→候选映射、任意正文入模的完整定位、恢复及撤权验证；截断后的部分成果仅记录了返回对象和最后调用的顺序关联，不证明调用产生其中所有内容。真正采用的语义、研究质量和投资效果均未评估；真实对照需预先冻结模型、参数、权限和预算。
