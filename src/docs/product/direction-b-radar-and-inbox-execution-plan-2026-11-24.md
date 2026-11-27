# Direction B（变化雷达 & 变化收件箱）+ P1-B 错峰调度 实施方案

- **日期**：2026-11-24
- **前置基线**：
  - **Direction A**（美股买方财报深研引擎）：已于 NVDA / META / JNJ 三行业实测闭环；
  - **V1 事件引擎**（`foresightnine-api:ca29276`，含 v6.1 P0 批量 Skeptic、平台并发闸门 `limit=2` + 429 长退避、6-K 兜底）：已在 15 个真实美股 + FPI 案例上通过平台模型（`glm-5.3-flash`）验收（15/15 通过，0 失败申报，0 限流缺口）。
- **关联文档**：
  - [整体产品定位](fusion-finance-ai-macro-plan-2026-11-11.md)；公司研究与雷达分工见[product-paradigm-a-engine-b-radar-2026-11-24.md](product-paradigm-a-engine-b-radar-2026-11-24.md)
  - 理论蓝图：[direction-b-continuous-thesis-tracking-plan-2026-11-24.md](direction-b-continuous-thesis-tracking-plan-2026-11-24.md)
  - V1 规格与 v6.1 实测：[v1-sec-events-insider-ownership-design-2026-11-24.md](v1-sec-events-insider-ownership-design-2026-11-24.md) · [v1-sec-events-eval-report-2026-11-24.md](v1-sec-events-eval-report-2026-11-24.md)
- **核心目标**：打通 [AGENTS.md](/home/ubuntu/Foresightnine/AGENTS.md) 首版可试用产品的第二半闭环——把 **V1 重大事件流** 与 **Direction A 深度财报底稿** 通过 **假设台账（Thesis Ledger）+ P1-B 错峰雷达调度 + 统一变化收件箱（Change Inbox）** 连接起来，回答：**“新信息出现后，我原来的判断是否需要修改？”**

---

## 一、总体架构：单一 SEC 分流器 + P1-B 错峰高危优先队列

为彻底避免重复轮询 SEC 与高并发 429 撞墙，全系统只维护**一套共享 SEC 调度分流器**，采用两阶段漏斗架构：

```
已启用订阅列表 (FilingWatchSubscription)
        │
        ▼  【阶段 1：全量 L1 确定性快扫（0 LLM，仅受共享 SEC 7-8 RPS 限制，单家 1-3s）】
  ┌─────────────────────────────────────────────────────────────────────┐
  │ 1. 拉取最新 submissions，与 subscription.seen_accessions 按通道去重  │
  │ 2. Item 级确定性双路分流：                                           │
  │    - 财报通道 (earnings)：10-Q / 10-K / 20-F（含 /A）、含 2.02 8-K、 │
  │      业绩 6-K                                                        │
  │    - V1 事件通道 (v1)：非 2.02 8-K（含混合 8-K 剥离 2.02）、Form 4、 │
  │      SC/SCHEDULE 13D/13G、非业绩 6-K                                 │
  │ 3. 无新实质候选（常态 >90%）→ 更新 last_checked_at，静默结束（0 Tok）│
  └─────────────────────────────────────────────────────────────────────┘
        │ 仅 has_new_candidates > 0 的公司进入队列
        ▼  【阶段 2：P1-B 高危优先队列 + 平台并发闸门（limit=2）错峰消费】
  ┌─────────────────────────────────────────────────────────────────────┐
  │ 优先级排序：                                                         │
  │   P0 法定高危：8-K 4.02 / 1.03 / 2.04 / 4.01 / 1.05                  │
  │   P1 财报增量：10-Q / 10-K / 20-F / 含 2.02 8-K / 业绩 6-K           │
  │   P2 重大治理/举牌：8-K 5.02 / 1.01 / 1.02 / 2.05 / 13D              │
  │   P3 常规信号：其他 8-K / Form 4 / 13G / 非业绩 6-K                  │
  ├─────────────────────────────────────────────────────────────────────┤
  │ • V1 通道：调用已上线 analyze_sec_events(...)，注入该主题活跃         │
  │   ThesisLedger 支柱与 R/Y 旗标（复用已预留参数）                     │
  │ • 财报通道：调用 2 阶段轻量 Delta 复核引擎（< 200K tok）             │
  │ • 全程经由 _platform_llm_concurrency_gate(2) + 429 长退避            │
  └─────────────────────────────────────────────────────────────────────┘
        │
        ▼
  统一 ChangeInboxItem（按 event_thread_key 折叠修正案，按支柱关联 ThesisClaim）
  - push_alert / challenges_thesis / thesis_broken → 生成未读收件箱卡片
  - log_only / no_material_change                  → 写入审计时间线，不弹红点
  - 抓取/模型故障                                  → 写入 health_status="degraded|error"，绝不伪装“无变化”
```

