# V2 垂类引擎设计规格（v2 定稿）：同业横向对齐与相对估值引擎

- **日期**：2026-11-25
- **版本状态**：v2 定稿（已整合 `/grill-me` 决策树、`/boost` 代码库深度体检及红队复核全部 11 项工程硬约束，可直接开工）
- **前置基线**：方向 A（美股季报深研）、V1（SEC 重大事件 `ca29276`）、方向 B（假设台账 + P1-B 错峰雷达 + 财报 Delta + `/inbox` `5613f15`）均已上线验收。
- **关联文档**：[product-paradigm-a-engine-b-radar-2026-11-24.md](product-paradigm-a-engine-b-radar-2026-11-24.md) · [AGENTS.md](/home/ubuntu/Foresightnine/AGENTS.md)

---

## 一、产品定位与 5 项核心决策

1. **范围与节奏**：专注把 **V2** 做深做透；V2.5（DEF 14A 激励考核 + 5 年回购/净股本审计 + 跨季信用）紧随其后独立交付（但本轮顺手在 `STANDARD_LINES` 补齐 `stock_based_compensation` 以直接激活 `ev_to_fcf_ex_sbc`）。
2. **前端双落点（零内联样式，遵守 `style-src 'self'` CSP）**：
   - **独立横评视图（`/compare`）**：自由选 2–6 家公司做同口径横向对比、保存/管理自定义 Peer 组；
   - **单公司 `TopicPage` 顶部行业坐标条（`PeerCoordinateBar`）**：轻量展示 1 行行业横向坐标（增速排名 / FCF 利润率排名 / Reverse-DCF 相对中位数溢价），点击一键下钻 `/compare`。
3. **混合 Peer 组生成机制（严禁无校验盲拉 SIC）**：
   - 外置可热更新策展配置（`peer_groups.json`），覆盖核心美股行业/主题；
   - 用户自定义 Peer 组持久化（Postgres `PEER_GROUPS`，按 `account_id` 隔离）；
   - 冷门标的由用户手动增删，辅以 10-K Item 1 竞争对手线索提示。
4. **严格 SEC 原始口径（严禁自然季插值）+ 三色口径告警灯**：
   - 双基准横评：**最新可得 TTM（滚动 12 个月）+ 最新已披露单季 YoY**；
   - 显式展示每家公司的 `period_end_date`、`fiscal_period_label`、`accounting_standard`、`currency`；
   - 存在截止日差 `> 45 天`、跨 IFRS/US-GAAP、非 USD 币种、金融 SIC 混入或股本偏差 `> 15%` 时显式亮灯，支持“仅按可比标的算同业中位数”。
5. **0-LLM 确定性优先 + 按需 AI 归因简评**：
   - **Layer 1（默认，0 LLM）**：纯 Python Decimal 计算全部财务、盈利质量、相对估值、Reverse-DCF、排名与中位数偏离；
   - **Layer 2（按需触发）**：单次受控 `deep_reasoner` 调用，仅基于 Layer 1 矩阵 JSON 生成相对估值归因简评，服务端强制校验引用数字一致性。

---

## 二、性能、容器稳定性（`512M / 1.0 vCPU`）与限速架构

