# V1 窄垂类引擎生产架构方案（v6）：美股重大 SEC 事件、内部人交易与举牌 Bounded Event Agent

- **日期**：2026-11-24
- **版本**：v6（基于 [v1-sec-events-boost-evaluation-2026-11-24.md](./v1-sec-events-boost-evaluation-2026-11-24.md) 11 维度代码级深评完成全部 P0/P1 架构加固，替代 v5）
- **上层范式**：遵循 [product-paradigm-a-engine-b-radar-2026-11-24.md](./product-paradigm-a-engine-b-radar-2026-11-24.md) —— V1 是面向**下游 Agent（方向 B 变化雷达 / 后续研究 Agent）**的后台 **2 号高保真引擎**，覆盖两次季报之间 **90 天空窗期**；保留完整结构化证据与语义分析（不做面向人类的短字数裁剪，人类 30 秒压缩由前台方向 B 负责）。
- **定位与实时性边界**：SEC `submissions` JSON 存在数分钟至约 15 分钟发布延迟，V1 定位为**小时级 / 盘后变化雷达引擎**，不承诺盘中分钟级行情预警；且客观层不包含事件前后股价反应与卖方一致预期（由信封显式声明）。

---

## 一、宏观顶层架构与 7 条核心工程原则

1. **统一公共信号信封（`CompanySignalEnvelope`）**：方向 A、V1 及未来 V2–V5 共享统一信号头部与事件演化字段：
   - 核心标识与演化：`signal_id`、`event_thread_key`、`amends_accession`、`superseded_by_accession`（修正案反向指针，供下游按线程折叠展示最新有效版本并保留修订 Diff 链）；
   - 结构化语义与传导：`event_subtype`（持久化为 `str` + 外置规则包校验，未知值降级 `other_material` 防旧缓存反序列化崩溃）、`affected_thesis_pillars`（映射至方向 B 6 大支柱 `growth | margin | cash_quality | capital_allocation | valuation | risk`）、`impact_scope`、`mentioned_counterparties`、`evidence_completeness`、`open_questions`、`quantified_financial_impact`、`upcoming_milestones`；
   - 语境诚实声明：`market_pricing_context`（默认 `price_reaction_unobserved`，明确告知下游 V1 仅基于 SEC 一手披露，未观测市场定价反应）。
2. **纯净静态客观层 vs 动态语境/个人逻辑层物理切分（防时间线与合规污染）**：
   - **全局不可变客观缓存（`owner_scoped=False`）**：仅包含在 `filed_at` 时刻起永久不变且来自 SEC 公共披露的事实（`fin_data/policy.py` 中 `sec_edgar` 默认 `owner_scoped=False, export=True`）——SEC 公告本身 + 披露时 `as_of` 截断的 SEC 原生 XBRL `ScaleAnchor` + 披露前 12 个月历史先例 `HistoricalPrecedent`。**严禁将 `user_byok`（Twelve Data / FMP）股价或私人数据写入全局客观缓存**。
   - **动态风险/个人逻辑叠加层（`owner_scoped=True` 或旁路共享缓存）**：仅当调用方传入随时间变化的 `active_red_yellow_flags`（来自方向 A）或用户私人 `ThesisLedger` / BYOK 数据时，走独立缓存键或按 `(account_id, topic_id)` 隔离。
3. **规则与缓存三段解耦（零重构调参，避免单点微调击穿全库）**：
   - **外置规则包（`sec_events_rules.json`）**：将硬过滤阈值、8-K 排除/高危清单、Form 4 防尘底线与职级映射、13D/G 被动机构白名单与变动阈值、FPI `6-K` 噪音/业绩分流模式、引文双侧转折词库、16 类 `event_subtype` 定义与 `pillar` 路由表全部外置为带版本号（`rules_version`）的 JSON 配置（启动时经 Pydantic 强校验）。
   - **段 1 · Layer 1 原始解析缓存（`sec_events_l1_raw`）**：按 `(cik, accession, parser_version)` 存储**未经过滤的原始确定性解析事实**。调过滤阈值、被动名单或 6-K 噪音规则时：**0 代码改动、0 SEC 请求、本地毫秒级重筛**。
   - **段 2 · Layer 2 原始语义缓存（`sec_events_l2_semantic`）**：按 `(cik, batch_and_context_hash, parser_version, active_form_prompt_versions, prompt_taxonomy_hash, model_fingerprint)` 存储模型原始研判与 Skeptic 原始复核结果：
     - `batch_and_context_hash` 同时哈希本批目标 `accessions` 与只读背景 `prior_context_accessions`（避免有无历史上下文碰撞）；
     - **按表单族拆分 `prompt_version`**（`v1_8k` / `v1_f4` / `v1_13dg` / `v1_6k`），批次键仅包含本批实际涉及表单族的版本号；
     - `prompt_taxonomy_hash` 仅哈希注入 Layer 2 提示词的 16 类 `event_subtype` 定义子集（修改 Layer 1 阈值/被动名单或 Layer 3 转折词库仍不击穿 Layer 2 缓存）；
     - 纳入 `parser_version` 与 `model_fingerprint`（`deep_reasoner` + `bear_reviewer` 解析后的协议/模型标识），防止解析器修复或受控实验切换模型时命中过期语义缓存。
   - **段 3 · 0-LLM 后置门控实时计算层**：不变量校验、引文 `span` + 双侧转折词守卫、`pillar` 路由在读出 Layer 2 缓存后本地确定性执行，修改后置规则 **0 LLM 成本即时生效**。