### 关键工程纪律（复用已验证底座）
1. **单通道独立幂等与失败重试**：`seen_accessions: dict[str, dict[str, str]]` 按 `accession -> {"earnings"?: status, "v1"?: status}` 记录；某通道失败仅记 `failed_accessions[acc][channel]` 且**绝不写入该通道 `seen_accessions`**，下次巡检自动只重试失败通道。
2. **短事务存储**：所有批量读写复用 `storage.py` 已落地的 `PostgresDocumentRepository.get_many / put_many`，不在 SEC / LLM I/O 期间持有数据库事务。
3. **不覆写已确认判断**：后台自动任务只能生成 `ChangeInboxItem` 与修订建议，**绝不静默修改**用户 `review_status="confirmed"` 的 `ThesisLedger`；仅在用户显式点击确认后升级至 `v(N+1)`。

---

## 二、核心数据模型（Pydantic `extra="forbid"` + Postgres 独立集合）

全部持久化到 PostgreSQL `research_state_documents`（新增 4 个独立 collection：`THESIS_LEDGERS`、`WATCH_SUBSCRIPTIONS`、`DELTA_REVIEW_RUNS`、`CHANGE_INBOX_ITEMS`）：

1. **`ThesisLedger` & `ThesisClaim` & `WatchTrigger`**（版本化假设台账）
   - 绑定 `(account_id, topic_id, cik, symbol)`，`version: int`（`1, 2, ...`），`parent_version: int | None`，`review_status: Literal["draft", "confirmed"]`，`base_dossier_id`。
   - 每条 `ThesisClaim` 包含：
     - `claim_id`, `pillar: Literal["growth", "margin", "cash_quality", "capital_allocation", "valuation", "risk"]`
     - `statement: str`, `stance: Literal["bull", "cautious", "bear", "neutral"]`
     - `watch_triggers: list[WatchTrigger]`（支持 `kind="metric_rule"` Decimal 确定性求值 + `kind="disclosure_rule"` 文本/事件触发）
     - `invalidation_condition: str`（初始直接承接 Direction A `bear_cases[].next_quarter_invalidation_trigger`）
     - `status: Literal["active", "strengthened", "weakened", "invalidated", "superseded"]`
     - `evidence_refs: list[str]`, `user_note: str | None`
   - **一键建档（低成本）**：从已锁定 `EarningsDossier` 的紧凑投影单次轻量抽取 3–5 条 `ThesisClaim` 草案（`< 25K` tokens），用户编辑后点击“确认判断”。

2. **`FilingWatchSubscription`**（公司监控订阅与健康状态）
   - `subscription_id`, `account_id`, `topic_id: str | None`（支持无主题纯自选股订阅，也支持绑定研究主题）, `cik`, `symbol`, `company_name`, `enabled: bool`
   - `watched_channels: list[Literal["v1_events", "earnings_delta"]]`（默认双开）
   - `seen_accessions: dict[str, dict[str, str]]`, `failed_accessions: dict[str, dict[str, str]]`
   - `check_interval_minutes: int = 60`, `last_checked_at`, `next_check_at`, `last_signal_at`
   - `health_status: Literal["healthy", "degraded", "error"]`, `last_error: str | None`

3. **`DeltaReviewRun` & `ThesisScorecardDiff`**（财报 2 阶段增量复核）
   - **Delta Stage 0（纯确定性 Python Decimal，0 LLM）**：
     1. 仅拉取新 `accession` 的 8-K EX-99.1 或 10-Q/10-K 分节与最新 XBRL 期间；
     2. **Promise vs. Delivery**：上期 `guidance_nongaap.new_guidance` 对撞本期实际营收/毛利率/EPS/CapEx，确定性输出 `Beat / Inline / Miss` 及精确差值；
     3. **旗标与 Reverse-DCF 漂移**：对比 R/Y/G 旗标状态迁移与 5 年隐含 FCF CAGR 漂移；
     4. **红线增量**：新公告为 10-Q/10-K 时自动跑 `sec_redline_diff`；
     5. **数值 Trigger 求值**：对 `ThesisLedger` 中所有 `metric_rule` 用 Decimal 确定性求值（`triggered / cleared / unchanged / awaiting_10q`）。
   - **Delta Stage 1（精简增量 Reviewer，1–2 次 LLM 调用，输入 `< 45K` tok）**：
     - 喂入：`ThesisLedger` + Stage 0 确定性差异包 + 新公告关键片段；
     - 输出 `ThesisScorecardDiff`（`overall_impact`、`claim_updates[]`、`new_variables[]`、`promise_delivery_summary`、`open_gaps[]`），所有逐字引文经 `research_llm_primitives.py` 同空间核验门控。

