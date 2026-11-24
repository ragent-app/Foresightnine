# 方向 B（阶段 C）深度架构与产品方案：持续跟踪、结构化假设台账与变化收件箱

> **范式更新（2026-11-24）**：产品顶层交互与主副关系已确立为 **“方向 A 做后台高保真底稿引擎，方向 B 做前台 30 秒决策卡与变化雷达（渐进式下钻完整底稿）”**，详见 [product-paradigm-a-engine-b-radar-2026-11-24.md](./product-paradigm-a-engine-b-radar-2026-11-24.md)。融入 V1 已上线引擎与 P1-B 错峰调度的可执行落地规格见 [direction-b-radar-and-inbox-execution-plan-2026-11-24.md](./direction-b-radar-and-inbox-execution-plan-2026-11-24.md)。

- **日期**：2026-11-24
- **前置基线**：方向 A 美股单公司买方财报深研已连续在 **NVDA（半导体）**、**META（互联网广告）**、**JNJ（医药）** 三行业跑通端到端实测（0 失败、TTM FCF / Reverse-DCF 全激活、引文与空头锚定门控生效、组织图与侧栏生命周期全闭环）。
- **核心目标**：打通 [AGENTS.md](/home/ubuntu/Foresightnine/AGENTS.md) 中首个可试用版本的第二半闭环（**阶段 C：持续观察与增量更新**），回答主动投资者的终极问题：**“新信息出现后，我原来的判断是否需要修改？”**

---

## 一、深度诊断：为什么方向 B 才是真正不可替代的护城河？

### 1. 通用 AI 的结构性天花板
- 任何通用 AI 都可以“丢一份财报 PDF → 写一段摘要”，哪怕方向 A 已经把单次拆解做到了机构级（SEC 8-K/10-Q 深度解析 + 确定性 Decimal 排雷 + Reverse-DCF + 锚定空头反证），如果停留在“单次问答”，用户看完报告后，下次财报季依然要从零开始回忆“我上季度到底在赌什么、当时最担心什么、管理层当时承诺了什么”。

### 2. 方向 A 已经为方向 B 备齐了 80% 的“原材料”
当前每一份锁定的 `EarningsDossier` 已经天然包含以下结构化资产：
- **可量化下季证伪阈值**：`bear_cases[].next_quarter_invalidation_trigger`（如“AR 增速 - 营收增速剪刀差 <= +5pp”、“OCF/NI >= 0.80x”、“Q4 GAAP 毛利率 >= 74.0%”）；
- **管理层当期承诺**：`snapshot.guidance_nongaap.new_guidance`（下季/全年营收、毛利率、EPS、CapEx 指引）；
- **确定性诊断旗标**：`snapshot.flags`（R/Y/G 盈利质量与营运资本信号）；
- **隐含增长预期**：`snapshot.valuation.reverse_dcf.implied_5y_fcf_cagr_pct`；
- **跨期披露红线**：`snapshot.redline_summary` 与 SEC `accession` 溯源。

### 3. 方向 B 要解决的 4 个核心痛点
1. **逻辑不沉淀**：单次报告里的核心假设与证伪指标无法独立保存、编辑和跨期追踪；
2. **财报季盯不过来**：投资者跟踪 10–30 家公司时，难以第一时间知道哪家发了新 8-K/10-Q/10-K、哪条变动真正冲击了原投资逻辑；
3. **重复更新成本太高**：新公告到来如果重跑一遍 800K Token 的全量 6 阶段 Squad，既慢又贵，且会淹没“真正发生变化的那 10%”；
4. **结论修改缺乏审计链**：AI 如果静默覆盖用户已确认的判断，专业投资者根本不敢信任；必须展示新旧差异、原文依据，并记录用户的接受/修订/异议过程。

---

## 二、核心数据模型与状态机（全部 Pydantic `extra="forbid"` + Postgres `research_state_documents`）

### 1. `ThesisLedger` & `ThesisClaim`（版本化投资假设台账）
- 挂在每个公司研究主题（`topic_id` + `entity_id` / `cik`）下，独立于聊天上下文持久化。
- **版本化结构**：
  - `ledger_id`, `topic_id`, `account_id`, `entity_id`, `symbol`, `cik`
  - `version: int`（`1, 2, 3...`），`parent_version: int | None`
  - `review_status: Literal["draft", "confirmed"]`（遵循 `AGENTS.md`：用户确认后的核心判断不可被后台自动任务静默覆写）
  - `base_dossier_id`, `base_period_label`, `confirmed_at`
  - `claims: list[ThesisClaim]`（建议 3–5 条核心支柱）
