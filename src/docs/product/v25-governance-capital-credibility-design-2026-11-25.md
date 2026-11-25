# V2.5 管理层激励、真实资本配置与跨季信用审计 — 深度架构与实施方案（v2 定稿）

更新日期：2026 年 11 月 25 日  
状态：✅ `/grill-me` 决策已全部收敛定稿，可执行实施  
前置基线：`master @ 7179cea`（方向 A 美股季报深研 Squad、V1 SEC 重大事件引擎、方向 B 假设台账与 P1-B 雷达、V2 同业对齐与相对估值引擎均已上线验收）  
上游总纲：[product-paradigm-a-engine-b-radar-2026-11-24.md](./product-paradigm-a-engine-b-radar-2026-11-24.md)

---

## 1. 产品目标与竞品选型定位

V2.5 解决买方基本面研究中最容易被常规财报指标掩盖的三大盲区：
1. **“假回购真稀释”幻觉**：公司花数十亿美元回购，究竟是真正在缩减总股本，还是仅仅在给高管巨额 SBC（股权激励）兜底？
2. **管理层激励错位**：高管年度/长期薪酬考核（`DEF 14A`）究竟挂钩的是资本回报（ROIC/FCF），还是容易注水的 Adjusted EBITDA / 营收规模？今年是否悄悄降级了考核指标？
3. **指引兑现信用缺失**：管理层过去 4–8 季给的业绩指引究竟是保守超预期（沙袋效应）、精准兑现，还是屡次画饼？

### 前沿产品与竞品对标选型

| 竞品 / 范式 | 核心长板 | 核心短板 | ForesightNine V2.5 选型与超越点 |
| --- | --- | --- | --- |
| **Calcbench** | 附注级 XBRL 解析（SBC、代扣税现金流、未确认 SBC 悬顶、回购股数） | 偏原始会计查询，缺 30 秒决策综合与假设联动 | **吸收附注级 XBRL 指标**：100% 免费官方 SEC 一手源，0-LLM `Decimal` 计算 SBC 代扣税、未确认 SBC 悬顶、3–5 年复权净稀释 |
| **AlphaSense / Hebbia** | 跨代理书 CD&A 语义检索与治理矩阵抽取 | 全文 RAG + 多轮 LLM，成本高、延迟长、缺乏确定性对账 | **结构化 iXBRL 替代 80% LLM**：Layer 1 直接解析 SEC 2023+ 强制披露的 `DEF 14A *_htm.xml`（仅 65–250KB）0-LLM 提取 Pay-vs-Performance、考核指标集与新规治理旗标；仅 Layer 2 按需对 `<=20KB` CD&A 锚点切片做 1 次思考模型深挖 |
| **Fiscal.ai / Koyfin** | SBC 占比、稀释股本、回购可视化 | 未戳穿回购对冲稀释真相，未与高管考核锚对撞 | **资本配置真相卡**：输出 `True Net Shareholder Yield`、回购对冲效率与 3–5 年真实净稀释，与 `DEF 14A` 考核锚同屏对撞 |
| **Quartr** | 业绩会与指引原文追踪 | 缺乏自动化跨季“承诺-兑现”确定性台账 | **双轨信用台账**：温路径直取方向 A 落盘指引；冷启动按需 1 次批量回溯近 6 季 `8-K Ex-99.1` + 0-LLM 严格同口径对账 |

---

## 2. 现网代码核查与前置底座修复（Step V2.5-0）

实施 V2.5 主体前，须同步修复 5 处经代码实证的底座隐患：