4. **Layer 1 / Layer 2 物理分层与单一 SEC 调度分流器**：
   - `sec_material_events`（Layer 1，纯确定性 0 LLM）注册进 `FinDataToolbox`，供任意 Agent 按 `categories` 点菜调用；
   - `analyze_sec_events`（Layer 2，`deep_reasoner` Bounded Event Agent）作为独立研判服务；
   - **与方向 B 统一单一 SEC 调度分流器（Single SEC Filing Dispatcher，Item 级双路分流）**：每个被跟踪公司只维护一套共享 `submissions` 游标，按通道记录完成状态（`seen_accessions[accession][channel]`，严禁单一字符串覆盖双路状态）——`10-Q / 10-K / 20-F / 8-K Item 2.02 / 业绩 6-K` 路由至方向 A/B 财报增量复核；非财报 `8-K`（含 `Item 5.02` 与全量 `Item 8.01`）/ `Form 4 / 4/A` / `SC 13D/G`（含 `SCHEDULE 13D/G`）/ 非业绩 `6-K` 路由至 V1。**混合 `8-K` 双路铁律**：同一份 `8-K` 若同时包含 `Item 2.02` 与其他实质非财报 Item（如财报日同时公告 `5.02` CFO 离职、`2.05` 重组、`4.02` 重述或 `8.01` 重大事项），**必须同时路由至财报通道与 V1 通道**（V1 剥离 `2.02` 后仅研判非 `2.02` Item），绝不因含 `2.02` 而互斥丢弃高危非财报条款。
5. **先独立验收，不扰动方向 A**：V1 独立落地并通过 15–20 个真实美股历史案例实测验收，暂不修改方向 A 已稳定跑通 NVDA / META / JNJ 的 6 阶段 Squad 流程（仅抽离共享底层原语并保留方向 A 向后兼容导出）。
6. **双时钟与游标原子防漏（`AGENTS.md` 硬纪律）**：
   - 严格区分 `event_date` 与 `filed_at`（`as_of` 截断、XBRL 标尺查询与去重一律以 `filed_at` 为准防未来数据泄漏，同时计算 `reporting_lag_days`）；
   - 两级增量拉取（先读 `submissions` 索引与 `seen_accessions` 做差集，再按需抓取）；抓取失败的 `accession` 记入 `failed_accessions` 且**绝不写入 `seen_accessions`**，下次巡检自动重试，绝不把采集故障伪装成“无变化”。
7. **常态极轻、遇大事自动深挖与独立证伪**：80% 简单/静默事件 1 轮直出（0 钻取、0 Skeptic 额外调用，`< 10K` Token）；20% 复杂/高危事件自动解锁最多 2 轮本地按需钻取 + 对 `push_alert` 条件触发物理隔离 Skeptic 独立证伪（含思考模型 thinking tokens 时，复杂批次实耗预期为 **`25K–55K` Token**）。

---

## 二、基础设施与稳定性底座（编码前必须先落地的工程加固）

针对线上代码核验暴露的限流、DB 连接、内存、序列化与接口超时短板，V1 编码前确立以下硬边界：

### 1. SEC 共享可等待 Pacing 限流器 + 原始 `submissions` 翻页（`sec_events/pacing.py`）
- **解决双面限流陷阱**：现有 `research_sec.py:324` 的 `reserve_sec_request(SOURCE_RATE_LIMITS)` 硬锁死 **1 req/s**（20–40 个请求需 20–40s），而 `fin_data/adapters/base.py:87-89` 调用进程内 `InProcessRateLimiter` 一旦耗尽令牌**不等待直接 `raise StopWith`**。
- **方案**：新建 SEC 专用 **4–5 req/s 可等待 Pacing 限流器**（低于 SEC 10 req/s Fair-Access 上限并留出 50% 安全余量），并作为唯一 SEC 速率门禁统一注入 V1 全部出站请求（含 `submissions`、`filings.files`、`companyconcept`、`EFTS` 单 CIK `filing_search_cik` 与 `EX-10/EX-4` 懒加载，严禁绕过或混用 `base.py` 默认非等待 `InProcessRateLimiter`）：
  - 单进程模式：平滑令牌桶 + `cancel_event.wait` / `deadline` 感知等待（超时或取消才中断，绝不因短暂 burst 直接抛错）；
  - 多进程/多容器部署：复用现有 `FORESIGHTNINE_REDIS_URL`（如未配置则回退独立短事务 Postgres advisory lock，绝不复用业务长事务）实现跨进程共享速率控制；
  - 遇到 SEC HTTP `429` 时严格遵守 `Retry-After`（默认退避并记入 `EventRunMetrics`）。