4. **`ChangeInboxItem`**（统一变化收件箱条目）
   - `inbox_item_id`, `account_id`, `subscription_id`, `topic_id: str | None`, `cik`, `symbol`, `company_name`
   - `source_channel: Literal["v1_events", "earnings_delta"]`
   - `event_thread_key: str`, `superseded_by_accession: str | None`（同事件修正案自动折叠，不刷屏）
   - `headline: str`, `impact_level: Literal["critical", "high", "medium", "low"]`, `direction: Literal["positive", "negative", "mixed", "neutral"]`
   - `affected_pillars: list[str]`, `linked_claim_ids: list[str]`（自动匹配 `ThesisLedger` 中同支柱活跃假设）
   - `signal_envelope: CompanySignalEnvelope | None`（V1 来源） / `scorecard_diff: ThesisScorecardDiff | None`（财报 Delta 来源）
   - `skeptic_dissent: str | None`（显式呈现反方保留意见）
   - `review_state: Literal["unread", "read", "accepted", "dismissed_with_note"]`, `user_decision_note: str | None`, `resulting_ledger_version: int | None`

---

## 三、V1 信号 × Direction A 假设台账联动设计

1. **上下文注入（零改动 V1 核心）**：
   - 调度器调用已上线的 `analyze_sec_events(...)` 时，若该订阅绑定了 `ThesisLedger`，自动把活跃 `claims` 摘要传入 `thesis_context`，把最新 `EarningsDossier` 的 R/Y 旗标传入 `active_red_yellow_flags`（这两个参数在 V1 Layer 2 中已预留并支持）。
2. **支柱自动映射入箱**：
   - V1 输出的 `CompanySignalEnvelope.affected_thesis_pillars` 自动匹配 `ThesisLedger.claims[].pillar`，生成 `linked_claim_ids`。
   - 仅 `disposition == "push_alert"`（或法定兜底触发）生成未读 `ChangeInboxItem`；`log_only` 写入公司时间线不弹红点；`suppress` 仅留审计日志。
3. **收件箱闭环动作**：
   - 有 `ThesisLedger`：`[接受修订 → 升级至 v(N+1)]` / `[编辑后接受]` / `[保留原判断（记异议）]` / `[标为已读]` / `[重跑完整买方底稿]`；
   - 无 `ThesisLedger`（纯自选股监控）：`[基于此事件发起深度研究]` / `[标为已读]`。

---

## 四、后端模块与 API 契约

### 1. 新增独立模块（不侵入现有 Direction A Squad）
- `src/apps/api/app/research_thesis_ledger.py`：台账模型、从锁定 `EarningsDossier` 轻量抽取草案、版本升级与 `v(N) → v(N+1)` Diff 计算。
- `src/apps/api/app/research_delta_review.py`：财报 2 阶段 Delta 复核引擎（Stage 0 确定性差异 + Stage 1 增量 Reviewer）。
- `src/apps/api/app/research_radar_scheduler.py`：单一 SEC 分流器 + P1-B 错峰高危优先队列 + 后台定时巡检器 + `ChangeInboxItem` 统一入箱路由。

### 2. 新增 `/v2/research/*` API 端点（`main.py` + OpenAPI/TS 契约同步）
- **假设台账**：
  - `GET /v2/research/topics/{topic_id}/thesis-ledger`（最新确认版 + 草案 + 版本历史）
  - `POST /v2/research/topics/{topic_id}/thesis-ledger/draft`（从锁定 `EarningsDossier` 一键生成草案）
  - `POST /v2/research/topics/{topic_id}/thesis-ledger/confirm`（确认/编辑并落盘为 `v(N+1)`）
- **雷达订阅与巡检**：
  - `GET /v2/research/radar/subscriptions` / `POST /v2/research/radar/subscriptions` / `PATCH /v2/research/radar/subscriptions/{subscription_id}` / `DELETE /v2/research/radar/subscriptions/{subscription_id}`
  - `POST /v2/research/radar/check-now`（202 异步触发单公司或全量订阅 P1-B 错峰巡检）
  - `GET /v2/research/radar/health`（返回在跟公司数、上次巡检时间、各订阅 `healthy / degraded / error` 汇总）