| # | 现网位置 | 实证问题 | 修复方案 |
| --- | --- | --- | --- |
| 1 | `research_delta_review.py:225` / `main.py:8191` | 读取不存在的 `guidance_nongaap.new_guidance`（真实字段为 `guidance_evaluations: list[GuidanceMetricEvaluation]`）；`run_p1b_radar_sweep` 尚未传 `earnings_delta_fn` | 抽共享 helper 从 `guidance_evaluations` 提取指引与实际值，供支柱 3 温路径与 `research_delta_review.py` 共用 |
| 2 | `research_peer_compare.py:512-521` / `storage.py:518` | `cache_only=True` 未校验 `projection_version`，且缺 `cik` 时调用 `.items()` 全表 `fetchall()` 存在 OOM 风险 | 增加 `min_projection_version` 校验（旧版返回 `stale_projection` 而非伪造 `\$0`）；禁止 `.items()` 全表扫描，严格按 `cik10` 主键点查 |
| 3 | `main.py:3770` / `research_fin_tools.py:1681` | `FinDataToolbox` 传的是 `SEC_EVENTS_L1_RAW`，未接入 `PEER_COMPANY_SNAPSHOTS`，导致 Squad 与 V2/V2.5 缓存割裂 | 显式注入 `peer_snapshot_cache=PEER_COMPANY_SNAPSHOTS` 与 `def14a_snapshot_cache=DEF14A_FACT_SNAPSHOTS`，打通四端共享缓存 |
| 4 | `sec_events/pacing.py:577-610` | `fetch_company_submissions` 无短 TTL 缓存，单次审计若 V2 快照与 DEF 14A 各自调用会产生 2 次冗余 SEC 外呼 | 单次审计只拉 1 次 `submissions` 向下透传，并加 15–30 分钟 Redis/内存短 TTL 缓存，确保热点响应 `<80ms` |
| 5 | `main.py:7385-7399` / `main.py:5396` | `_resolve_sec_events_runtime` 仅认标准槽位 `("deep_reasoner", "bear_reviewer")`；`_platform_llm_concurrency_gate()` 无参 | 复用 `slot="deep_reasoner"` + 独立 `stage`（`v25_governance_cda_audit` / `v25_guidance_backfill`），配 `max_output_tokens=16384`、`timeout >= 240s` 及无参并发闸门 |

---

## 3. 三大核心支柱设计（全 Decimal + 严格 `as_of`）

### 3.1 支柱 1：3–5 年真实资本配置与 SBC 净稀释审计（0 LLM）

- **快照升级（`projection_version: 2 -> 3`）**：
  - 将 `sec_edgar.py` 已有 `share_repurchases`、`dividends_paid` 加入 `_V2_PROJECTION_LINES`；
  - 在 `STANDARD_LINES` 新增 4 个通用 US-GAAP 科目：
    1. `sbc_tax_withholding`（cashflow, flow, money）：`PaymentsRelatedToTaxWithholdingForShareBasedCompensation`
    2. `unrecognized_sbc`（footnote, instant, money）：`EmployeeServiceShareBasedCompensationNonvestedAwardsTotalCompensationCostNotYetRecognized`
    3. `repurchased_shares`（equity, flow, shares）：`StockRepurchasedAndRetiredDuringPeriodShares` / `StockRepurchasedDuringPeriodShares` / `TreasuryStockSharesAcquired`
    4. `rsu_grants_shares`（footnote, flow, shares）：`ShareBasedCompensationArrangementByShareBasedPaymentAwardEquityInstrumentsOtherThanOptionsGrantsInPeriod`
- **确定性口径防线**：
  - **任意比例拆股链式复权**：直接遍历 `PeerCompanySnapshot.facts_by_line["shares_diluted"]` 原始 `(accn, filed, start, end, val)` 行，按 `(filed, accn)` 从最新 10-K 倒推重叠财年比值 `val_newer / val_older`（`|ratio - 1| > 0.02` 即链式复权），兼容 10:1、3:2 与股票股利；单年复权股本激增 `>10%` 标 `possible_ma_equity_issuance`。
  - **现金流符号归一化**：对 `share_repurchases` / `dividends_paid` / `sbc_tax_withholding` 统一取 `abs()`（兼容 US-GAAP 正流出与 IFRS 负流出）。
  - **市值与金融股门控**：`true_net_shareholder_yield_pct = (Buybacks + Dividends - SBC) / MarketCap` 与 `unrecognized_sbc_pct_of_mcap` 复用 V2 行情缓存与 DEI 双层股校验；行情缺失或 FPI 未核验汇率时优雅降级保留无量纲比率；金融 SIC（`6000-6299/6300-6411/6798`）隐藏 `sbc_pct_of_fcf`，保留净稀释、回购、分红与悬顶占比。

### 3.2 支柱 2：DEF 14A 高管激励考核锚与治理体检（Layer 1 0-LLM + Layer 2 按需 CD&A）