- **直读原始 `submissions` JSON 与按需翻页**：现有 `sec_edgar.py:851-876` 丢弃了 `items` 数组且未翻页 `filings.files`。V1 直接读取原始 `submissions` 的 `items` 列供 8-K 零网络预筛；当请求的 `start_date < earliest_recent_date` 时，按需顺序拉取 `filings.files` 中的历史分片（受单次最大分片数与时间预算保护，超出时写入 `open_gaps`）。

### 2. Postgres 批量读写 + 短事务纪律 + `None` 安全序列化 + 512MB 防 OOM
- **消除 N+1 连接风暴且严禁长事务跨越网络 I/O**：现有 `storage.py:368-376, 445, 537` 中 `PostgresDocumentRepository` 无连接池，每次独立 `get`/`put` 会新建 TCP 连接；同时 `storage.py:33-46, 560-568` 的 `postgres_unit_of_work` 会保持单个数据库事务打开（且使内部 `pg_advisory_xact_lock` 绑定至整个事务生命周期）。
  - 在 `storage.py` 为 `JsonDocumentRepository` / `PostgresDocumentRepository` 增补批量方法 `get_many(keys: Sequence[str])`（`WHERE collection = %s AND document_key = ANY(%s)`）与 `put_many(items)`；
  - **短事务边界铁律**：**严禁**将长达 20–200s 的 SEC 抓取或 LLM 推理包裹在单个 `postgres_unit_of_work` 内（防止 `idle in transaction` 连接枯竭、中途失败回滚已解析缓存、以及 advisory lock 锁死其他 worker）；改为“网络 I/O 前 1 次短事务 `get_many` → 无事务执行 SEC/LLM → 每完成 L1 或单个 L2 批次后 1 次毫秒级短事务 `put_many` 立即落盘”；
  - **严禁**对全局缓存集合调用全表 `.items()`。
- **`None` 安全反序列化契约**：现有 `storage.py:170`（`exclude_none=True`）与 `storage.py:177-195`（`_drop_legacy_null_fields`）在存取时会剥离所有值为 `None` 的字段键。
  - **硬规则**：V1 全部持久化 Pydantic 模型中，**所有可空字段（`T | None`）必须显式声明 `= None` 默认值**，并在单测中强制执行“经 `_drop_legacy_null_fields` 往返验证”。
- **512MB 容器内存硬顶**：
  - `EX-10.1` / `EX-4.*`（大合同/债券契约）在 Layer 1 **绝不预下载入内存**，仅保存附件角色、文件名与 URL 延迟句柄；
  - 仅在 Layer 2 明确发起钻取时按需懒加载，复用 `_CappedSink` 施加 **2–3 MB 单附件硬字节上限**；单批 `source_by_id` 驻留文本总量设硬上限（`<= 6 MB`），批次结束后释放大文本引用。

### 3. 公共原语抽离与模块化拆分（防循环依赖与方向 A 耦合）
- 将 `_AccountSlotModelRunner` 及其关联协议/配置（`SlotModelRequest`、`SlotModelResponse`、`SquadModelRunner`、`_CompositeCancelSignal` 及阶段超时/输出上限默认表）一并从 `main.py` / `research_earnings_squad.py` 抽离至独立模块 `src/apps/api/app/research_model_runner.py`（原模块保留兼容重导出，彻底断开对 7,719 行 `research_earnings_squad.py` 的重型/循环依赖）；
- 将引文核验与 JSON 校验修复原语抽离至 `src/apps/api/app/research_llm_primitives.py`：
  - **解耦 `_invoke_and_parse_with_repair`**：原实现（`research_earnings_squad.py:4928`）为紧耦合 `SquadExecutionLedger` 与 `OrgGraphEmitter` 的实例方法，抽离为无 Squad 依赖的通用函数 `invoke_and_parse_with_repair(...)`（通过可选回调钩子 `on_before_call / on_after_call / on_repair_attempt` 扩展），方向 A `EarningsSquad._invoke_and_parse_with_repair` 降为薄包装零回归兼容；
  - **升级 `find_quotation_span` 并消除归一化偏移漂移**：原 `_quotation_matches_source`（`research_earnings_squad.py:290`）回退至 `_norm_quote_text` / `_norm_quote_table_tolerant` 时会改变字符串长度，直接拿归一化偏移去切原文会错位。新原语返回结构化 `QuotationMatch(start, end, match_space, matched_text)`（`match_space: Literal["raw", "norm_text", "norm_table"]`），后续转折词守卫**必须在同一 `match_space` 文本中切片**（并保留 `_quotation_matches_source` 布尔包装供方向 A 零改动兼容）；
