# E1 截断回执追踪（只读）

范围为归档的 E1 16 次隔离内存实验，12/16 为 `provider_output_truncated`，其余 4/16 达到模型调用预算；均 partial。运行回执中的 facts 和 calculations 是内存客户端返回的已保存结果，不能称为生产数据库落库记录。下文“事实/计算”仅计这些结果，来源原文的读取不等于事实记录。

已知：12 次截断事件均为 `external_state=returned`、`settlement=settled`，保存了该轮合计 usage；事件本身未保存原始 stop reason、输入/输出 token 细分、响应正文或部分 tool call。此前成功轮的 finish_reason 为 `tool_use`，其明细见结构化结果。`paired_e1.py` 只把完成并进入 checkpoint history 的工具输入输出写入 `tool_io`，不能以没有部分内容证明提供商未产生它；12 条截断响应中是否含部分 tool call 均不可知。记录的提示仅含每轮哈希及 tail note，不是完整消息正文。

推断：冻结包 `apps/api/app/anthropic_messages.py` 的 `_build_response` 将 `max_tokens` 映射为 `provider_output_truncated`，且在解析工具块前抛错；结合预登记单次输出上限 2048，可推断这些截断走了该分支，但各次原始 stop reason 和输出 token 计数未独立保留，不能逐条直接验证。无法据此判定截断输出具体在生成哪个工具或内容。

表内轮次为该次模型调用序号；“末次成功工具”按事件顺序最后一个 `status=completed`，括号是工具调用 ID；合计 usage 是模型事件 usage 之和，不是输出 token。每个完整输入、输出回执、模型事件、提示哈希、事实和计算以及原文件 SHA-256 均在 `trace-results.json`。

| 运行 | 结束 | 轮次 | 末次成功工具回执（调用 ID） | 最后轮 usage / 总 usage | 已存事实 / 计算 |
|---|---|---:|---|---:|---:|
| D1-A-1 | 调用预算耗尽 | 4 | `calculate_financials` (`call_6a33122971504d2e94b6ad63`) | 20854 / 75766 | 4 / 3 |
| D1-A-2 | 截断 | 2 | `read_sources` (`call_b3d1ad7809f44167949ccaf9`) | 18330 / 34229 | 0 / 0 |
| D1-B-1 | 调用预算耗尽 | 4 | `search_source` (`call_daa63229cac64ae394545fcc`) | 22041 / 75362 | 4 / 3 |
| D1-B-2 | 调用预算耗尽 | 4 | `calculate_financials` (`call_7997aa3838b94837b71b5968`) | 20303 / 75083 | 4 / 3 |
| D2-A-1 | 调用预算耗尽 | 4 | `calculate_financials` (`call_71c6eedbdce54d7e9bc623ed`) | 20450 / 74111 | 3 / 1 |
| D2-A-2 | 截断 | 2 | `read_sources` (`call_3edafea257f34f37a09f2903`) | 18590 / 34742 | 0 / 0 |
| D2-B-1 | 截断 | 2 | `read_sources` (`call_4ed1109f19d146cba1e66451`) | 18499 / 34531 | 0 / 0 |
| D2-B-2 | 截断 | 4 | `calculate_financials` (`call_47de5b6b7f884b0b9e5d7aef`) | 21159 / 74126 | 3 / 1 |
| D3-A-1 | 截断 | 2 | `read_sources` (`call_73e7c3d9955f491b8d414f97`) | 18115 / 33807 | 0 / 0 |
| D3-A-2 | 截断 | 2 | `read_sources` (`call_d8093ec9a82c44909cdc24bc`) | 17712 / 33015 | 0 / 0 |
| D3-B-1 | 截断 | 2 | `read_sources` (`call_63cedd6fb01f4d07a383e838`) | 18363 / 34285 | 0 / 0 |
| D3-B-2 | 截断 | 2 | `read_sources` (`call_d594c025ed7342f6a37154c9`) | 18135 / 33817 | 0 / 0 |
| D4-A-1 | 截断 | 2 | `read_sources` (`call_e55d711c4ac5452480d90a8c`) | 17673 / 32936 | 0 / 0 |
| D4-A-2 | 截断 | 2 | `read_sources` (`call_5af9feb0387a44638f722a18`) | 18199 / 33979 | 0 / 0 |
| D4-B-1 | 截断 | 2 | `read_sources` (`call_e612f0f29c194753a6a05921`) | 17915 / 33396 | 0 / 0 |
| D4-B-2 | 截断 | 2 | `read_sources` (`call_3d5d28ab4dc14fbb81034cd0`) | 17656 / 32880 | 0 / 0 |

11/12 截断在第二轮：各自第一轮的 `read_sources` 成功读取完整合成 exhibit（D1 103、D2 136、D3 73、D4 88 字符，回执 `truncated=false`），随后没有已保存事实或计算。D3/D4 是相对日的合成文本，不能当结构化行情或真实交易日回执。其余 1/12 为 D2-B-2：前三轮完成读取、记录三项事实、反方搜索及收入增长计算，末次成功工具回执为 `calculate_financials`，结果 20.00%；三项事实为 2025/2024 收入 120/100 USD million 及 2025 稀释 EPS 3 USD/share，均附原文位置，语义未独立核验。第四轮截断后仍保留这些结果。

四条参照均四次调用后预算耗尽：D1-A-1、D1-B-1、D1-B-2 各保存 4 项事实、3 项计算；D2-A-1 保存 3 项事实、1 项计算。D2-A-1 的最后一次工具尝试为无效的 `checkpoint_question_part`，表中列的是它之前最后一个成功回执，而非最后一次尝试。D1-B-2 也曾有无效计算调用；完整工具事件见结构化文件。预算耗尽不证明无截断倾向，也不能以这四例推断分组策略优劣。

复核：在仓库根目录运行 `python3 src/docs/product/research-quality-acceptance/fact-batching/trace_extract.py`，只读取 `e1-results/*.json` 并重写同目录 `trace-results.json`，不导入应用、不联网、不发模型或工具调用。脚本逐文件记录输入 SHA-256，可对照归档是否变化。该重放是归档提取，不能重建未记录的响应或重跑原实验。
