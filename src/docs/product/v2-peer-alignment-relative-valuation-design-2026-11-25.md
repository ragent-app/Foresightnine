# V2 垂类引擎设计规格：同业横向对齐与相对估值引擎（Peer Alignment & Relative Valuation）

- **日期**：2026-11-25
- **状态**：经 `/grill-me` 全分支对齐确认，待实施
- **前置基线**：方向 A（季报深研）、V1（SEC 重大事件 `ca29276`）、方向 B（假设台账 + P1-B 错峰雷达 + 财报 Delta + `/inbox` `5613f15`）均已上线验收。
- **产品顶层约束**：[product-paradigm-a-engine-b-radar-2026-11-24.md](product-paradigm-a-engine-b-radar-2026-11-24.md) · [AGENTS.md](/home/ubuntu/Foresightnine/AGENTS.md)

---

## 一、已锁定的 5 项核心产品与架构决策

1. **范围与节奏**：专注把 **V2** 做深做透，V2.5（DEF 14A 激励 + SBC 真实净稀释 + 跨季信用）紧随其后独立交付。
2. **产品双落点**：
   - **独立横评视图（`/compare`）**：支持用户自由选择 2–6 家公司做同口径横向对比、保存/管理自定义 Peer 组；
   - **单公司 `TopicPage` 行业坐标条**：在锁定 `EarningsDossier` 顶部轻量展示 1 行行业横向坐标（如“同业增速第 1 / FCF 利润率第 2 / Reverse-DCF 隐含增速较中位数贵 +420 bps”），点击一键跳转或展开完整横评矩阵。
3. **混合 Peer 组生成机制（严禁无校验盲拉 SIC）**：
   - **外置可热更新策展配置**（`peer_groups.json`）：覆盖核心美股行业/主题（半导体与算力、云计算/超大规模、互联网广告、消费电子、企业软件、医疗制药、大型零售、电动车/工业等）；
   - **用户自定义 Peer 组持久化**（Postgres `peer_groups` 集合，按 `account_id` 隔离）：用户可随时增删对比标的、另存为常用同业组；
   - **10-K 竞争对手线索兜底**：对未命中内置策展组的冷门公司，提示用户手动添加，可选从最新 10-K Item 1 竞争格局提取候选提示（不自动按 SIC 硬凑苹果和橘子）。
4. **严格 SEC 原始口径对齐与不可比告警灯（不捏造自然季插值）**：
   - 双基准横评：**最新可得 TTM（滚动 12 个月）+ 最新已披露单季 YoY**；
   - 每家公司显式标注 `period_end_date`、`fiscal_period_label`、`accounting_standard`（`us-gaap` / `ifrs-full`）、`currency`（`USD` / `TWD` / `EUR` 等）；
   - **确定性口径告警灯（`comparability_warnings`）**：
     - 组内任意两家 `period_end_date` 差距 `> 45 天` → 标黄提示财年错位；
     - 存在跨会计准则（US-GAAP vs IFRS）或非 USD 币种 → 标黄提示；
     - 存在金融/地产 SIC（如银行/REITs）混入非金融组 → 标红提示 FCF/EV/Reverse-DCF 不可比；
     - 某标的缺失关键 XBRL（如部分 FPI 未报季报 XBRL）→ 标为 `partial` 且默认不污染同业中位数计算（前端支持勾选“仅按可比标的算中位数”）。
5. **计算与 LLM 边界（默认 0-LLM 秒出 + 按需归因简评）**：
   - **Layer 1（默认，0 LLM，目标 `3–8s`）**：纯 Python Decimal 计算全部财务/盈利质量/估值/Reverse-DCF/排名/中位数偏离度指标（复用方向 A 已验证的 SEC XBRL YTD→QTD 现金流差分、Reverse-DCF 与行情/市值适配层，并发拉取受共享 SEC 7–8 RPS 限速器保护 + L1 缓存复用）。
   - **Layer 2（可选，按需触发“生成 AI 同业归因简评”）**：单次受控 `deep_reasoner` 调用（经平台并发闸门 + 429 长退避），**仅喂入 Layer 1 确定性横评矩阵 JSON**，严格要求每个数字必须回指矩阵字段，解释“为什么高估值/低估值是否被增速、毛利率与 FCF 质量支撑”及最大相对风险点。

---

## 二、核心确定性指标矩阵（全部 Python Decimal）

| 维度 | 指标清单 | 说明 |
| --- | --- | --- |
| **口径锚点** | `period_end_date`, `fiscal_period_label`, `accounting_standard`, `currency`, `market_cap_usd`, `enterprise_value_usd` | 强制展示，杜绝隐藏错位 |
| **增长动能** | `ttm_revenue_usd`, `ttm_revenue_yoy_pct`, `latest_q_revenue_yoy_pct`, `revenue_accel_pp`（单季增速 − TTM 增速） | 识别加速 vs 减速拐点 |
| **盈利能力** | `ttm_gross_margin_pct`, `ttm_operating_margin_pct`, `ttm_net_margin_pct`, `ttm_roic_or_roe_pct` | 同口径对比护城河厚度 |
| **现金流质量** | `ttm_fcf_usd`, `ttm_fcf_margin_pct`, `ocf_to_ni_ratio`, `net_cash_usd`, `quality_light`（🟢/🟡/🔴） | 复用方向 A 确定性排雷逻辑 |
| **相对估值 & 隐含预期** | `pe_ttm`, `ev_to_sales_ttm`, `ev_to_ebit_ttm`, `ev_to_fcf_ttm`, **`reverse_dcf_implied_5y_fcf_cagr_pct`** | 核心差异化：直接横向对比市场给每家公司定价了多少隐含增速 |
| **横向坐标（自动计算）** | 每项核心指标的 `rank`（第几名）与 `vs_peer_median`（相对同业中位数偏离百分点 / bps / %） | 直接生成 1 行行业坐标摘要喂给 `TopicPage` |