- Layer 1 按表单拆分为子包 `src/apps/api/app/fin_data/adapters/sec_events/`：
  - `__init__.py`（编排、游标防漏、L1 原始缓存与本地重筛入口）
  - `rules.py` + `sec_events_rules.json`（外置规则加载器与配置）
  - `pacing.py`（SEC 4–5 req/s 共享可等待限流 + 原始 `submissions` / `filings.files`）
  - `form8k.py`、`form4.py`、`form13dg.py`、`form6k.py`
  - `scale_anchor.py`（合规 SEC XBRL 标尺 + `as_of` 截断）

---

## 三、Layer 1：确定性解析、去壳与外置规则过滤底座（0 LLM）

> **三段解耦铁律**：以下各表单解析器均先将**未过滤的完整原始解析事实**持久化到 `sec_events_l1_raw`，再套用 `sec_events_rules.json` 执行确定性预筛与分流。

### 1. 非财报 8-K 解析器（`sec_events/form8k.py`）
- **基于原始 `submissions.items` 的零网络预筛与混合 8-K 拆分（规则外置）**：
  - 纯 `Item 2.02`（含 `2.02 + 9.01` 或可配置伴随财报演示的 `2.02 + 7.01`）标记 `routed_to_earnings_engine`（交由方向 A/B 财报引擎，V1 不重复研判）；
  - **混合 `8-K` 双路保底**：若 `8-K` 同时含 `Item 2.02` 与其他实质非财报 Item（如 `5.02 / 2.05 / 4.02 / 1.01 / 8.01`），仅剥离 `2.02`（交财报通道）并保留其余非 `2.02` Item 送 V1 研判（绝不整份丢弃）；
  - 纯法定低价值噪音（纯 `5.07` 股东投票、纯 `5.03` 例行章程、`3.02 / 3.03`、无主体条款的 `9.01`）标记 `low_materiality_filtered`；
  - 其余全部 8-K——**包括全部 `Item 8.01` 与 `Item 5.02`**——全量保留送 Layer 2（零漏报）。
- **结构化去壳（绝不暴力头部截断）**：以首个 `Item \d\.\d{2}` 为起点、`SIGNATURES` 为终点剥离 Cover Page 与签字页（立砍 40%–65% 废话，0 信息损失），按 `Item` 独立切片为 `dict[item_code, text]`（剔除纯 `9.01`）。
- **附件角色感知与差异化懒加载**：
  - `ex16_auditor_letter`（`Item 4.01` 换审计师必抓 **`EX-16.1`**）：前任审计师确认函（通常 1 页），100% 全文直传；
  - `ex99_press_release`（`EX-99.1 / 99.2`）：复用 `sec_deep.py` 保表格提取器 `strip_html_preserving_tables`，保留首 2 段 + 表格 + 含金额/百分比/关键事件词的高密度段，完整原文入 `source_by_id`；
  - `ex10_material_contract` / `ex4_debt_indenture`（百页合同/债券契约）：Layer 1 仅注册延迟句柄（文件名、角色标签、URL，**不预下载**），供 Layer 2 在主文出现交叉引用且缺失核心条款时按 2–3 MB 上限懒加载定点钻取。

### 2. Form 4 / 4/A XML 解析器（`sec_events/form4.py`）
- **原始 XML 直取**：剥离 `xslF345X05/` 前缀直取 `.xml`，完整规避实战暗坑：
  1. 多 `<reportingOwner>` 1-to-N 全量解析、独立角色标志与**职级标准化**（`officer_tier`: `ceo_cfo_chair | c_suite_evp | vp_other_officer | director | ten_pct_owner`，规则外置）；
  2. Form `4/A` 标记 `is_amendment=True` 与 `amends_accession`，防与原件重复加总；
  3. `<footnoteId>` → `<footnote>` 全量挂载回每笔交易；
  4. Table I / II 分离，支持**完整与部分无现金行权拆分**：不仅识别同日同股数 `M + S`，还识别同日 `M` 股数 `> S` 股数（或 `S` 覆盖行权/税负成本），拆分标注 `cashless_exercise_shares` 与 `discretionary_net_sale_shares`（并保留原件明细）；
  5. `10b5-1` 双源校验：`<aff10b5One>` 官方布尔标签 + 关联脚注正则兜底；
  6. `pricePerShare` 缺失绝不填 `0`，显式标 `price_status="missing_or_footnote_only"`，**并强制豁免 `\$25,000` 防尘底线**直通 Layer 2 读脚注；持股变动% 始终用**同份 XML 内同口径 `shares / (post_shares + shares)`**（安全处理 `post_shares == 0` 100% 清仓边界，免疫历史拆股失真）。