- **Layer 1（0 LLM，`<2MB` 瞬时内存，`<1.5s`）**：
  - 复用已拉取的 `submissions` 定位最新 2 份 `DEF 14A`（兼容 `DEFA14A`）；FPI（仅 20-F）标 `fpi_20f_limited`，`as_of < 2023-01-01` 标 `pre_pvp_mandate_unavailable`。
  - 拉取归档 `index.json` 定位 `*_htm.xml`（硬上限 `5MB`，先扫描拒绝 `<!DOCTYPE` / `<!ENTITY` 防 XXE/DTD 炸弹）。
  - **XBRL 维度解析**：先建 `context_id -> (period_end, dimensions_dict)` 索引，按 `(fiscal_year, peo_member)` 复合键聚合多 PEO（如 `JNJ` 过渡年）与非 PEO 高管，清洗 `CoSelectedMeasureName` 脏引号/项目符号并保留 `raw_label` 溯源。
  - **确定性输出**：
    - 考核锚 5 档分级（外置正则词典）：`capital_efficiency`（ROIC/ROCE/Economic Profit）> `per_share_cash_gaap`（FCF/GAAP EPS）> `relative_tsr` > `revenue_or_nongaap` > `weak_adjusted`（Adj. EBITDA/Billings）；
    - 年度考核锚 Diff（今年 vs 去年）：删除 ROIC/FCF 或降级触发 `INCENTIVE_ANCHOR_DOWNGRADED`；
    - Pay-TSR 反向背离 + **Founder-CEO 豁免**（`PeoTotalCompAmt < \$2M` 且无巨额股权调整）；
    - SEC 新规硬旗标：`InsiderTrdPoliciesProcAdoptedFlag`、`MnpiDiscTimedForCompValFlag`（`true` 直接红灯）。
- **Layer 2（按需触发，1 次 `glm-5.3-flash` 调用）**：
  - 主 HTML 硬上限 `8MB`，流式转纯文本后即刻 `del` 原 HTML，按正则锚点（`Executive Summary` / `Annual Cash` / `Long-Term Equity` / `Clawback` / `Stock Ownership Guidelines` / `Say-on-Pay`）切出最多 4 窗，严格 `<=20,000` 字符 + 3KB Layer 1 事实 JSON。
  - **单次调用内置 `skeptic_counterview` 反方栏** + 双重服务端门控（数值复用 V2 `_verify_cited_metrics`，引文复用 V1 `find_quotation_span` + `check_negation_context`），未核验条目降级 `unverified_dropped` 并记入 `open_gaps`。

### 3.3 支柱 3：4–8 季指引兑现信用台账（Guidance Credibility Ledger）

- **轨 A（0 成本主轨）**：新增按 `cik10` 键控的 Postgres 集合 `GUIDANCE_CREDIBILITY_LEDGER`（单公司一文档，禁止 `.items()` 扫描；客观 SEC 事实跨 Topic 复用，用户批注按 `account_id` 隔离）。每次方向 A 锁稿通过共享 helper 从 `guidance_evaluations` 幂等提取季度指引区间、实际值、`basis`（`gaap`/`non_gaap`）、`source_accession` 落库。
- **轨 B（按需冷启动 6 季回溯）**：Layer 1 诚实展示“已有季度 = 0”，提供按需按钮触发 **1 次合并 `glm-5.3-flash` 调用**：拉近 6 季 `8-K Item 2.02` 的 `Exhibit 99.1`（每季仅截 Outlook/Guidance 段 `<=3,500` 字符，合计 `<=21KB`），批量提取指引区间与逐字引文，经 `find_quotation_span` 核验后由 **0-LLM 代码**按 `guidance.basis == actual.basis` 口径锁对账（错配标 `basis_mismatch_unscored`），永久落库缓存。
- **确定性信用画像（样本 `<3` 季不评级）**：`beat_rate_pct`、`inline_rate_pct`、`miss_rate_pct`、`avg_surprise_vs_mid_pct`，以及 `chronic_sandbagger` / `high_credibility` / `guidance_miss_prone` / `guidance_withdrawn_or_sparse`。

---

## 4. “规则改变也不用大规模重建”的解耦架构

- **事实层与规则层彻底物理分离**：
  - Postgres 仅存客观原始/投影事实：`PEER_COMPANY_SNAPSHOTS`（v3）、`DEF14A_FACT_SNAPSHOTS`（v1）、`GUIDANCE_CREDIBILITY_LEDGER`（v1）。
  - **Layer 1 派生判定不落库缓存**，每次读取实时基于事实快照 + 外置 `v25_governance_rules.json`（Pydantic `extra="forbid"` + 正则预编译 + `rules_sha256`）内存重算（`<5ms`）。
  - 仅持久化 Layer 2 LLM 产物（`GOVERNANCE_L2_CACHE`），主键强制包含 `(cik, def14a_accession, rules_sha256, prompt_version)`。
- **效果**：日后调整 SBC/FCF 红黄线、考核锚词典、Founder-CEO 豁免门槛或沙袋效应参数，**0 SEC 重抓、0 LLM 重跑、0 数据库迁移**，全量即时生效。

---

## 5. 稳定性、实时性、雷达闭环与前端有效内容密度

