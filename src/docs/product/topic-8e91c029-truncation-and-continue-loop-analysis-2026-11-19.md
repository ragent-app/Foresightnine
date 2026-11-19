# `topic_8e91c0299a7b4b25823bc12ab9c7e53a` 多轮运行实测复盘与截断/续跑死循环缺陷分析

日期：2026 年 11 月 19 日。状态：**实证诊断与改进设计（文档整理，尚未实施对应修复代码）**。

本文基于线上 PostgreSQL（`research_topics`、`research_run_inputs`、`research_sources`、`usage_ledger`、`billing_states`）、容器环境变量与源码核查，完整记录研究主题 `topic_8e91c0299a7b4b25823bc12ab9c7e53a`（问题：“比较英伟达最近两个财年的总收入和营业利润率”，账号 `account_8887e09f4185483a8323042c6e4ba643`）在 2026-11-19 公网改造上线前后的全链路执行表现、报错细节与 5 类系统性缺陷，并给出后续修复方案。

---

## 1. 运行环境基线（实测采集时配置）

- **推理模型配置**：`FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL='anthropic_messages'`、`FORESIGHTNINE_RESEARCH_LLM_BASE_URL='https://api.z.ai/api/anthropic'`、`FORESIGHTNINE_RESEARCH_LLM_MODEL='glm-5.3-flash'`、`FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS='4096'`、`FORESIGHTNINE_RESEARCH_LLM_TIMEOUT_SECONDS='120'`；`FORESIGHTNINE_LLM_CAPABILITY_PROFILE` 未设置（`provider_limits.max_input_tokens=None`）。
- **搜索与 SEC 配置**：`FORESIGHTNINE_SEARCH_PROVIDER='glm_coding_mcp'`、`FORESIGHTNINE_SEC_USER_AGENT='ForesightNine/1.0 privacy@foresightnine.com'`。
- **账户额度**：`quota_limit=50,000,000`，截至 `2026-11-19T13:51:32Z` 已累计消耗 `consumed_tokens=501,140`（本主题 3 轮研究合计 `498,115` tokens）。

---

## 2. 最近几轮执行情况与报错全记录