- **聚合与过滤（全 `Decimal`，规则外置）**：
  - 剔除非 `P/S` 交易码（`A/M/F/G/J/C`）与确认 `10b5-1` 计划交易；
  - **按 `(reporting_owner_cik, transaction_date, transaction_code, is_10b5_1, direct_or_indirect)` 聚合当日拆单总额**（严格区分 Direct `D` 与 Indirect `I` 持股实体，避免混算），再对有有效价格的聚合记录套用可配置防尘底线（默认 `\$25,000`）；
  - 计算 14 天集群聚合（`cluster_insiders`）与 **12 个月个人行为基线（`behavioral_baseline`）**。

### 3. Schedule 13D / 13G 解析器（`sec_events/form13dg.py`）
- **2024-12-18+ 新旧表单全兼容**：
  - 表单匹配同时兼容历史 `SC 13D / SC 13D/A / SC 13G / SC 13G/A` 与 2024-12-18+ 新表名 **`SCHEDULE 13D` / `SCHEDULE 13D/A` / `SCHEDULE 13G` / `SCHEDULE 13G/A`**；
  - 剥离 `xslSCHEDULE_13D_X01/`、`xslSCHEDULE_13G_X01/`（正则 `xslSCHEDULE_13[DG]_X\d+/`）前缀直取结构化 XML，提取申报人、股数、`percentOfClass`、`typeOfReportingPerson`、Item 4 文本、Item 6 现金结算衍生品/TRS、修正案 `amends_accession`；历史 HTML 表单保守降级解析。
- **发行人 `submissions` 召回补漏**：实测验证 13D 在发行人 `submissions` 中的覆盖度；如发现发行人索引缺失第三方举牌文件，按需辅以默认 policy 已放行的严格单 CIK `filing_search_cik`（`fin_data/policy.py:217`）补漏。
- **降噪与身份切换（规则外置）**：结合 `typeOfReportingPerson`（`IA/IC/BK`）+ 持股变动幅度（默认 `< 1.0 pp`）+ 外置被动指数机构名单联合降噪；自动识别 **`13G → 13D` 身份切换**。

### 4. 外资美股 FPI `6-K` 解析器（`sec_events/form6k.py`）
- 自动识别外资发行人（存在 `20-F` / `6-K` 归档历史），显式标注 `fpi_exempt_from_form4=True`，避免下游误报“0 内部人交易”；
- **可配置预筛与业绩分流（防打满 18 条上限）**：
  - **业绩分流**：依据外置规则包识别季度/年度业绩发布 `6-K`（如标题/首段命中季报业绩、财务结果新闻稿模式），标记 `routed_to_earnings_engine` 交由方向 A/B 财报引擎，避免与 V1 重复处理；
  - **高频例行噪音预筛**：对 TSM 例行月度营收快报（可选配置保留或折叠）、BABA 等高频常规每日回购披露（`Next Day Disclosure Return`）、例行月度股本变动表（`Monthly Return`）等按外置正则模板标记 `low_materiality_filtered`（完整原文仍在 L1 缓存中，改规则可 0 请求重筛）；
  - 其余实质 `6-K` 解析主文与 `EX-99.*`（保表格提取）送 Layer 2。