- **变化收件箱**：
  - `GET /v2/research/inbox`（支持按 `symbol`、`impact_level`、`review_state`、`source_channel` 筛选 + 未读计数）
  - `POST /v2/research/inbox/{inbox_item_id}/decide`（执行 `accept / edit_and_accept / keep_original / mark_read`，若接受且绑定台账则原子生成 `ThesisLedger v(N+1)`）

---

## 五、前端工作台设计（克制、30 秒决策、零内联样式）

1. **侧栏一级入口：`变化收件箱`（`/inbox`）**
   - 仅当存在未读实质变化时展示数字徽章。
   - 顶部 **监控健康条**：清晰展示“正在跟踪 N 家公司 · 上次巡检 X 分钟前 · SEC 通道正常”+ `立即巡检` 按钮；若存在 `degraded/error`，独立标红展示具体失败原因，**绝不与“无变化”混淆**。
   - **30 秒决策卡片流**：
     - 头部：Ticker + 事件类型/财报期 + 影响徽章（`CRITICAL / HIGH / MEDIUM`）+ 关联支柱标签；
     - 主体：一句话结论 + 关键量化数字 / 承诺兑现表 + **反方保留意见（`skeptic_dissent`）折叠条** + 可展开原文逐字引文（带校验状态）；
     - 底部动作栏：确认升级 `v(N+1)` / 保留原判断（填异议备注）/ 标为已读 / 跳转或发起 Direction A 完整底稿。
2. **`TopicPage` 轻量模块：`假设台账 & 雷达监控`**
   - 位于 `EarningsDossierView` 顶部快捷条/独立 Tab（不打断现有研报阅读流）：
     - 首轮底稿完成后显示一键引导条：“提炼核心假设与下季证伪阈值，开启 SEC 变化雷达”；
     - 展示可编辑 `ThesisClaim` 列表、`WatchTrigger`、版本时间线（`v1 → v2` Diff）以及订阅开关 + `立即检查 SEC 新公告`。

---

## 六、分期实施路线图与验收门控

| 阶段 | 交付内容 | 验收出口标准（均须跑真实平台模型 `glm-5.3-flash`） |
| --- | --- | --- |
| **Step B1：假设台账 + P1-B 错峰雷达 + V1 统一收件箱** | `research_thesis_ledger.py` + `research_radar_scheduler.py`（L1 全量快扫 + P1-B 高危优先队列 + V1 通道接入 + `ChangeInboxItem` 统一入箱与确认升级 `v(N+1)`）+ API 路由 + 后台定时巡检（`FORESIGHTNINE_RADAR_ENABLED` 开关） | 1. 从现有 NVDA / META / JNJ 锁定底稿一键生成 `ThesisLedger v1` 并确认；<br>2. 5 家公司并发跑 P1-B 雷达巡检：无新申报公司 `0 Token`，有新申报按高危优先错峰出队，`0` 429 缺口，重复巡检 `0` 重复调用；<br>3. SMCI 4.01 / UNH 1.05 历史回放准确生成 `ChangeInboxItem`、关联台账支柱并完成 `v1 → v2` 确认闭环 |
| **Step B2：财报 2 阶段 Delta 复核引擎 + 跨季回放** | `research_delta_review.py`（Delta Stage 0 确定性 Promise-vs-Delivery + 旗标/Reverse-DCF 漂移 + Decimal Trigger 求值；Delta Stage 1 精简增量 Reviewer + 引文门控）并接入单一分流器的 `earnings_delta` 通道 | NVDA / META / JNJ 真实 `as_of` Q2 → Q3 跨季回放全通；单次增量复核 `< 200K` tokens（较全量 Squad 压降 `>= 75%`）；引文核验 100%；8-K 无 10-Q 时诚实标明缺口 |
| **Step B3：前端 `/inbox` 与 `TopicPage` 台账/监控 UI + 上线** | 同步 OpenAPI/TS 契约；落地 `/inbox` 变化收件箱页（含监控健康条与 30 秒决策流）与 `TopicPage` 假设台账/雷达开关；前端 Vitest + 真实浏览器端到端验收 + 部署上线 | 线上真实账号跑通“深度研报 → 一键生成假设台账 → 开启雷达监控 → 巡检入箱 → 审阅 Skeptic/引文 → 确认升级 `v2`”全链路 |

---

## 七、明确暂缓项（不阻塞本轮交付）

- **v6.1 P1-A（快/慢双模型分级路由）**：待 Direction B 雷达积累真实多公司巡检成本数据后再做同条件对照评测；
- **v6.1 P1-C（HKEXnews / 台股 IR 等本土交易所补源）**：待美股核心闭环稳定后按市场需求单独立项扩展。