| 轮次 / 版本 | Operation ID 与时间（UTC） | 触发入口 | 状态与消耗 | 关键行为与报错实证 |
| :--- | :--- | :--- | :--- | :--- |
| **Turn 1 / V2** | `operation:b6f0cc51d464a20832bd7827`<br>`09:28:30 – 09:29:03` | 改造前首次提问 | `partial` (`provider_no_result`)<br>`calls=0`, `search=0`, `tokens=0` | 连续 5 次在发送前阶段触发 `provider_transport_error`（`external_state="not_started"`），未获得模型响应。 |
| **Turn 1 / V3** | `operation:d7ff50a51b5265d7746f5ae0`<br>`10:30:57 – 10:35:40` | 改造前 `/continue` | `partial` (`no_cited_claims`)<br>`calls=18`, `search=8`, `127,392 tok` | 旧版 3 域名白名单与未启用 SEC 导致 8 次搜索后所有 `acquire_sources` 均被拒（`source_cannot_be_acquired`），0 份资料入库，报告为空。 |
| **Turn 2 / V4** | `operation:e4bd9a9a14e1be3c8aa8fbb2`<br>`13:36:46 – 13:40:15` | **改造上线后重新提问** | **`completed`** (`submitted`)<br>`calls=7`, `search=0`, `137,698 tok`<br>报告 `1,586` 字 | 模型直接调用 `sec_company_concept` 获取 FY2025/FY2026 的 `OperatingIncomeLoss` 与 `Revenues`（中途 2 次 `RevenueFromContractWithCustomerExcludingAssessedTax` 与 53 周结束日猜测返回 `unit_or_period_not_found_in_returned_records`，随后自动纠偏命中），记录 4 条 Fact、完成 4 项确定性计算与 6 条引文校验并交付完整报告。<br>隐患：第 7 次最终提交调用（`46978526...`）`output_tokens=3,831`，已紧贴 `4096` 上限；`model_reported` 指出“仅来自 SEC XBRL API，未直读 10-K 财务报表原文建立语义验证（`semantic_status: not_verified`）”。 |
| **Turn 3 / V5** | `operation:bf6d4d6b0a60ba437cec640f`<br>`13:41:58 – 13:44:34` | 点前端“补充上一轮的缺口”追问 | **`partial` (`provider_output_truncated`)**<br>`calls=4`, `search=2`, `96,101 tok`<br>报告 `0` 字，`partial_notes=None` | 1. **Call 1**（`7,927 tok`）：2 次 `discover_sources` 成功找到两份 SEC 10-K 直链（`nvda-20250126.htm`、`nvda-20260125.htm`），各有 6 条不透明跳转链接被安全过滤（`redirect_link: 6`），未再阻断整批发现；<br>2. **Call 2**（`8,858 tok`）：2 次 `acquire_sources` 成功抓取两份 10-K 原文入库（`source_4e9a23f79697313673205be749901243` `422,524` 字符、`source_96cc9bd5218aa1ddf4a9876a2ae04fff` `407,708` 字符，`retrieval_method="sec_filing_html"`）；<br>3. **Call 3**（`10,856 tok`）：2 次 `read_sources` 盲选 4 段各 50,000 字符（共 **200,000 字符**），tool result 分别长达 `101,033` 与 `101,056` 字符；<br>4. **Call 4**（`aa7ede00...`，耗时 `76.0s`）：输入膨胀至 `242,082` UTF-8 字节（实际 `~64,364` input tokens），在 `max_tokens=4096` 约束下触发 `stop_reason == 'max_tokens'`，在当时运行容器的 `anthropic_messages.py` 第 141 行（当前工作区流式重构版对应 [anthropic_messages.py](../../apps/api/app/anthropic_messages.py#L83-L84) `_build_response`）抛出 `ProviderFailure('provider_output_truncated')`，结算 **`68,460` tokens**。 |
| **Turn 3 / V6** | `operation:5b7b17eac36ac5fa51d8379f`<br>`13:48:53 – 13:50:03` | 第 1 次点击“继续研究” (`/continue`) | **`partial` (`provider_output_truncated`)**<br>新增 `+1 call`, `+68,462 tok`<br>Turn 3 累计 `164,563 tok` | Checkpoint 原样重放 Call 4 的 **`242,082` 字节上下文 + `max_tokens=4096`**，首轮即耗时 `67.7s` 在同一位置再次截断，白耗 `68,462` tokens。 |
| **Turn 3 / V7** | `operation:eea8edd1ae798fda88148003`<br>`13:50:20 – 13:51:32` | 第 2 次点击“继续研究” (`/continue`) | **`partial` (`provider_output_truncated`)**<br>新增 `+1 call`, `+68,462 tok`<br>Turn 3 累计 **`233,025 tok`** | 系统仍返回 `can_continue: true`，用户再次点击后第 3 次原样重放同请求，耗时 `69.9s` 后再次截断，再耗 `68,462` tokens，无任何进展。 |

### Turn 3 致命巧合的实证细节：读入 20 万字符却双双错过利润表

通过查询 PostgreSQL `research_sources` 原文字符偏移并与 `bf6d4d6b` `history[2]` 模型的 `thinking` 与 `read_sources` 入参比对，确认了当时发生了极具代表性的“双漏”现象：

1. 模型在 `thinking` 中明确意识到 10-K 超过 40 万字符且无法关键词搜索，只能估算 Item 7 / Item 8 大致比例来盲切两段 50,000 字符试探：
   - FY2025 10-K（`422,524` 字符）：读取 `[150,000..200,000]` 与 `[250,000..300,000]`。经查原文，`130,497` 出现在 **`203,201`**、`Consolidated Statements of Income` 出现在 **`213,993`** —— 恰好全部落在未读间隙 `[200,000..250,000]` 内。
   - FY2026 10-K（`407,708` 字符）：读取 `[140,000..190,000]` 与 `[240,000..290,000]`。经查原文，`215,938` 出现在 **`195,724`**、`Consolidated Statements of Income` 出现在 **`206,193`** —— 同样恰好落在未读间隙 `[190,000..240,000]` 内。
2. 最终上下文塞入了 200,000 字符的 Risk Factors（如出口管制说明）等无关段落，不仅一份利润表原文都没读到，反而把下一轮模型请求撑爆导致连环截断。

---

## 3. 根因缺陷拆解

### 缺陷 1（P0）：`provider_output_truncated` 后 `/continue` 原样重放，构成必现死循环

- **源码位置**：[research_runtime.py](../../apps/api/app/research_runtime.py#L798-L807)、[main.py](../../apps/api/app/main.py#L2982-L3025)、[Conversation.tsx](../../apps/web/src/pages/topic/Conversation.tsx#L87-L96)
- **机制缺陷**：
  1. 模型因 `stop_reason == 'max_tokens'` 失败后，`runtime_checkpoint` 仅把 `pending` 置为 `None`，**没有清理、压缩或折叠 `history` 里刚刚引发超限的巨型 `tool_result`（本例中为两笔 ~101 KB 的 `read_sources` 返回）**；
  2. [main.py](../../apps/api/app/main.py#L2982) `get_research_recovery_status` 仅检查 `pending` 是否未知和 `submission` 是否存在，直接返回 **`can_continue: true`**；
  3. 前端 [Conversation.tsx](../../apps/web/src/pages/topic/Conversation.tsx#L94) 随即将 **`"继续研究"` 作为主操作按钮（`btn primary`）** 提示用户点击；
  4. 用户每次点击 `POST /continue`，服务端都会以**一模一样的 `history`、一模一样的 `max_tokens=4096`、同一个模型**重新发包，数学上注定在同一位置再次截断，每次白耗 ~70 秒与 ~68.5k tokens，且每失败一次还会额外生成一个冗余草稿版本（`V5 -> V6 -> V7`）。
- **对比**：旧结构化分析路径 [research_coordinator.py](../../apps/api/app/research_coordinator.py#L575-L585) 曾针对 `provider_output_truncated` 做过输入切半（`generation-split-v1`）处理，但当前主路径 `research_runtime.py` 完全缺失针对 `provider_output_truncated` 的恢复策略或禁止重放保护。

### 缺陷 2（P0）：长文档缺乏站内检索/目录工具，且 `read_sources` 无单次/单批体积上限

- **源码位置**：[research_runtime.py](../../apps/api/app/research_runtime.py#L32-L42,L183-L192,L881-L910)、[research_fetch.py](../../apps/api/app/research_fetch.py#L192-L203)
- **机制缺陷**：
  1. `TOOLS` 中仅有基于字符区间 `[start, end)` 的 `read_sources`，**缺少文档内关键词检索（如 `search_source`）或章节/表格目录工具（如 `outline_source`）**。面对 40 万+ 字符的 SEC 10-K/20-F 或长篇财报 PDF，模型只能盲猜偏移量，且 10-K 正文开头的目录只有页码没有字符偏移，无法用于精确定位；
  2. `SourceRead` / `ReadRequest` 未限制单区间 `end - start`，也未限制单轮 `read_sources` 总返回字符数，允许模型一次批读 200,000 字符进对话历史；
  3. [research_fetch.py](../../apps/api/app/research_fetch.py#L192-L203) 在解析 SEC iXBRL HTML 表格时，为每个跨列空单元格保留 `| [colspan=3] |` 占位格式，进一步放大了表格前后文本的字符与 token 密度。

### 缺陷 3（P0）：`FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS='4096'` 过低且与推理模型不匹配

- **源码位置**：[main.py](../../apps/api/app/main.py#L644-L660)、[anthropic_messages.py](../../apps/api/app/anthropic_messages.py#L83-L84,L123)
- **机制缺陷**：
  1. `glm-5.3-flash` 在 `anthropic_messages` 协议下会输出较长 `thinking` 块再生成工具参数或长篇报告。`T2` 正常完成时末轮输出就已达 `3,831` tokens，距离 `4096` 仅差 `265` tokens；在本仓库另一条 SEC 实测（`task-745`）和公开网页实测（`task-691`）中也均在 `4096` 处触发 `provider_output_truncated`；
  2. 当上下文存在大量待消化资料时（如 `T3` 的 `~64,364` 真实输入 tokens），模型用于分析对比的 `thinking` + 下一组工具调用或报告很容易超过 `4096` tokens；
  3. [anthropic_messages.py](../../apps/api/app/anthropic_messages.py#L83-L84) 只要遇到 `stop_reason == 'max_tokens'` 就直接整包丢弃并报错，没有任何降级或安全缩减上下文后再试的余地。

### 缺陷 4（P1）：字节级输入估算（`1 byte = 1 token`）导致预估与实际上下文严重失配

- **源码位置**：[anthropic_messages.py](../../apps/api/app/anthropic_messages.py#L128-L129)、[research_coordinator.py](../../apps/api/app/research_coordinator.py#L33-L120)
- **机制缺陷**：
  1. `estimate_turn_input` 直接取 UTF-8 序列化字节长度（`242,082` 字节）充当输入 token 估算，对比英文财报真实输入（`~64,364` tokens）**虚高约 3.76 倍**，导致单次调用前向账户额度系统多冻结近 4 倍额度（`reserved_tokens=246,178`，结算实扣 `68,460`）；
  2. 因生产未配置 `FORESIGHTNINE_LLM_CAPABILITY_PROFILE` 且请求 `max_input_tokens_per_call=None`，运行时在发送 `202 KB` 工具历史前**没有任何输入体积警戒线或上下文自动折叠触发条件**，只能等发给模型并截断扣费后才事后失败。

### 缺陷 5（P1）：截断失败后的过程展示、中文文案与私有诊断盲区

- **状态与过程保留缺失**：`T3` 已经完成了高价值的线索发现和两份官方 10-K 原文下载（已入库 83 万字符原文，可在后续对话直接复用），但因失败在 `ProviderFailure` 返回分支（[research_runtime.py](../../apps/api/app/research_runtime.py#L798-L807)），`partial_notes` 为 `None`、报告为空，且没有说明“已成功获取 FY2025/FY2026 10-K，但单次读入过多导致响应截断”。
- **前端缺少常见错误码中文映射**：[copy.ts](../../apps/web/src/lib/copy.ts#L99-L165) 的 `TURN_REASON_LABEL` 与 `MODEL_FAILURE_LABEL` 未包含 `provider_output_truncated`（也缺少 `provider_context_limit`、`provider_request_rejected`），界面只能拼出中英混杂提示：`停止原因：模型服务返回错误（provider_output_truncated）` 与 `模型服务返回错误。（Provider output was truncated.）`。
- **私有诊断日志丢弃 `stop_reason`**：[anthropic_messages.py](../../apps/api/app/anthropic_messages.py#L83-L84,L432-L444) 在 `research_runtime.py:776` 写 `private_finish` 之前就因 `max_tokens` 抛出异常，而 `diagnostic_callback` 在 HTTP `200`（以及 SSE 流式路径）时未读取响应体的 `stop_reason`。结果数据库中 3 次截断的 `private_validation_diagnostics` 里 `finish` 与 `provider_error.code` 全记录为 `"unknown"`，排查必须靠异常栈行号反推。
- **次要状态口径滞后**：`T2` 通过 `sec_company_concept` 成功拉取两份 SEC 官方数据源并完成研究，但 `turn.official_fetch_status` 仍显示 `"no_eligible_source"`（目前仅 `acquire_sources` 回调会更新 `official_fetch_status`，见 [main.py](../../apps/api/app/main.py#L2712)）。

---

## 4. 上下文管理专章：前沿 Harness 与 `Ragent_product` 对比及架构取舍

检查最新工作区源码（含流式输出接入后的 [research_runtime.py](../../apps/api/app/research_runtime.py#L305-L423,L630-L637)）可见：**ForesightNine 对跨轮记忆（Turn N → Turn N+1 的 `_prior_research`，`PRIOR_LIMITS=200/100/100`）已有边界，但单轮工具循环内（`state['history']`）完全采用只追加、不清理、无上限模式**；而仓库早期批量流水线中的 [research_context.py](../../apps/api/app/research_context.py) 与 [research_coordinator.py](../../apps/api/app/research_coordinator.py#L575-L585) 切半机制并未接入新版原生工具循环。

参考目前主流 Agent Harness 实践（Anthropic 的 Tool Result Clearing / Note-Taking / Compaction 三层体系、Manus 的前缀 KV-Cache 优先与文件系统卸载、OpenAI Codex 的阈值压缩与 Compaction Loop 教训）以及同团队项目 [Ragent_product](https://github.com/ragent-app/Ragent_product.git) 中的对应实现，ForesightNine 必须结合自身**“短周期高密工具循环（4–15 步）+ 严格原文偏移引文校验（`locate()`）+ 高 KV-Cache 命中率（Turn 2 Call 7 达 `27,520 / 29,388` ≈ 93.6%）”**特点做显式取舍：

### 4.1 逐项技术对比与取舍结论

| 参考来源与对应模块 | 原生机制要点 | 取舍结论 | 在 ForesightNine 中的取舍理由与边界 |
| :--- | :--- | :--- | :--- |
| **Manus**：Context Offloading<br>**`Ragent_product`**：`chat/tool-result-descriptor.mjs` | 超大输出不直接留在会话上下文，而是持久化并保留 SHA-256 指针（`rawOutputRef`），上下文仅放受控预览，按 `readToolResultRange`（上限 `64 KB`）按需读取。 | **核心吸收（补齐最后一公里）** | ForesightNine 已将原文持久化于 `research_sources`（含 `source_id`、`content_sha256`），缺的是：**站内检索定位工具（`search_source`）、`read_sources` 单窗/单批上限、以及将旧 `read_sources` 结果折叠回紧凑 Descriptor。** |
| **Anthropic**：Tool Result Clearing<br>**`Ragent_product`**：`chat/session-auto-compaction.mjs` | 三档水位线：`50% TRIM_TOOLS`（零成本裁剪）→ `70% COMPACT`（后台 Worker 增量 LLM 摘要，每 `5` 轮全量重写防漂移）→ `85% EMERGENCY`（清 `resumeId`）。 | **吸收分层触发思想；<br>舍弃“后台异步 LLM Compactor Worker”** | 单轮研究循环内另起独立 LLM 摘要会话会带来 10–30 秒额外延迟、计费复杂度以及金融数字“二手改写幻觉”。轮内第一道与主防线应采用 Anthropic 最轻层——**纯确定性 Tool Result Clearing**（零模型调用、零幻觉）。 |
| **`Ragent_product`**：`chat/session-pre-compact-extraction.mjs` | 压缩前先低开销提取 8 条精确数字/约束写入持久化 `protectedFacts`。 | **无需引入 LLM 抽取器** | ForesightNine 已具备严格来源定位的结构化记忆：`state['facts']`、`state['calculations']`、`state['claims']` 与 `state['read_ranges']` 本身即是 100% 确定性的 Protected Facts。 |
| **Manus**：KV-Cache 黄金法则<br>**`Ragent_product`**：`chat/context-attention-assembler.mjs`、`context-renderer.mjs` | `Ragent` 在跨会话/跨 Harness 交接时按优先桶（P0 必选 / P1 结构化 30% / P2 最近 40% / 工具 12% / 召回 10%）重装上下文并用 `clipMiddle`（60%/40%）截断。 | **仅用于跨轮与越限一次性重组；<br>轮内禁止每步重排和 `clipMiddle`** | 1. 低于安全水位时，单轮 `SYSTEM + TOOLS + user + history` **必须保持 Append-only**，保住 90%+ 前缀 KV-Cache 命中；仅跨过水位线时做一次阶梯折叠形成新前缀；<br>2. **禁止对可引用原文用 `clipMiddle` 中段裁剪**，避免破坏 `locate()` 的逐字与字符偏移核验。 |
| **Codex**：Compaction Loop 教训<br>**`Ragent_product`**：`chat/continuity-core.mjs`、`context-receipt.mjs` | Codex 在压缩后丢掉“已失败/已尝试路径”会导致无限重读死循环；`Ragent` 通过 `failedAttempts` 与 `omittedRanges`（不变量 `I-CTX-003`）显式记录边界。 | **直接吸收（封堵 `/continue` 死循环）** | 运行时增加 `failed_attempts` 与 `omitted_tool_results` 账本，显式记住“因读取过宽在某区间后触发截断”，防止自动恢复或 `/continue` 再次重复同一无效动作。 |
| **`Ragent_product`**：`chat/model-context-windows.mjs`、`ceil(chars/3)` 估算 | 显式上下文窗口表（未知模型安全返回 `null`）与 `ceil(chars/3)` 工作估算。 | **吸收 `chars/3` 工作估算；<br>按 `AGENTS.md` 拒绝模型品牌硬编码** | 遵守 `AGENTS.md` 协议/配置优先原则，不写模型品牌正则表，而是完善 [provider_capabilities.py](../../apps/api/app/provider_capabilities.py) `CapabilityProfile` 与部署配置；**拆分“工作窗口估算（`ceil(chars/3)`，用于触发轮内清理）”与“账务防溢出上界（`utf8_byte_upper_bound`）”**，避免英文财报 3.76 倍虚高误杀。 |

### 4.2 原型核心结论

1. **短高密研究 Agent 的第一防线不是 LLM 摘要，而是“工具先定位 + 轮内确定性卸载”**：先把 20 万字符的盲读消减成 1–2 万字符的精准读取，绝大多数截断根本不会发生。
2. **上下文折叠必须“阶梯触发、不频繁抖动前缀”**：日常 Append-only 吃满 KV-Cache；越线或截断时一次性将早期大 `read_sources` 载荷替换为结构化占位引用（保留 `source_id`、`title`、`[start, end)`、SHA-256 与摘要提示）。
3. **保留失败记忆与不可隐瞒缺口边界**：折叠掉的旧文本不伪装成已彻底掌握，已有 `facts` / `read_ranges` / `failed_attempts` 作为结构化锚点常驻工作记忆。

---

## 5. 改进方案（融入上下文治理，待后续排期实施）

| 优先级 | 改造项 | 核心设计 | 预期效果 |
| :--- | :--- | :--- | :--- |
| **P0** | **新增文档内定位工具 + 约束 `read_sources` 单次/单批体积** | 1. 新增纯本地只读工具 `search_source`（在当前 run 已授权且符合 `as_of` 的 `research_sources` 内检索关键词/短语，仅返回命中字符偏移与前后各 ~300 字符上下文窗口）与可选 `outline_source`（章节/表头偏移）；<br>2. 在 [research_runtime.py](../../apps/api/app/research_runtime.py#L32-L42) 对 `read_sources` 设硬上限（例如单区间 ≤ `25,000` 字符、单批合计 ≤ `40,000` 字符），超限返回可读错误及总长度，引导先搜后读；<br>3. 优化 [research_fetch.py](../../apps/api/app/research_fetch.py#L192-L203) 空表格单元格拼接规则，折叠连续空 `|  |` 与纯占位 `[colspan=...]` 噪音。 | 从源头消灭 80% 无效输入体积；模型先定位 `"Consolidated Statements of Income"` 精确偏移（`213,993` / `206,193`），再用 1–2 万字符精准读出并完成引用核验。 |
| **P0** | **轮内阶梯式 Tool Result Clearing（保 KV-Cache）** | 1. 不改动数据库审计与断点里保存的完整原始历史，在 `turn_input()` 生成送模视图时引入工作水位检查；<br>2. 低于工作阈值（如估算工作 token < `55%–60%` 上下文窗口或历史工具载荷 < `100 KB`）时保持严格 Append-only；<br>3. 越过阈值后触发一次**确定性折叠**：保留 `SYSTEM`、`TOOLS`、`user`、**最近 1–2 步完整 `tool_result`** 以及全部 `facts`/`calculations`/`read_ranges`/`failed_attempts`，将更早轮次中的超大 `read_sources` 结果替换为紧凑 Descriptor（含 `source_id`、`read_ranges`、`total_characters`、`content_sha256` 与“正文在库中、必要时可窄范围重读”的提示）。 | 单轮哪怕执行 15–20 步深度核查，有效工作上下文也始终稳定在安全推理区内，同时正常步数保持 90%+ 前缀 KV-Cache 命中。 |
| **P0** | **打破 `provider_output_truncated` `/continue` 死循环** | 1. 在 `runtime_checkpoint` 记录结构化 `failed_attempts`（含失败原因 `provider_output_truncated`、当时输入规模与最后待消化 `read_ranges`）；<br>2. 禁止对 `provider_output_truncated` 做无改造的原样 `/continue`：首次续跑加载前强制执行一次激进 Tool Result Clearing（折叠掉引发超载的历史大段读取并注入“上次读段过多被截断，禁止原样重放，请改用 `search_source` 精确定位或提交已完成部分”约束）；<br>3. 若强制折叠后仍连续发生截断，[main.py](../../apps/api/app/main.py#L2982) `recovery-status` 置 `can_continue=false`（如返回新原因码），阻断前端继续引导点击“继续研究”。 | 彻底终结“点一次继续再等 70 秒、白扣 6.8 万 tokens”的死循环，同时允许已入库的 10-K 原文在紧凑上下文下顺利完成核验。 |
| **P0** | **调高研究模型的 `FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS`** | 通过部署环境配置上调 `FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS`（例如对 `glm-5.3-flash` 等带思维链模型从 `4096` 提升至 `16384`），并补齐 `FORESIGHTNINE_LLM_CAPABILITY_PROFILE` 中的显式 `context_tokens` / `output_tokens` 配置（遵从 `AGENTS.md` 不硬编码模型品牌）。 | 为 `thinking` 块与带完整引文/对比表的终版报告留足输出裕度，消除正常工具调用与 `submit_research` 被 `4096` 硬切的问题。 |
| **P1** | **估算双轨制 + 补全前端文案与诊断可观测性** | 1. 保留 `utf8_byte_upper_bound` 仅用于账务配额与极端协议保护；轮内上下文水位检测改用接近英文/中文混合现实的 `ceil(chars / 3)` 启发式估算；<br>2. 在 [copy.ts](../../apps/web/src/lib/copy.ts) 补全 `provider_output_truncated`、`provider_context_limit`、`provider_request_rejected` 中文说明；<br>3. 运行时因截断中止时生成客观 `partial_notes`（只列已入库来源清单、已完成阶段与停止原因）；<br>4. 在 [anthropic_messages.py](../../apps/api/app/anthropic_messages.py#L432-L444) 中将 HTTP 200 响应及 SSE 流事件里的 `stop_reason` 同步写入 `private_diagnostics`，并让 `sec_company_concept` 成功调用同步更新 `official_fetch_status`（或独立标注）。 | 既不误杀正常 6 万 token 英文财报，又能在中断时向用户和排查日志提供完整中文解释与精确诊断。 |

---

## 6. 后续实施验证标准（规划）

1. **单元测试与契约回归**：
   - `anthropic_messages`（含普通 JSON 与 SSE 流式模式）发生 `max_tokens` 时私有诊断正确记录 `finish` 为 `max_tokens`（或标准化码）；
   - `read_sources` 超限拦截返回可读错误；新增 `search_source` 严格受当前运行已授权可见来源列表与 `as_of` 约束，输出只含位置与原文片段；
   - 轮内 Tool Result Clearing 验证：低于阈值不修改 `history` 前缀（保证 KV-Cache 稳定性）；超阈值或 `provider_output_truncated` 后准确折叠早期 `read_sources` 并保留 `facts`/`read_ranges`/`failed_attempts`；连续两次截断后 `recovery-status` 正确置 `can_continue=false`；
   - Web 前端正确渲染 `provider_output_truncated` 中文提示，且不再对死循环断点推荐“继续研究”。
2. **线上实测验收对照**：
   - 在隔离测试或受控复测环境中以 `topic_8e91c0299a7b4b25823bc12ab9c7e53a` 的两段核心路径验证：
     1. 首次对比英伟达最近两个财年总收入与营业利润率；
     2. 追问“从 10-K 财务报表原文核对并建立语义验证”——验证模型能否先通过 `search_source` 命中两份 10-K 合并利润表原文，配合精读窗口与必要时的确定性折叠，最终完成 `record_facts` / 引文核验并产出 `status="completed"`，无 `provider_output_truncated` 与 `/continue` 死循环，同时记录实际 `cache_read_input_tokens` 命中率与总耗时/Token 变化。

---

## 7. 实施与阶段性验收记录（2026-11-19）

本节记录上述改进方案按三阶段（Stage 1 工具与诊断基础 → Stage 2 上下文折叠与熔断保护 → Stage 3 合约门禁、配置调优、公网滚动升级与受控回环真实验收）的实施内容与实际核验结果（含如实未完成项与局限）。

### 7.1 Stage 1：文档导航、表格瘦身、诊断与双轨估算（提交 `7cc486f`，看板验收 `#12`）

- **HTML 表格瘦身**（[research_fetch.py](../../apps/api/app/research_fetch.py)）：`_TextParser`（配合 `_TableRowState` / `_TableCellState`）过滤空占位 `<td>`/`<th>` 单元格、空白占位行、无正文内容的 `[colspan]`/`[rowspan]` 标记及行尾空单元格，将连续空列折叠为单个 `|  |` 分隔符，同时保持财务数值、嵌套表格与 `research_table_provenance` 行列对齐。
- **站内只读定位工具与 `read_sources` 硬上限**（[research_runtime.py](../../apps/api/app/research_runtime.py)）：
  - 新增只读工具 `outline_source` 与 `search_source`，并在工具执行与 `_tool_activity_meta` 中强制执行 `_is_source_eligible(source, as_of)`（不泄露受限、FRED 或晚于 `as_of` 的来源标题、字符数与正文）；
  - 在 `_read_sources` 中引入 `MAX_READ_SOURCE_SPAN_CHARS = 25000` 与 `MAX_READ_SOURCES_BATCH_CHARS = 40000` 硬上限；单区间超限（含对 `>25,000` 字符原文传入 `end=None`）或单批超限均返回 `status='invalid'`（`is_error=True`）、总长度与先调 `outline_source`/`search_source` 再读 `[start, end]` 的引导而不污染 `read_ranges`，`<=25,000` 字符小来源沿用原行为。
- **`stop_reason` / 用量 / 报文观测诊断**（[anthropic_messages.py](../../apps/api/app/anthropic_messages.py)、[research_diagnostics.py](../../apps/api/app/research_diagnostics.py)、[research_coordinator.py](../../apps/api/app/research_coordinator.py)）：在 HTTP 200 JSON 与 SSE 流式两条路径触发 `provider_output_truncated` 或结构化校验失败抛出异常前，安全采集并仅写入一次 `stop_reason`、标准化 `finish`、`usage`、`wire_bytes`、`max_tokens` 与 `stream`；保持白名单脱敏，不泄露密钥、提示词或半成品输出。
- **双轨输入估算与非硬编码能力配置**（[provider_capabilities.py](../../apps/api/app/provider_capabilities.py)、[research_coordinator.py](../../apps/api/app/research_coordinator.py)）：新增 `DualInputEstimate`（拆分账务/准入保守上界 `utf8_byte_upper_bound` 与工作容量估算 `chars_div_3` 即 `ceil(chars/3)`）、`configured_research_max_output_tokens`、`resolve_output_token_limit` 与 `VALID_INPUT_COUNTERS`，全过程不硬编码任何模型品牌。
- **Web 中文化错误映射**（[copy.ts](../../apps/web/src/lib/copy.ts)）：补全 `provider_output_truncated`、`provider_context_limit`、`provider_request_rejected` 等失败文案、`TOOL_LABEL` 中的 `search_source`/`outline_source`、`PROVIDER_ERROR_MESSAGE_LABEL` 与 `recoveryReasonLabel` 安全兜底。
- **Stage 1 验收结果（容器内执行）**：
  - API Stage 1 与流式/输入预算 12 个测试文件 **`199 passed`**；
  - API 下游 10 个回归文件 **`210 passed`**；
  - API 既有遗留上下文/容量基线（[test_research_context_capacity.py](../../apps/api/tests/test_research_context_capacity.py)、[test_runtime_capacity_policy.py](../../apps/api/tests/test_runtime_capacity_policy.py)、[test_research_input_budget.py](../../apps/api/tests/test_research_input_budget.py)）：**`17 passed, 3 failed`**（其中 `test_research_input_budget.py` `2/2` 通过；3 项失败均属 `949fc89` 之前即存在的旧版 `ResearchOrchestrator._choose` 与已退役分阶段 `/runs` 字段返回 HTTP 422 的基线差异，与 Stage 1 无关）；
  - Web（`foresightnine-web-test:local`）：`typecheck` **PASS**，`copy.test.ts` + `TopicPage.test.tsx` `40/40 passed`，全量 `vitest run`（21 个文件）**`231 passed`**。

### 7.2 Stage 2：KV-Cache 友好折叠、失败记忆、`/continue` 熔断与界面保护（工作区相对 `7cc486f` 的 11 个已跟踪文件，看板验收 `#24`）

- **共享轮内 L1 确定性压缩**（[research_runtime.py](../../apps/api/app/research_runtime.py)、[research_recovery.py](../../apps/api/app/research_recovery.py)）：实现唯一共享函数 `compact_runtime_history(state, *, keep_recent=2, trigger_chars=120_000, force=False, reason=None)`（由 `research_recovery.py` 直接导入复用）。未超过阈值（随 `max_input_tokens`、`research_capacity` 与剩余 `max_total_tokens` 动态收紧）时保持 `state['history']` 前缀字节不变以保留前缀 KV-Cache；越限时仅将更早轮次的大体积工具结果折叠为紧凑 JSON 描述符（保留 `source_id`、`source_spans`、`original_chars`、`facts_recorded_count` 与重定位提示），同时保留最近 `keep_recent` 步、`read_ranges`、`facts`、`calculations` 与 `events`，确保 `record_facts`/`submit_research` 的 `locate()` 持续通过。
- **失败记忆、瞬态引导与中文 `partial_notes`**（[research_runtime.py](../../apps/api/app/research_runtime.py)）：在截断、上下文超限、不完整响应、`no_submission` 及非重试/结算失败（含 `received=False` 截断/超限）时将 `failed_attempts`（`call_id`、`reason`、`finish_reason`、`usage`、`history_chars`、`at`）写入检查点；`turn_input()` 仅瞬态注入有界 `prior_failed_attempts`、`recovery_guidance` 与 `continuation_guidance`（不污染 `state['user']`）；`outcome('partial', ...)` 缺省时自动生成简洁中文 `partial_notes`（`_default_partial_notes`）。
- **`/continue` L2 强制压缩、连续截断熔断与 `sec_company_concept` 状态同步**（[research_recovery.py](../../apps/api/app/research_recovery.py)、[main.py](../../apps/api/app/main.py)）：
  - `RecoveryJournal.claim_runtime_continuation` 与 `prepare_runtime_continuation` 在建议锁内执行 L2 强制压缩（`keep_recent=1, force=True`，若因截断/上下文超限或超出 `effective_limit` 则升级为 `keep_recent=0, force=True`），清除 `stopped` 并递增 `continuation_count` 与 `consecutive_truncation_continuations`；
  - 新增 `MAX_TRUNCATION_CONTINUATIONS = 2`（`runtime_truncation_loop_blocked`）熔断器：连续达到 2 次截断续跑后，`get_research_recovery_status` 与 `recovery_status` 均返回 `can_continue=false, can_deliver=false, reason='repeated_truncation'`，`POST /continue` 在调用 `start_research_run` 前直接拒绝并返回 HTTP `409`（`lifecycle_conflict`）；
  - `query_sec_concept` 通过 `_merge_official_fetch_status` 同步更新 `official_fetch_calls` 与 `official_fetch_status`（不降级先前 `'completed'`/`'retrieved'`，失败时不覆盖 `'not_requested'`）。
- **Web 恢复保护与中文文案**（[copy.ts](../../apps/web/src/lib/copy.ts)、[Conversation.tsx](../../apps/web/src/pages/topic/Conversation.tsx)）：`RECOVERY_REASON_LABEL` 补齐 `repeated_truncation` 中文说明（不暴露原始错误码或内部路径）；`CalloutActions` 在 `repeated_truncation` 时屏蔽继续/交付/补缺按钮并将检查文案修正为 `"正在检查恢复状态…"`，`GapCallout` 与 `PartialNotesBlock` 协同展示。
- **Stage 2 验收结果（容器内执行）**：
  - API 18 个核心研究/恢复/流式/SEC/计算套件 **`393 passed`**；
  - [test_research_recovery_acceptance.py](../../apps/api/tests/test_research_recovery_acceptance.py)（`FORESIGHTNINE_LOCAL_AUTH_MODE=mock`）：**`18 passed`**（含全部 6 项 Stage 2 与恢复用例及 12 项回执用例），`1 skipped`（未配 Postgres DSN），`17` 项为既有遗留分阶段流水线 HTTP 422 / `direction_derivation` 基线失败；
  - Web `typecheck` **PASS**，`vitest run` 21 个文件 **`234 passed`**；
  - **离线 Stage 2 端到端检查 PASS**：实证 120k 水位以下前缀字节严格不变、跨过 120k 后 L1 折叠且 `read_ranges`（6 段）保留并完成 `record_facts` + `submit_research`；真实 `AnthropicMessagesProvider` 模拟截断 → `/continue` L2 折叠使报文由 `22,292` 降至 `15,374` 字节并顺利完成；二次截断触发 `repeated_truncation` 熔断且第三次 `POST /continue` 返回 HTTP `409`（未再发起模型调用）。

### 7.3 Stage 3：契约/安全门禁、配置调优、滚动部署与受控回环实测（看板验收 `#27`、`#35`）

- **部署前门禁与可逆备份**：
  - `export_openapi.py --check`（`PASS OpenAPI 25 paths`）、`packages/contracts` 两个生成检查脚本、`packages/asset-registry` 校验与 `scripts/verify/boundaries.mjs` 均 **PASS**；`scripts/verify/secret-scan.mjs` 扫描 6,670 个历史 Git 对象及工作区，`git diff HEAD` **0 新告警**（仅余既有测试夹具/Phase A 文档与历史 Git blob 基线匹配）；
  - 在 `/home/ubuntu/.local/state/foresightnine/aws-runtime` 创建可逆备份：镜像 `foresightnine-migration-api:pre-contextgov-20261119`（`00af76c0062d`）、`api.env.bak-contextgov-20261119` 与 `web.bak-contextgov-20261119`。
- **运行时配置调优与滚动重启**：
  - 更新 `aws-runtime/api.env` 非秘密参数：`FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS='16384'`（由 `4096` 提升）、`FORESIGHTNINE_LLM_ENDPOINT_VERSION='2023-06-01'` 及 `FORESIGHTNINE_LLM_CAPABILITY_PROFILE`（`chars_div_3`, `output_tokens: 16384`, `mode: "json_object"`, `native_tools: true`, `stream: true`, `continuation: true`，不设 `context_tokens` 以保留 `utf8_byte_upper_bound` 计费防护与 `120,000` 字符 L1 水位）；
  - 在隔离构建容器重建 Web 静态资源（`assets/index-P5gDvInw.js` `533,807 B`、`assets/index-ThGviAW-.css` `44,854 B`，保留 4 个发布 App Link 指纹且实测包含 Stage 1/2 中文文案）与 API 镜像 `foresightnine-migration-api:public`（`17b1cd96a8fb`），滚动重启 `aws-runtime` 全部 6 个容器（`api`、`challenge-gateway`、`origin`、`postgres`、`redis`、`web`），回环及公网 `https://app.foresightnine.com` `/readyz`、`/`、`/.well-known/assetlinks.json` 等均 `200`。
- **受控回环真实验收（`http://127.0.0.1:8000`，`anthropic_messages` / `glm-5.3-flash`，使用隔离一次性账号并在测后清理，未触动 `account_8887e09f4185483a8323042c6e4ba643` 与 `topic_8e91c0299a7b4b25823bc12ab9c7e53a`）**：
  - **已实证生效的改进**：
    1. **Run 1**（`operation:c3624fb5e817bd907ed11bc0`，MSFT FY24 vs FY25，上限 `8 calls / 240s / 200k tokens`）：`sec_company_concept` 获取 4 份 US-GAAP 数据源并正确同步 `official_fetch_status="completed"`；轮内 L1 压缩（`context_compacted`, `compaction_count=2`）将 Call 5 `wire_bytes` 从 `141,472` 降至 `43,057`（`input_tokens` `39,686 -> 12,104`），结束时生成中文 `partial_notes`；
    2. **10-K 表格瘦身直测**（`POST /v2/research/sources/fetch`）：`nvda-20260125.htm`（`sec_filing_html`）抽取文本由改造前 `407,708` 字符降至 **`375,946`** 字符（减少约 `31.8k` 字符）；（同批 `msft-20250630.htm` 返回上游 `503`、`aapl-20260926.htm` 触发 SEC `429` 限流）；
    3. **Run 2**（`operation:d0e4b1c19ba4c8ed29027013`，针对 `375,946` 字符 10-K，上限 `8 calls / 0 search / 240s / 150k tokens`）：模型先调用 `outline_source` + 5 次 `search_source` 再精准 `read_sources`（单切片 `800–2,365` 字符，零 5 万字符盲读），完成 6 条 `record_facts` 与 4 项 `calculate_financials`；SSE `/events` 回放正常；
    4. **Postgres 诊断与前缀缓存实测**（`research_run_inputs.private_validation_diagnostics`）：确认 `max_tokens=16384` 生效（Run 1 Call 4 临近 200k 预算上限时安全钳位为 `6640`），`stop_reason`、`finish`、`wire_bytes`、`stream: true`、`budget_estimate`（含 `utf8_wire_estimate` 与 `ceil_chars_div_3`）及 `usage` 完整落库；观测到 `cache_read_input_tokens` 命中（`6,016` / `9,536` / `16,448` / `11,904`；`api.z.ai` 不返回 `cache_creation_input_tokens`）；
    5. **Run 4 `/continue` 真实续跑**：`operation:ab6fd6a9f194754368fea44b` 遭遇单次 `120.0s` 上游流式超时后，触发 `POST /continue`（`operation:05b2b80c337c7f1c0f357351`）返回 HTTP `202`、标记旧 checkpoint `already_used` 并递增 `continuation_count=1` 继续执行 `search_source` + `read_sources`。
  - **如实记录的实测边界与未覆盖项**：
    1. 因线上 `discover_sources`（`glm_coding_mcp`）返回 `search_response_invalid`、`api.z.ai` 上游流式延迟较高（单次可达 `>120s`）以及测试严格控制预算与总时长（`150k–200k` tokens / `180–300s`），全部 **5 次受控真跑均在最终成功 `submit_research` 前以 `partial`（`provider_timeout` 或 `provider_budget_exhausted`）结束**，本轮尚未在公网受控窗口内录得一次 `completed` 全量交付；
    2. `read_sources` 超 `25,000` 字符报错、`stop_reason='max_tokens'` 真实截断以及前端 `repeated_truncation` 熔断路径在本次受控真跑中**未实际触发**（前述三项已在 Stage 1/2 单元测试与离线 E2E 中通过验证；本次 Run 2 Call 5 的 `3,294` tokens 输出在 `16384` 上限下未再截断）；
    3. 一次性测试账号经 `DELETE /v1/auth/local/account` 清理（仅余账务审计表 `billing_states` 1 行与 `usage_ledger` 20 行），`git status -sb` 仅含 Stage 2 的 11 个已跟踪文件，无临时脚本或密钥泄露。