### 5. 去锚定 + 合规 `ScaleAnchor`（`sec_events/scale_anchor.py`）+ 静态三维语境
- Layer 1 只传客观事实，**严禁向 Layer 2 预传主观 `severity` 标签**（防确认偏差）；
- **合规 `ScaleAnchor`（纯 SEC 原生 XBRL，严禁混入 BYOK 股价）**：
  - **严格 `as_of` 截断**：所有 XBRL fact 强制过滤 `filed_at <= event.filed_at`（解决 `sec_edgar.py:942` `statement_lines()` 无 `as_of` 的未来数据泄漏隐患）；
  - **双会计准则回退与币种显式校验**：自动按 `us-gaap → ifrs-full` 回退提取 `latest_ttm_revenue`、`latest_net_income`、`latest_total_assets`，显式携带 `taxonomy` 与 `currency`（如 `USD` / `TWD` / `EUR` / `CNY` / `DKK`）；计算 Materiality Ratio 时如事件金额币种与 `ScaleAnchor.currency` 不一致，显式标 `currency_mismatch` 而非硬算；
  - **SEC 原生股本与公众流通市值标尺（独立 `dei` 分类表 + FPI ADR 防错算）**：现有 `sec_edgar.py:945` `statement_lines()` 仅接受 `us-gaap`/`ifrs-full`，因此 `dei:EntityPublicFloat`（最近 10-K/20-F 封面公众流通股市值）与 `dei:EntityCommonStockSharesOutstanding`（最新流通股本）通过直拉 `companyconcept/CIK{cik}/dei/{tag}.json` 获取并同样执行 `filed_at <= event.filed_at` 截断；显式附带 `public_float_as_of_date`、`public_float_currency`、`shares_outstanding_as_of_date`（`dei` 缺失时优雅降级为 `None` 并写入 `scale_anchor_gaps`）。对 FPI（`fpi_exempt_from_form4=True`），因 `20-F` 股本多为普通股而美股交易/13D 常为 ADS/ADR（如 1:5 / 1:8），显式标记 `share_unit_basis="ordinary_vs_adr_unverified"`，**禁止在未核验 ADR 折算比例时将 ADS 股数直接除以 `dei` 普通股总股本**。
- 另附带两项静态语境：① **事件时点语境**（距最近一次 `10-Q/10-K/20-F/8-K 2.02` 披露天数）；② **12 个月历史先例频次**（同类 8-K Item 与高管交易历史基线）。

---

## 四、Layer 2：Bounded Event Agent 与非对称证伪门控（`research_event_analyst.py`）

### 1. 上下文管理：时间簇聚类分批 + 能力自适应有界钻取（硬上限 2 轮）
- **时间簇聚类分批**：按 **14 天时间簇 / 关联主体** 聚类，单批硬预算 `<= 24,000` chars；若拆分多批，后续批次自动继承前批单行裁决摘要（绝不拆散跨公告因果链）；`recent_context_events` 强制单行极简骨架（最多 10 条）物理隔离在 `READ_ONLY_PRIOR_CONTEXT_DO_NOT_RE_ALERT`。
- **双模式有界按需钻取（解决 `SLOT_REQUIRES_NATIVE_TOOLS["deep_reasoner"] == False` 断路隐患）**：
  - 仅提供 3 个本地只读工具（`outline_source` / `search_source` / `read_sources`，0 外部网络请求；对未加载的 `ex10`/`ex4` 延迟句柄在首次钻取时按 2–3 MB 硬上限懒加载）；
  - 运行时检查 `ResolvedModelSlot.native_tools_supported`：
    - **模式 A（`native_tools_supported == True`）**：原生本地 tool calling，简单事件第 1 轮直出最终 JSON（0 工具调用），复杂事件最多 2 轮钻取；
    - **模式 B（`native_tools_supported in (False, None)` 自动兜底）**：**纯 JSON 两阶段本地钻取协议**——第 1 轮模型返回 `EventAnalysisBatch` 或仅输出 `drilldown_requests`（最多 3 条 `{source_id, mode, query?, offset?, limit?}`），服务端本地同步执行并回填命中片段（1,500 字窗口），第 2 轮模型产出最终 `EventAnalysisBatch` JSON。
  - 严禁通读 `ex4_debt_indenture` 常规债券契约；仅保留命中片段进入后续推理。

### 2. 逻辑链前置 + 16 项买方细粒度分类 + 结构化提炼槽
- 外层 `EventAnalysisBatch` **最前面强制输出 `cluster_reasoning_chain`**（利用自回归顺序，先纵览全批 + 30 天背景完成全局因果串联，再逐条输出 `events[]`）；
- 单条事件映射到外置规则包定义的 16 项买方细粒度分类树（含 2024 SEC CorpFin `1.05` vs `8.01` 网安区分与 `other_material` 兜底），并自动映射 `affected_thesis_pillars`，强制结构化提炼：
  - `skeptic_counter_check`（排在 `disposition` 之前）
  - `evidence_completeness`（`complete | preliminary_pending_amendment | insufficient_in_excerpt`）+ `open_questions`
  - `quantified_financial_impact`（金额/区间/币种/现金或非现金/影响期间）+ `upcoming_milestones`（交割日/生效日/预计 8-K/A 窗口）
  - `market_pricing_context`（默认 `price_reaction_unobserved`）