针对现网 [compose.yaml](file:///home/ubuntu/Foresightnine/src/infra/docker/compose.yaml) 的 **`cpus: "1.0"` + `memory: 512M`** 硬约束与 SEC / 行情配额限制，V2 采用以下机制级保障：

1. **统一单一 Redis SEC 限速器**：
   - V2 所有 SEC 请求（`submissions` + `companyfacts`）**必须接入** [pacing.py](file:///home/ubuntu/Foresightnine/src/apps/api/app/fin_data/adapters/sec_events/pacing.py) 的 `SecPacingLimiter.from_env()`（复用 Compose 已有 `redis:7-alpine`），与 V1/方向 B 共享全局 4.5–6 RPS 令牌桶及 429 `Retry-After` 全局冷却；严禁直走 `InProcessRateLimiter`。
   - 前台 `/compare` 交互请求带短等待预算（`10s`），优先于后台雷达巡检。
2. **两级门卫 + per-CIK `Singleflight` + 2 路有界解析 + 完整历史轻量快照**：
   - **门卫 1（`submissions` 指纹校验）**：并发拉取 ~200KB `submissions/CIK{c}.json`，取最新 `10-Q/10-K/20-F` 的 `(accession, filingDate)`，通过 `PostgresDocumentRepository.get_many` 单次批量校验 `PEER_COMPANY_SNAPSHOTS`；命中即 0 下载、0 解析（`< 50ms`）。
   - **门卫 2（per-CIK `Singleflight` + `BoundedSemaphore(2)`）**：同 CIK 并发未命中时只下载解析 1 次（杜绝重复吃双倍内存与 SEC 额度）；全局最多 2 路并发流式拉取 `companyfacts`，在独立函数作用域内**只投影出 V2 所需的 ~16 个标准科目的完整历史事实行（含每条 `filed_at`）**，离开作用域即通过引用计数自然释放 10–30MB 原 dict（不在热路径频繁手动 `gc.collect()` 避免单核 STW 停顿）。
   - **~25–40KB 轻量快照兼顾历史 `as_of`**：单公司快照保留该 ~16 个科目的全部历史行及 `filed_at`（比原 JSON 小 ~500 倍），一份 `(cik, latest_accession)` 快照即可**零二次下载**服务 `as_of=None` 和任意历史 `as_of` 回放。
   - **低峰期预热**：支持对 `peer_groups.json` 中 40–50 家核心龙头闲时预热快照，使 80%+ 首次点击直接落入 `< 1s` 温/热路径。
3. **行情配额保护（Twelve Data 8 req/min）与无痛降级**：
   - 调用 `TwelveDataAdapter.bars()` **强制传 `start = today - 10 days`**（取代默认 `outputsize=5000`）；
   - 建立 `(symbol, trade_date)` 共享收盘价缓存（盘中 TTL 15min，盘后至次日开盘前 TTL 12h）；
   - 无行情 Key 或命中 429 时**优雅降级为完整基本面横评矩阵**（增速、利润率、FCF 质量、ROIC 全保留），仅估值列标 `quote_unavailable` 并支持用户手动输入参考股价；
   - `TopicPage` 的 `PeerCoordinateBar` 默认走 **`cache_only=true`**（命中即刻渲染，未命中显示轻量按钮“加载同业坐标（0 LLM）”），避免刷页面打穿 8 req/min 配额。

---

## 三、精确财务口径、FPI 兼容与门控细则

1. **FPI / ADR 三层渐进兼容（如 `TSM` / `ASML` / `BABA`，严防 160× 错算）**：
   - **无量纲比率永远保留**：增速（YoY %）、毛利率（%）、营业利润率（%）、FCF 利润率（%）、OCF/NI（×）、ROIC（%）与币种/ADR 比例无关，20-F FPI 照常精确计算并显式标 `FY (20-F)` + 原始币种；
   - **估值列精准隔离**：无经核验 `(adr_ratio, fx_rate)` 时，禁止将 USD/ADR 股价与非 USD 报表混算估值倍数，置空并挂黄灯 `currency_adr_unverified`，且默认排除出中位数；
   - **外置经核验 ADR 比例表**：`peer_groups.json` 支持为高频 FPI 声明经核验的 `adr_ratio`（如 `TSM: 5`、`ASML: 1`、`BABA: 8`）与 `reporting_currency`，具备授权 FX 汇率时解锁 USD 等价市值与估值并附带完整溯源。
2. **`as_of` 时点过滤（零未来数据泄漏）**：
   - 在 [sec_edgar.py](file:///home/ubuntu/Foresightnine/src/apps/api/app/fin_data/adapters/sec_edgar.py) 原始 fact 选取层（**在 `_derive_ttm_rows` 与 `_derive_growth_rows` 之前**）支持可选 `as_of: date | None = None`，硬过滤 `filed_at <= as_of`。
3. **金融 SIC 门控补齐（`6200–6299`）与 `quality_light` 四态定义**：
   - 在 [research_earnings_diagnostics.py](file:///home/ubuntu/Foresightnine/src/apps/api/app/research_earnings_diagnostics.py#L581-L646) 的 `evaluate_sec_sic_gate()` 中补入 `6200–6299`（券商/投行/资管，覆盖 `GS`/`MS`/`BLK`），同时为后续 V4a 扫清障碍；
   - `quality_light` 明确四态：被金融 SIC 门控 → `not_applicable`（⚪）；否则任一红灯 → `red`（🔴）；否则任一黄灯 → `yellow`（🟡）；否则 → `green`（🟢）。
4. **无债公司 EV 资产负债表完整性守卫**：
   - 仅当同期间资产负债表完整存在（`total_assets` 与 `total_liabilities` 均非空）且短期/长期债务标签均未披露时，判定 `total_debt = Decimal(0)` 并附透明注解；若资产负债表本身缺失，仍严格返回 `unavailable`。
5. **纯净 `ttm_roic_pct` 与边界守卫**：
   - 放弃混排 `ROIC/ROE`，统一为资本结构中性的 `ttm_roic_pct = TTM_NOPAT / Avg_Invested_Capital`；
   - 有效税率钳位在 `[0%, 35%]`，缺失或税前亏损时回退法定基准 `21%`（注 `statutory_tax_21pct`）；
   - 当 `Avg_Invested_Capital <= 0`（巨额净现金或负净资产）时，显式标 `unavailable ("Invested Capital <= 0")` 并排除出中位数。
6. **股本口径、亏损季与负 FCF 边界**：
   - 市值优先取最新单季 `shares_diluted`（亏损季如 `INTC` 缺失时安全回退 `shares_basic` 并加注）；
   - 与同期 `EntityCommonStockSharesOutstanding` 交叉比对：若偏差 `> 15%`（双层股权/拆股信号，如 `GOOGL`/`META`），挂出 `share_count_divergence` 黄灯并展示所用股本口径与日期；
   - `TTM FCF <= 0` 时 `reverse_dcf` 与 `ev_to_fcf` 显式标 `unavailable ("TTM FCF <= 0")`，不污染中位数；表头明示 Reverse-DCF 采用统一基准（`WACC 10% / 永续 3%`）。
7. **顺手补齐 `stock_based_compensation`**：
   - 在 `sec_edgar.py` 的 `STANDARD_LINES` 补充 `ShareBasedCompensation` 标签（自动走现有 YTD→QTD 差分），直接激活 `research_earnings_diagnostics.py` 已实现的 `ev_to_fcf_ex_sbc`，同屏对比“含 SBC vs 扣 SBC 真实 FCF 倍数”。

---

## 四、核心确定性指标矩阵与方向 B `WatchTrigger` 预埋契约

| 维度 | 指标清单（全部 Python Decimal） | 说明 |
| --- | --- | --- |
| **口径锚点** | `period_end_date`, `fiscal_period_label`, `period_basis`（`TTM`/`FY`）, `accounting_standard`, `currency`, `market_cap_usd`, `enterprise_value_usd` | 强制展示，杜绝隐藏错位 |
| **增长动能** | `ttm_revenue_usd`, `ttm_revenue_yoy_pct`, `latest_q_revenue_yoy_pct`, `revenue_accel_pp` | 识别加速 vs 减速拐点 |
| **盈利能力** | `ttm_gross_margin_pct`, `ttm_operating_margin_pct`, `ttm_net_margin_pct`, `ttm_roic_pct` | 纯 ROIC，带税率钳位与负投入资本守卫 |
| **现金流质量** | `ttm_fcf_usd`, `ttm_fcf_margin_pct`, `ocf_to_ni_ratio`, `net_cash_usd`, `quality_light`（🟢/🟡/🔴/⚪） | 复用方向 A 排雷引擎 |
| **相对估值 & 隐含预期** | `pe_ttm`, `ev_to_sales_ttm`, `ev_to_ebit_ttm`, `ev_to_fcf_ttm`, `ev_to_fcf_ex_sbc_ttm`, **`reverse_dcf_implied_5y_fcf_cagr_pct`** | 同屏对比含/扣 SBC FCF 倍数与隐含 5Y 增速 |
| **横向坐标 & 预埋键** | 每项核心指标 `rank` + `vs_peer_median`；锁定稳定 `WatchTrigger` 键：`peer_reverse_dcf_spread_bps`、`peer_revenue_yoy_rank`、`peer_fcf_margin_vs_median_pp` | 直接喂给 `TopicPage` 与未来方向 B 主动监控 |

---

## 五、Layer 2 按需 AI 归因简评（稳定与防幻觉门控）

- **运行时配置**：在 `main.py` 为 `"peer_attribution_brief"` 显式登记 `max_output_tokens=16384` 与 `timeout=240s`，经由 `_platform_llm_concurrency_gate(2)` + 429 长退避调度（绝不阻塞 Layer 1 秒出）。
- **服务端数值一致性门控**：输出严格 JSON（`summary`, `valuation_vs_fundamentals`, `relative_risks`, `cited_metrics: [{symbol, metric_key, value}]`），服务端用确定性代码核对每条 `cited_metrics` 是否与 Layer 1 矩阵精确匹配，杜绝串行写错公司数字。

---

## 六、模块拆分、API 契约与前端落点

### 1. 后端模块与存储
- `src/apps/api/app/fin_data/adapters/peer_groups.json`：外置行业同业组 + 头部 FPI 经核验 `adr_ratio` 配置。
- `src/apps/api/app/research_peer_compare.py`：Pydantic `extra="forbid"` 模型、两级门卫 + Singleflight 快照加载、确定性横评矩阵计算、口径告警灯、按需 Layer 2 简评与数值门控。
- `src/apps/api/app/main.py` 新增 3 个 Postgres 集合：`PEER_GROUPS`、`PEER_COMPANY_SNAPSHOTS`、`PEER_COMPARE_CACHE`（以及进程/PG 报价缓存）；新增端点：
  - `GET /v2/research/peers/suggest?symbol=NVDA`
  - `GET /v2/research/peers/groups` / `POST /v2/research/peers/groups` / `DELETE /v2/research/peers/groups/{group_id}`
  - `POST /v2/research/peers/compare`（同步 0-LLM，支持 `cache_only`、`as_of`、`exclude_non_comparable_from_median`、`manual_prices`）
  - `POST /v2/research/peers/compare/brief`（按需单次 LLM 归因简评）
- `FinDataToolbox` 注册纯 Layer 1 工具 `peer_relative_valuation`，供方向 A Agent 按需调用。

### 2. 前端双落点
- **侧栏一级入口 `同业横评`（`/compare`）** → `src/apps/web/src/pages/ComparePage.tsx`：同业选择器（2–6 家 + 行业预设 + 自定义保存）、三色口径告警横幅（支持一键排除不可比标的算中位数）、分组横评矩阵表、按需“生成 AI 同业归因简评”卡片。
- **`TopicPage` 顶部行业坐标条** → `src/apps/web/src/pages/topic/PeerCoordinateBar.tsx`：默认 `cache_only=true`，命中即刻展示 1 行行业坐标，未命中展示轻量按钮“加载同业坐标（0 LLM）”，点击 `展开完整横评 →` 跳转 `/compare`。

---

## 七、分阶段实施计划与实测验收门控

| 阶段 | 交付内容 | 验收出口标准 |
| --- | --- | --- |
| **Step V2-1（底座修补 + 确定性横评引擎 + 离线单测）** | 修补 `sec_edgar.py`（`as_of` + `SBC` 标签）与 `research_earnings_diagnostics.py`（`6200–6299` SIC 门控 + 无债 EV 完整性守卫 + `ev_to_fcf`）；实现 `peer_groups.json` + `research_peer_compare.py`（含 Singleflight、2 路信号量、FPI 三层兼容、ROIC 守卫、双层股告警、Layer 2 数值门控） | 离线单测 100% 覆盖 7 大边界：`TSM` TWD/ADR、`INTC` 负 FCF+亏损股本、`GOOGL` 股本偏差、`GS`/`JPM` 金融 SIC、`as_of` 零泄漏、无债公司 EV、负投入资本 ROIC |
| **Step V2-2（API + 工具注册 + 3 组真实美股 + 平台模型实测）** | `main.py` 路由与 Postgres 集合 + `peer_attribution_brief` 16384/240s 登记 + `FinDataToolbox` 注册 + 真实 SEC + 平台模型（`glm-5.3-flash`）实测 3 组代表行业：**半导体（NVDA/AMD/AVGO/INTC/TSM）**、**云巨头（MSFT/GOOGL/AMZN/META）**、**医药（JNJ/LLY/MRK/PFE）** | 3 组真实 SEC 横评 0 崩溃、512M 容器零 OOM；冷启动 `6–14s` / 温缓存 `< 1.2s` / 热缓存 `< 100ms`；`TSM`、`INTC`、`NVDA` 口径灯准确触发；按需简评 `cited_metrics` 100% 核验通过 |
| **Step V2-3（前端 `/compare` + `TopicPage` 坐标条 + 上线）** | `src/apps/web/src/api/peers.ts`、`ComparePage.tsx`、`PeerCoordinateBar.tsx`、`Sidebar.tsx`、`router.tsx`、`index.css` + 构建/单测 + 部署上线 | `vite build` + 前端测试全绿，线上容器 Healthy，`/compare` 与 `TopicPage` 联动闭环 |