- **单条 `ThesisClaim`**：
  - `claim_id`, `pillar: Literal["growth", "margin", "cash_quality", "capital_allocation", "valuation", "risk"]`
  - `statement: str`（简洁的核心判断，支持用户直接编辑）
  - `stance: Literal["bull", "cautious", "bear", "neutral"]`
  - `watch_triggers: list[WatchTrigger]`：
    - 支持 **确定性数值型 Trigger**（`kind="metric_rule"`, `metric_key`, `operator` (`>=`, `<=`, `>`, `<`, `delta_bps`), `threshold_decimal`, `horizon`）——新财报到达时由 Python Decimal 代码 100% 确定性求值；
    - 支持 **文本/事件型 Trigger**（`kind="disclosure_rule"`, `description`, `target_sections` 如 `8-K EX-99.1 / Item 1A / Commitments`）——由增量 Reviewer 结合原文引文判定。
  - `invalidation_condition: str`（明确证伪条件，初始可直接承接 `bear_cases[].next_quarter_invalidation_trigger`）
  - `status: Literal["active", "strengthened", "weakened", "invalidated", "superseded"]`
  - `evidence_refs: list[str]`（关联的 `dossier_id` / `accession` / `source_id`）
  - `user_note: str | None`（用户保留异议或补充备注）
- **零额外高成本建档**：首轮 `EarningsDossier` 锁定后，提供“一键生成假设台账草案”（从锁定的 `EarningsDossier` 紧凑投影单次轻量抽取 3–5 条 `ThesisClaim`，成本 <25K Token），用户可编辑、增删后点击 **“确认判断”**。

### 2. `FilingWatchSubscription` & `WatchHealth`（单一 SEC 调度分流器与去重）
> **与 V1 架构对齐（2026-11-24）**：为避免方向 B 与 V1 事件引擎重复轮询 SEC 及过滤口径冲突，每个被跟踪公司只维护**一套共享 SEC 调度分流器（Single SEC Filing Dispatcher）**与共享游标（详见 [v1-sec-events-insider-ownership-design-2026-11-24.md](./v1-sec-events-insider-ownership-design-2026-11-24.md) v6）。

- **按主题显式订阅**（不做全市场无差别爬取）：
  - `subscription_id`, `account_id`, `topic_id`, `cik`, `symbol`, `enabled: bool`
  - `watched_forms: list[str]`（默认覆盖财报与非财报事件：`["8-K", "10-Q", "10-K", "20-F", "6-K", "4", "SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G"]`，含对应 `/A` 修正案）
  - `seen_accessions: dict[str, dict[str, str]]`（按通道记录状态：`accession -> {"earnings"?: status, "v1"?: status}`，支持单份混合 `8-K` 双路独立幂等，防重复外呼与重复 LLM 调用）与 `failed_accessions: dict[str, dict[str, str]]`（某通道抓取/解析失败仅记入该通道的 `failed_accessions` 且**绝不写入该通道的 `seen_accessions`**，下次巡检只重试失败通道）
  - `last_checked_at`, `next_check_at`, `check_interval_minutes`（默认 60 分钟 + 前端支持“立即检查”，底层复用 SEC 4–5 req/s 共享可等待 Pacing 限流器）
  - `health_status: Literal["healthy", "degraded", "error"]` + `last_error: str | None`
- **硬性纪律（来自 `AGENTS.md`）与单一确定性分流口径（0 LLM，Item 级双路分流）**：
  - **严格区分“没有重要变化”与“监控故障”**：SEC 限流/网络超时写入 `health_status="degraded" | "error"` 并独立展示，绝不静默伪装成“一切正常”；
  - **确定性 Item 级双路分流（基于原始 `submissions.items` 与外置规则包）**：
    1. **财报增量复核通道（方向 B Delta Review）**：`10-Q`、`10-K`、`20-F`（含对应 `/A` 修正案）、含 `Item 2.02` 的 `8-K`、以及经规则识别为季度/年度业绩发布的 FPI `6-K`；
    2. **非财报重大事件通道（V1 事件引擎）**：非财报 `8-K`（含 `Item 5.02` 核心高管/董事变动与全量 `Item 8.01`，仅按外置规则屏蔽纯 `5.07 / 5.03 / 3.02 / 3.03 / 9.01` 法定噪音）、`Form 4 / 4/A`、`SC/SCHEDULE 13D / 13G`（含 `/A`）以及非业绩 FPI `6-K`。V1 输出统一 `CompanySignalEnvelope` 并自带 `affected_thesis_pillars`，直接路由至方向 B 对应 `ThesisClaim` 与变化收件箱（支持按 `event_thread_key` + `superseded_by_accession` 折叠修正案）；
    3. **混合 `8-K` 双路保底**：当同一份 `8-K` 同时包含 `Item 2.02` 与其他实质非财报 Item（如财报日同发 `5.02` 高管离职、`2.05` 重组、`4.02` 重述或 `8.01` 重大事项）时，**同时路由至财报通道与 V1 通道**（V1 剥离 `2.02` 后仅研判非 `2.02` Item），两通道独立维护 `seen_accessions` / `failed_accessions` 互不阻塞、绝不因含 `2.02` 而漏报高危非财报事件。