### 3. 条件触发的独立 Skeptic 复核（跨族槽位 + 组合防误杀 + 非对称裁决）
- 对 `suppress / log_only` 事件：**不触发**（0 额外成本）；
- **仅对拟推送的 `push_alert` 事件（90 天通常仅 0–2 条）**：条件触发 1 次物理隔离 Skeptic 复核：
  - **槽位路由升级**：优先绑定 **`bear_reviewer`** 槽位（复用平台既有跨族空头审校配置 `cross_family_bear_review`，打破同模型自证偏差），未配置时才回退 `deep_reasoner`；
  - **跨公告组合防误杀**：继续**严格屏蔽 Primary 推理过程**，但在涉及多条关联公告的组合警报（如“CFO 离职 + 换审计师 + 延迟发报”）时，向 Skeptic 注入该时间簇内**关联事件的 Layer 1 客观骨架**，防止单条盲测误杀“单看无害、组合致命”的高危信号；
  - **非对称裁决矩阵**：
    1. **硬原文反证（Skeptic 胜 → 强制降级 `log_only / suppress`）**：Skeptic 提供经服务端 `find_quotation_span` 核验的逐字反证引文（如“预计无实质财务影响”、“无会计分歧”、“前期已披露交易的例行交割”），且主分析漏看该段；
    2. **法定高危豁免（Primary 胜）**：`4.02` 重述、`1.03` 破产、`2.04` 违约加速，Skeptic **无权降级为 `suppress`**，最多附加 `skeptic_dissent`；
    3. **主观判断分歧（保留双边观点）**：Skeptic 无硬原文反证 → 保留信号并将 `skeptic_dissent` 原样附在信封中，交由下游 Agent（方向 B）结合用户假设权衡。

### 4. 服务端 0-LLM 后置确定性门控（三段解耦之段 3，0 LLM 成本可重算）
1. **法定与方向不变量校验（Invariant Checker）**：锁定 Layer 1 `Decimal` `key_facts`；强校验 `4.02/1.03/2.04` 不可标 `suppress/low`、Form 4 方向与分类一致、`is_cashless_exercise`（无额外自主净减持时）非 `critical`、纯 `13G` 非 `activist_13d`；
2. **引文 `span` 核验 + 同空间双侧转折/例外词守卫**：使用抽离升级的 `find_quotation_span` 返回 `QuotationMatch(start, end, match_space, matched_text)`，在命中的**同一 `match_space`（`raw | norm_text | norm_table`）文本**中扫描**前置 80 字符 + 后置 200 字符**双侧窗口内的外置转折/豁免词库（`however / except / unless / subject to / no disagreement / not expected to have a material / previously disclosed` 等），彻底消除归一化索引漂移；命中时自动附加 `quote_context_warning` 与上下文片段；
3. **1-to-1 覆盖率防漏条校验**：漏条自动触发 1 次定向补判；
4. **法定高危 Fail-Safe 硬兜底**：LLM 故障/超时/预算耗尽时，`4.02 / 1.03 / 2.04` 确定性强制产出 `push_alert`（`fail_safe_triggered=True`）。

---

## 五、API 契约、异步化与工程防护（已全部对齐 `main.py`）

1. **双通道暴露与异步防 504**：
   - `FinDataToolbox` 注册 `sec_material_events`（Layer 1，纯确定性 0 LLM，供任意 Agent 按 `categories` 调用）；
   - `POST /v2/research/sec-events/scan`（Layer 1 确定性扫描，同步返回，受 `deadline` 保护，超时部分完成时返回 `partial` + `open_gaps`）；
   - **`POST /v2/research/sec-events/analyze`**（Layer 1 + Layer 2 `deep_reasoner` + 条件 `bear_reviewer` Skeptic）：因冷启动与复杂批思考模型可达 90–200s，**强制对齐 `main.py` 异步 `Operation` 模式（返回 `202 Accepted` + `operation_id`，通过现有 Operation 状态轮询/事件流获取结果与取消）**，彻底消除同步 REST 504 超时。
2. **完整生产防护接入**：
   - `Idempotency-Key` 幂等去重（防并发重复外呼与重复计费）；
   - Abuse 限流（接入现有账号/全局配额校验）；
   - Usage Ledger 账本计量（如实记录模型标识、调用次数、输入/输出/thinking Token、SEC 请求数、耗时与费用）；
   - **预算遵循 `AGENTS.md` 默认研究入口原则**：请求省略或传空值表示未设置运行预算（不自动强加额度）；用户显式传入的预算（包括 `0`）透传并严格执行，耗尽时交付已完成批次、触发法定高危 Fail-Safe 并将未覆盖项记入 `open_gaps`。
