# V1 窄垂类引擎（v5 方案）11 维度深度评估报告

- **日期**：2026-11-24
- **评估对象**：[v1-sec-events-insider-ownership-design-2026-11-24.md](./v1-sec-events-insider-ownership-design-2026-11-24.md)（v5）
- **核验范围**：交叉核查 `sec_edgar.py`、`sec_deep.py`、`fin_data/adapters/base.py`、`fin_data/policy.py`、`fin_data/ratelimit.py`、`research_sec.py`、`research_fin_tools.py`、`research_model_slots.py`、`research_earnings_squad.py`、`storage.py`、`market_cache.py`、`main.py` 等线上真实代码。

---

## 一、11 维度总览评分与核心结论

| 维度 | 等级 | 核心判断 | 最关键代码级/架构级缺口 |
| :--- | :--- | :--- | :--- |
| **1. 表现（Latency / UX）** | **C+** | 增量静默极快，但冷启动/高频事件走同步 REST 极易 504 | 走现有 `SOURCE_RATE_LIMITS` 仅 **1 req/s**（20–40 请求需 20–40s）+ 多批思考模型/钻取/Skeptic 可达 **90–200s**；`submissions()` 未翻页 `filings.files` |
| **2. 花费（Cost）** | **A-** | Layer 1 0-LLM + 批合并 + 全局不可变缓存，边际成本极低 | 全量 `Item 8.01` + 未预筛 FPI `6-K`（如 BABA 回购/TSM 月报/业绩 `6-K`）易打满 18 条上限；复杂批含 thinking tokens 实耗更接近 **25K–55K** 而非 `< 30K` |
| **3. 准确性（Factual/Parsing）** | **B+** | Form 4 六大暗坑、去壳切片、`EX-16.1`、全 `Decimal` 属机构级 | ① 2024-12-18+ `SCHEDULE 13D/G` 表名与 `xslSCHEDULE_13D_X01/` 前缀；② `submissions()` 丢弃 `items`；③ `ScaleAnchor` 缺 `as_of` 与跨币种校验；④ Form 4 缺价需豁免 `\$25K` 底线、区分 `D/I` 与部分无现金行权拆分 |
| **4. 可靠性（Reliability）** | **B-** | `failed_accessions` 原子防漏 + 法定高危 Fail-Safe 很扎实 | ① `SLOT_REQUIRES_NATIVE_TOOLS["deep_reasoner"] == False`，无原生 tool calling 时钻取失效；② `base.py:88-89` 限流不等待直接抛错；③ `storage.py:177-195` 读库剥离 `None` 键隐患 |
| **5. 观点可靠性（Judgment）** | **B+** | 去锚定 + 逻辑链前置 + 非对称 Skeptic 证伪设计先进 | 单条盲测 Skeptic 易误杀“单看无害、跨公告组合致命”的高危警报；Skeptic 应优先绑定 `bear_reviewer` 跨族槽位；转折词守卫需返回 `span` 并扩宽前后窗口 |
| **6. 观点有效性（Actionability）** | **B** | 16 类买方分类 + 量化影响 + 里程碑 + 待验证问题极贴合买方 | 缺合规的市值/股本标尺（需用 SEC 原生 `dei:EntityPublicFloat` / `SharesOutstanding`，严禁混入 BYOK 股价）；缺 `market_pricing_context` 声明；Form 4 职级需标准化 |
| **7. 对进一步分析的作用** | **A-** | `CompanySignalEnvelope` + `event_thread_key` + `open_questions` 衔接性强 | 与方向 B 旧文档 `FilingWatchSubscription`（跳过 `5.02`）存在规则冲突与重复巡检；需明确单一调度分流器与 `affected_thesis_pillars` 路由映射 |
| **8. 系统稳定性（Stability）** | **C** | **当前最大短板**：DB 连接、限流、内存与接口防护均存在直接隐患 | ① `PostgresDocumentRepository` 无连接池（每次读写新建 TCP 连接），N+1 缓存读写易引发连接风暴；② SEC 限流双面陷阱（1 req/s 锁死 vs 10 req/s 突发失败）；③ 512MB 容器预加载大 `EX-10.1` OOM 风险；④ 新 REST 端点缺幂等/限流/账本 |
| **9. 易维护性（Maintainability）** | **B-** | 三模块分层清晰且不扰动方向 A | `_AccountSlotModelRunner` 在 `main.py`、`_quotation_matches_source` 与 `_invoke_and_parse_with_repair` 在 7,719 行 `research_earnings_squad.py` 中，需先抽公共原语防循环依赖；解析器宜按表单拆分为子包 |
| **10. 易实时对齐性（Real-Time）** | **C+** | 适配“小时级/盘后变化雷达”，不具备盘中分钟级实时对齐 | `submissions` JSON 有数分钟至 ~15 分钟延迟；修正案（`8-K/A`、`4/A`、`13D/A`）到达时需提供基于 `event_thread_key` + `superseded_by_accession` 的折叠视图；`statement_lines()` 缺 `as_of` |
| **11. 规则变化是否需重新构建** | **C+** | **按当前 v5 直接编码：几乎每次改规则都需改代码、重发并击穿全库缓存** | 硬过滤阈值、16 类枚举、被动名单、转折词均未外置；全局共享 `prompt_version` 导致微调单表单规则就击穿全部 Layer 2 缓存 |