### 3. `DeltaReviewRun` & `ThesisScorecardDiff`（低成本 2 阶段增量复核引擎）
当新实质公告（8-K / 10-Q / 10-K）到达，或用户在主题内上传新资料（电话会纪要/研报）点击“增量复核”时，**不重跑 800K Token 的 6 阶段全量 Squad**，而是执行 **2 阶段轻量 Delta 流水线**（目标单次 `< 150K–200K` Token，耗时 `< 60s`）：

- **Delta Stage 0：纯确定性差异计算（0 LLM 调用）**
  1. **只拉新增量**：复用 `sec_edgar.py` / `sec_deep.py` 仅抓取新 `accession` 的 8-K EX-99.1 或 10-Q/10-K 分节与最新 XBRL 期间；
  2. **管理层承诺兑现表（Promise vs. Delivery）**：将上期 `EarningsDossier.guidance_nongaap.new_guidance` 自动与本期实际营收/毛利率/EPS/CapEx 对撞，确定性输出 `Beat / Inline / Miss` 及精确差值；
  3. **旗标与估值漂移（Diagnostic & Reverse-DCF Drift）**：对比上期与本期 R/Y/G 旗标状态迁移（例如 `green → yellow`、`yellow → red`）及 Reverse-DCF 5 年隐含 FCF CAGR 漂移；
  4. **跨期红线增量（Redline Delta）**：若新公告为 10-Q/10-K，自动跑 `sec_redline_diff` 对比上一期，只保留实质增删；
  5. **数值型 Trigger 自动求值**：对 `ThesisLedger` 中所有 `kind="metric_rule"` 的观察条件用 Decimal 直接求值（`triggered / cleared / unchanged / awaiting_10q`）。
- **Delta Stage 1：精简增量复核 Agent + 服务端引文门控（1–2 次 LLM 调用）**
  - 输入仅喂给模型：**当前 `ThesisLedger` + Delta Stage 0 确定性差异包 + 新公告关键片段**（输入严格控制在 `25K–45K` tokens）；
  - 输出严格 JSON `ThesisScorecardDiff`（`extra="forbid"`）：
    - `overall_impact: Literal["no_material_change", "reinforces_thesis", "mixed_signals", "challenges_thesis", "thesis_broken"]`
    - `claim_updates: list[ClaimDeltaUpdate]`：每条假设的新状态（`strengthened | unchanged | weakened | invalidated`）、变化依据、原文引文（经 `_quotation_matches_source` 门控核验）、以及建议修订文案（`proposed_statement`）；
    - `new_variables: list[NewVariableItem]`：原台账未覆盖的新增重大变量（如突发诉讼、重大并购、指引结构性下修），附带建议新增的 `ThesisClaim` 草案；
    - `promise_delivery_summary`：上季管理层承诺兑现总结；
    - `open_gaps`：例如“仅有 8-K 业绩新闻稿，10-Q 营运资本/现金流待正式归档后二次补核”。

### 4. `ChangeInboxItem`（变化收件箱与用户确认版本流）
- **静默降噪原则**：若 `overall_impact == "no_material_change"` 且无数值 Trigger 触发，则记录审计日志但不产生未读红点提醒。
- **实质变化入箱**：生成一条去重的 `ChangeInboxItem`：
  - 事件头：公司（Ticker + 名称）、触发来源（如 `FY2027 Q3 8-K EX-99.1 · 2026-11-17` 或 `用户上传电话会纪要`）、总体影响徽章（`强化 / 混合 / 削弱 / 证伪`）；
  - **假设记分卡 Diff 视图**：逐条展示 `旧状态 → 新状态`、`原判断 vs. 建议修订`、确定性 Trigger 命中结果、以及可点击穿透原文的引文脚注；
  - **管理层承诺兑现表**：上季指引 vs. 本季实际；
  - **用户确认动作（严禁静默覆写已确认判断）**：
    - **[接受全部建议 → 升级至 v(N+1)]**
    - **[逐条编辑后接受 → 升级至 v(N+1)]**
    - **[保留原判断（记录异议原因）]**
    - **[标为已读]**
  - 接受后自动生成新版本 `ThesisLedger`（保留完整 `v1 → v2` Diff 历史），并提供可选按钮 **“基于新财报重跑完整买方底稿”**（由用户按需决定是否花全量 Token，不强制自动重跑）。