3. **高频爆发软上限与诚实缺口**：单次扫描最多处理 **3 个时间簇批次 / 18 条候选事件**；超限时按 `法定高危项 > 最新披露时间` 优先保留，未覆盖 `accession` 明确记入 `open_gaps`（绝不静默丢弃）。
4. **修正案折叠视图**：查询与返回结构支持按 `event_thread_key` + `superseded_by_accession` 折叠修正案（`8-K/A`、`4/A`、`13D/A`、`6-K/A`），默认呈现最新有效裁决并保留完整历史修订链。

---

## 六、实施步骤与受控实证验收（对齐 `AGENTS.md` 实验纪律）

| 步骤 | 交付文件 | 核心验收标准 |
| --- | --- | --- |
| **Step 0：公共原语抽离与存储扩展** | `src/apps/api/app/research_model_runner.py`<br>`src/apps/api/app/research_llm_primitives.py`<br>`src/apps/api/app/storage.py`（增补 `get_many` / `put_many`） | `_AccountSlotModelRunner` + `SlotModelRequest`/`SlotModelResponse`/`SquadModelRunner`/`_CompositeCancelSignal` 抽离；无 Squad 耦合的 `invoke_and_parse_with_repair` 与返回 `QuotationMatch` 的 `find_quotation_span` 抽离；方向 A 向后兼容重导出且现有全量单测零回归；`get_many`/`put_many` 短事务批量读写单测通过 |
| **Step 1：公共信号底座 + Layer 1 子包** | `src/apps/api/app/research_signals.py`<br>`src/apps/api/app/fin_data/adapters/sec_events/{__init__,rules,pacing,form8k,form4,form13dg,form6k,scale_anchor}.py`<br>`src/apps/api/app/fin_data/adapters/sec_events/sec_events_rules.json` | `CompanySignalEnvelope`（全部可空字段 `= None` + `market_pricing_context` + `affected_thesis_pillars` + `superseded_by_accession`）；4–5 req/s 共享可等待限流 + 原始 `submissions.items` 与 `filings.files`；8-K/Form 4（含缺价豁免、D/I 聚合、部分无现金行权、职级标准化）/13D-G（含 `SCHEDULE 13D/G` 与 `xslSCHEDULE_13[DG]_X\d+/`）/6-K（业绩分流与噪音预筛）；合规 `ScaleAnchor`（`us-gaap→ifrs-full` + `dei` + `as_of` 截断）；L1 原始缓存与本地重筛 |
| **Step 2：Layer 2 Bounded Event Agent** | `src/apps/api/app/research_event_analyst.py` | 时间簇聚类分批、双模式有界 2 轮本地钻取（native tools + 纯 JSON 两阶段兜底）+ 2–3 MB `EX-10/EX-4` 懒加载、`cluster_reasoning_chain` 前置、按表单拆分的 `prompt_version` 缓存、`bear_reviewer` 优先跨族 Skeptic + 关联事件骨架防误杀、四重 0-LLM 后置门控（含双侧转折词守卫）、Fail-Safe 硬兜底 |
| **Step 3：异步 API、Toolbox 接入与全量离线单测** | `src/apps/api/app/research_fin_tools.py`（注册 `sec_material_events`）<br>`src/apps/api/app/main.py`（`/scan` + 异步 `202` `/analyze` + 幂等/限流/账本）<br>`src/apps/api/tests/test_sec_events_and_analyst.py` | 离线单测覆盖：全部表单暗坑、`_drop_legacy_null_fields` 往返反序列化、无 native tools 两阶段钻取、非对称 Skeptic 与跨公告防误杀、三段缓存解耦与规则热重载、四重门控与 Fail-Safe、异步 `202` 端点与全量后端回归全绿 |
| **Step 4：15–20 个真实美股历史案例受控实测** | `src/docs/product/v1-sec-events-eval-report-2026-11-24.md` | 遵循 `AGENTS.md` 实验纪律：<br>1. 划分 **调优集（Dev Set，10–12 例）** 与 **独立留出测试集（Holdout Set，5–8 例，评测前冻结不针对其调参）**，覆盖本土 + FPI `6-K` 及 5 类陷阱（① 关键条款藏在 `EX-10.1`；② `4.01`+`EX-16.1` 有/无分歧；③ 含转折否定句的 `8.01`；④ 完整/部分 `M+S` 无现金行权 vs 首次非计划高管买入；⑤ 连续 `SCHEDULE 13D→13D/A` 含发行人 `submissions` 召回校验）；<br>2. 固定任务集、快照、`as_of` 与参数，增设 **单轮直出基线（Single-Pass Baseline）** 同条件对照，完整记录模型标识、Token（含 thinking tokens）、耗时、费用、召回率与误报抑制率；<br>3. 目标验收线：已知重大事件 **100% 召回**、噪音压降 **>= 80%**、简单公司 `< 10K` Token / 复杂批次 `25K–55K` Token、0 漏扫、0 幻觉引文 |