1. **受限容器内存控制（按更严格的 `512M` 约束设计）**：复用 per-CIK Singleflight + `BoundedSemaphore(2)`，XML 5MB / HTML 8MB 硬上限、切片后即刻 `del`，禁用 `.items()` 全表扫描，单请求峰值增量 `<35MB`。
2. **响应延迟目标**：热缓存（`cache_only` 或 `submissions` 15–30m TTL 命中）`<80ms`；温缓存（补拉 `*_htm.xml`）`<1.2s`；冷启动 Layer 1（0 LLM）`3–6s`。
3. **实时雷达集成（`governance_audit` 0-LLM 通道）**：在 `WatchChannel` / `InboxSourceChannel` 新增 `governance_audit`（Phase 1 0-LLM 运行），仅当新 `DEF 14A`/`10-K` 触发考核锚降级、MNPI 红灯、首次进入“假回购真稀释”或净稀释恶化 `>1.5pp/年` 时以 `event_thread_key = f"v25:{cik}:{fy}:{kind}"` 折叠推入 `/inbox`，平时保持安静。
4. **前端三层有效内容密度（零内联样式，`style-src 'self'` CSP）**：
   - **L1（30 秒扫读）**：`TopicPage` 顶部 `GovernanceAuditBar`（默认 `cache_only=true` 且传 `cik`），直出 `3Y净稀释` · `SBC/FCF` · `高管考核锚` · `指引兑现率`；
   - **L2 / L3（展开深挖）**：`GovernanceAuditPanel`（3 张高密度确定性卡 + 6 季指引冷启动按钮 + 按需 CD&A 简评），点击指标/引文穿透 SEC `accession` 与核验原文。

---

## 6. 分阶段实施路线与验收标准

1. **Step V2.5-1（底座修复 + 规则引擎 + 三大支柱 Layer 1 + 离线单测）**：修 5 处底座隐患 → 升级 `projection_version=3` + `v25_governance_rules.json` + `research_governance_audit.py` → 离线单测覆盖拆股（含非整数）、`JNJ` 多 PEO、`META` 脏字符/创始人豁免、`TSM` FPI、金融 SIC、口径错配、`as_of` 零泄漏、改规则零重建。
2. **Step V2.5-2（Layer 2 + 6 季回溯 + API / Radar / Tool + 5 股真实验收）**：接入 `/v2/research/governance/audit`、`/audit/cda-brief`、`/guidance/backfill`、`FinDataToolbox.governance_capital_audit`、`governance_audit` 雷达通道；在 `NVDA / META / JNJ / AAPL / TSM` + 平台 `glm-5.3-flash` 上实测验证内存、延迟与 100% 数值/引文核验。
3. **Step V2.5-3（前端落点 + 构建部署）**：`api/governance.ts`、`GovernanceAuditBar.tsx`、`GovernanceAuditPanel.tsx`、`index.css`，完成 `vite build` + Vitest 并部署上线。

---

## 7. `/grill-me` 已收敛的 4 项最终产品与工程决策（定稿）

1. **前端产品入口定位**：**仅内嵌在 `TopicPage`**（在 `PeerCoordinateBar` 下方挂载 1 行 `GovernanceAuditBar` + 可展开 `GovernanceAuditPanel`，零内联样式），**不新增独立一级侧栏页**，保持研究工作台聚焦。
2. **支柱 3 边界与方向 B 闭环**：**1 + 2 合并实施**——顺手修复 `research_delta_review.py:225` 指引 Schema 错配，支柱 3 采用「方向 A 锁稿温轨 + 按需 6 季 `8-K Ex-99.1` 冷启动回溯」双轨，**并一并将方向 B 的 `earnings_delta_fn` 接入 `main.py` 的 `run_p1b_radar_sweep`**，同步打通新财报自动 Delta 复核与指引台账自动回填。
3. **`governance_audit` 雷达通道策略**：**默认随新建订阅自动开启**（默认 `watched_channels = ["v1_events", "earnings_delta", "governance_audit"]`）；纯 0-LLM，仅在每年新 `DEF 14A / 10-K` 落地且触发 4 类高价值红黄线时推 `/inbox`，平时完全静默。
4. **Layer 1 预热与 Layer 2 触发时机**：**方向 A 锁稿时自动静默预热 Layer 1（纯 0-LLM，\$0 token 成本）**，用户打开已锁稿的 `TopicPage` 即可 `<80ms` 命中热缓存直接看到 1 行治理与资本配置锚点；Layer 2（CD&A 深度简评 & 6 季 `8-K` 指引回溯）保持用户**按需手动点击触发**并持久缓存（缓存键含 `rules_sha256`）。