---

## 三、前端信息架构与交互设计（克制、专业、零内联样式）

1. **侧栏一级入口：`变化收件箱`（`/inbox`）**
   - 位于侧栏顶部导航区（与“新建研究”并列），仅当存在未处理实质变化时显示数字徽章；
   - 收件箱页面顶部展示 **监控健康条**（如 `正在跟踪 5 家公司 · 上次巡检 12 分钟前 · SEC 通道正常`；若有异常，明确标红提示“监控故障”，绝不与“无变化”混淆）；
   - 支持按公司、影响级别（证伪/削弱/强化）、状态（待复核/已确认/已保留异议）筛选。
2. **主题页（`TopicPage`）新增轻量化模块：`假设台账 & 持续跟踪`**
   - 位于 `EarningsDossierView` 顶部快捷条或独立 Tab（不破坏当前干净的买方备忘录阅读流）：
   - **一键建档**：刚跑完首轮财报研究时，显示引导条 `“沉淀本轮 4 条核心假设与下季证伪阈值，开启 SEC 新公告自动跟踪”`；
   - **台账卡片**：展示每条假设的支柱标签、当前状态徽章、可编辑判断文案、量化观测阈值与证伪条件；
   - **版本时间线**：支持点击 `v1 → v2` 查看每次财报季的判断演变与当时的原文依据；
   - **手动入口**：提供 `立即检查 SEC 新公告` 与 `上传新资料并增量复核` 两个显式按钮。

---

## 四、无需等待下季财报的实证验收方案（历史 `as_of` 跨季回放）

不依赖未来 3 个月才发布的新财报，直接用真实美股历史 SEC 披露做 **Back-to-Back 跨季回放验收**：
- **3 家跨行业真实回放样本**：
  1. **NVDA**：`as_of = FY2027 Q2 10-Q 披露日` 建档（v1） → 放开至 `FY2027 Q3 8-K 披露日` 触发增量复核（验证 Q3 收入超指引 + Q4 毛利率指引下修 330bp 触发 `margin` 削弱 + 标明 Q3 10-Q 尚未归档的诚实缺口）；
  2. **META**：`as_of = 2026 Q2 10-Q 披露日` 建档（v1） → 放开至 `2026 Q3 8-K + 10-Q 披露日` 触发增量复核（验证 Q3 单季 FCF 转负与 98.9 亿美元法律和解费用触发 `cash_quality` / `risk` 警报）；
  3. **JNJ**：`as_of = 2026 Q2 10-Q 披露日` 建档（v1） → 放开至 `2026 Q3 8-K + 10-Q 披露日` 触发增量复核（验证 55 亿美元滑石粉诉讼费用与调整后 EPS 同比转负）。
- **硬性验收指标**：
  - 增量复核单次 Token `< 200K`（较全量 Squad 压降 **75%+**）；
  - 同一 `accession` 重复巡检 0 重复调用；非实质 8-K 0 误报；
  - 引文门控 100% 生效，无悬空引文；
  - 用户确认 `v1 → v2` 或保留异议后，历史版本与差异完整可追溯。

---

## 五、分期实施路线图

| 期次 | 交付内容 | 出口标准 |
| --- | --- | --- |
| **Phase B1（核心引擎与假设台账）** | `ThesisLedger` / `WatchTrigger` / `DeltaReviewRun` / `ChangeInboxItem` 数据模型与存储；一键从 `EarningsDossier` 提炼假设草案；Delta Stage 0（确定性 Promise-vs-Delivery + 旗标/Reverse-DCF 漂移 + Trigger Decimal 求值）+ Delta Stage 1（精简增量 Reviewer + 引文门控）；API 路由 + NVDA/META/JNJ Q2→Q3 真实回放验证 | 3 家跨行业真实 Q2→Q3 增量回放全通，单次 Token `< 200K`，单测全绿 |
| **Phase B2（SEC 监控调度与前端工作台）** | `FilingWatchSubscription` 后台轻量巡检器（接入 `main.py` lifespan，遵守 SEC 限速、去重、故障与静默分离）；前端 `/inbox` 变化收件箱页 + 主题页“假设台账 & 持续跟踪”视图 + `v1 → v2` 差异确认流；端到端上线验证 | 线上完整跑通“建档 → 监控/手动检查 → 变化收件箱 → 确认升级版本”闭环 |