---

## 三、模块拆分与 API 契约

### 1. 后端独立模块（零侵入方向 A / V1 / 方向 B）
- `src/apps/api/app/fin_data/adapters/peer_groups.json`：外置可热更新行业同业组配置。
- `src/apps/api/app/research_peer_compare.py`：
  - Pydantic `extra="forbid"` 模型：`PeerGroupConfig`、`PeerCompanyMetrics`、`ComparabilityWarning`、`PeerComparisonMatrix`、`PeerAttributionBrief`；
  - `resolve_default_peers(symbol_or_cik)`；
  - `build_peer_comparison_matrix(symbols_or_ciks, *, as_of, exclude_non_comparable_from_median)`（纯 0-LLM Decimal 引擎，复用 SEC XBRL + 行情缓存）；
  - `generate_peer_attribution_brief(matrix, *, runner, slot)`（按需 1 次 LLM 归因简评 + 数值一致性检查）。
- `src/apps/api/app/main.py`：
  - 新增 Postgres 集合：`PEER_GROUPS`（用户保存的自定义同业组）、`PEER_COMPARE_CACHE`（短期矩阵缓存，避免重复刷 SEC）。
  - 新增端点：
    - `GET /v2/research/peers/suggest?symbol=NVDA`（返回默认推荐同业 + 用户已保存同业组）
    - `GET /v2/research/peers/groups` / `POST /v2/research/peers/groups` / `DELETE /v2/research/peers/groups/{group_id}`
    - `POST /v2/research/peers/compare`（同步 0-LLM，返回完整 `PeerComparisonMatrix`，目标 `3–8s`）
    - `POST /v2/research/peers/compare/brief`（按需单次平台模型/BYOK 归因简评）
- 在 `FinDataToolbox` 注册 `peer_relative_valuation`（纯 Layer 1 确定性工具，供方向 A Agent 按需调用）。

### 2. 前端双落点（零内联样式，遵守 `style-src 'self'` CSP）
- **侧栏一级入口 `同业横评`（`/compare`）** → `src/apps/web/src/pages/ComparePage.tsx`：
  - 顶部同业选择器（输入 2–6 个 Ticker、一键加载行业默认组、保存自定义组）；
  - 口径不可比告警横幅（明确列出截止日差 > 45 天 / IFRS / 金融 SIC，提供“排除不可比标的算中位数”开关）；
  - 核心横评矩阵表（分“增长 / 盈利 / 现金流质量 / 相对估值 & Reverse-DCF”分组，高亮目标公司、展示排名与相对中位数偏离）；
  - 按需按钮“生成 AI 同业归因简评”（展示简洁 3 段式归因 + 相对风险点）。
- **`TopicPage` 顶部行业坐标条** → `src/apps/web/src/pages/topic/PeerCoordinateBar.tsx`：
  - 紧邻 `ThesisLedgerBar` 轻量展示目标公司的 1 行同业坐标（增速排名 / FCF 利润率排名 / Reverse-DCF 相对中位数溢价），点击 `展开完整横评 →` 跳转到 `/compare?primary=NVDA&peers=AMD,AVGO,INTC`。

---

## 四、分阶段实施与平台模型验收标准

| 阶段 | 交付内容 | 验收出口标准 |
| --- | --- | --- |
| **Step V2-1（确定性横评引擎 + 外置同业配置 + 单测）** | `peer_groups.json` + `research_peer_compare.py`（Layer 1 0-LLM 矩阵 + 口径告警灯 + 排名/中位数偏离 + Layer 2 按需简评）+ 离线单测 | 覆盖财年错位（AAPL/NVDA/MSFT）、IFRS/FPI（TSM）、金融 SIC 混入（JPM）等边界测试全绿 |
| **Step V2-2（API 路由 + 工具注册 + 3 组真实美股实测）** | `main.py` 路由与 Postgres 集合 + `FinDataToolbox` 注册 + 真实 SEC + 平台模型（`glm-5.3-flash`）实测 3 组代表行业：**半导体（NVDA/AMD/AVGO/INTC/TSM）**、**云巨头（MSFT/GOOGL/AMZN/META）**、**医药（JNJ/LLY/MRK/PFE）** | 3 组真实 SEC 横评 0 崩溃、Layer 1 冷/热响应达标、口径告警灯准确触发（TSM IFRS + NVDA 财年错位）、按需简评无编造数字 |
| **Step V2-3（前端 `/compare` + `TopicPage` 行业坐标条 + 上线）** | `src/apps/web/src/api/peers.ts`、`ComparePage.tsx`、`PeerCoordinateBar.tsx`、`Sidebar.tsx`、`router.tsx`、`index.css` + 构建/单测 + 部署上线 | `vite build` + 前端测试全绿，线上容器 Healthy，`/compare` 与 `TopicPage` 联动闭环 |