---

## 二、编码前必须解决的 P0 / P1 架构升级清单

### P0（编码前必须拍板，否则上线/压测必踩坑）
1. **API 异步化 + 工程防护**：`POST /v2/research/sec-events/analyze` 对齐 `main.py` 异步 `Operation`（`202` + 轮询），并接入 Idempotency-Key、Abuse 限流、Usage Ledger 与用户显式预算透传。
2. **SEC 共享 Pacing 限流 + 原始 `submissions` 补齐**：建立 4–5 req/s 可等待跨进程限流器（避开 1 req/s 锁死与 `base.py:88` 即时报错）；直接读取 `submissions` 原始 `items` 与 `filings.files` 翻页；兼容 2024-12-18+ `SCHEDULE 13D/G` 表名与 `xslSCHEDULE_13D_X01/` 前缀；补齐 FPI 业绩 `6-K` 分流与 Form 4 缺价豁免 `\$25K` 防尘底线。
3. **`deep_reasoner` 无原生工具兜底 + Skeptic 升级**：运行时检测 `supports_native_tools`，不支持时自动走“纯 JSON 两阶段本地钻取”；Skeptic 优先路由 `bear_reviewer` 槽位，并在跨公告组合警报时注入关联事件客观骨架防误杀。
4. **Postgres 批量读写 + 512MB 防 OOM + `None` 安全序列化**：缓存读写走 `postgres_unit_of_work` / 批量 `get_many`（严禁全表 `.items()`）；所有可空 Pydantic 字段强制 `= None` 防 `storage.py:177` 剥离报错；`EX-10.1` / `EX-4.*` 改为钻取时按 2–3 MB 上限懒加载。
5. **合规 `ScaleAnchor` + `as_of` 截断**：仅使用 SEC 原生 XBRL（支持 `us-gaap → ifrs-full` 回退、显式 `currency`、`filed_at <= event.filed_at` 截断、增补 `dei:EntityPublicFloat` / `EntityCommonStockSharesOutstanding`），严禁把 BYOK 股价写入全局客观缓存。

### P1（显著降低后续维护与调参重构成本）
6. **规则与缓存三段解耦（彻底解决维度 11“每次改规则都要重构”）**：
   - 规则外置至 `sec_events_rules.json`（含阈值、被动名单、6-K 噪音模式、转折词库、16 类到方向 B 6 大 `pillar` 映射）；
   - **Layer 1 全局缓存存“未过滤的原始解析事实”** → 调过滤阈值/名单时 **0 代码改动、0 SEC 请求、本地重筛**；
   - **Layer 2 原始语义输出与 0-LLM 后置门控分离** → 调转折词/不变量规则时 **0 LLM 成本重算**；
   - **按表单拆分 `prompt_version`**（`v1_8k / v1_f4 / v1_13dg / v1_6k`）且 `event_subtype` 采用 `str` + 规则包校验（保留 `other_material` 兜底），避免微调单表单击穿全库缓存或导致旧缓存反序列化崩溃。
7. **抽离公共原语与模块化拆分**：将 `_AccountSlotModelRunner`、`_quotation_matches_source`（升级返回 `span`）、`_invoke_and_parse_with_repair` 从巨石文件抽离复用；Layer 1 按 `fin_data/adapters/sec_events/{form8k,form4,form13dg,form6k,scale_anchor}.py` 拆分。
8. **Step 4 评测纪律**：遵循 `AGENTS.md` 划分调优集 vs 独立留出测试集，增设单轮基线对照，并验证 13D 在发行人 `submissions` 中的召回率（必要时辅以 `filing_search_cik` 补漏）。
