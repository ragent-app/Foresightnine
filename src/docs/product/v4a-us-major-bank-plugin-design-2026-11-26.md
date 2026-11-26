# V4a「美股大型银行专用财报插件」深度分析与定稿实施方案

- **代码基线**：`master @ 732c782`（方向 A、方向 B、V1、V2、V2.5、V3a v5 均已上线验收）
- **调研方法**：全仓源码逐行核验 + 实时 SEC EDGAR 探针覆盖 **14 家金融发行人**（6 大行 `JPM / BAC / WFC / C / GS / MS` + 区域行 `USB / PNC` + 信用卡 `COF / AXP` + 托管行 `BK / STT` + 券商/资管 `SCHW / BLK`）的 `submissions` / `companyfacts` / `FilingSummary.xml` / `R*.htm`（Statements、Tables、Details）/ `10-Q MD&A` / `8-K EX-99.1/2/3` + `TestClient` 实证复现。
- **核心定位**：**纯后端与 Agent / 雷达引擎先行（本轮不改前端 UI）**，以 **0-LLM 确定性三源底座 + 5 维银行专项诊断 + Gordon `P/TBV` vs `ROTCE` 估值锚 + `kpi_us_large_banks` L2 方法包**，在不破坏既有工业股门控的前提下，全面打通方向 A、方向 B 雷达、V2 同业横评、V2.5 治理审计、**V3a 银行分部深度核验**与主研究 Agent 供血。

## 〇、`/grill-me` 锁定决策总表（`D1` – `D8`，全部拍板）

| 编号 | 决策主题 | 已锁定结论 |
| --- | --- | --- |
| **D1** | **交付形态与前端边界** | **纯后端与 Agent / 雷达引擎先行**（与 V3a v4/v5 一致，**本轮不改前端 UI**；全部新增响应字段保持向后兼容，通过 API、Agent 工具、R2-D5 与现有雷达通道完整验收）。 |
| **D2** | **银行准入与子类边界** | **通用规则驱动**：美股 6 大行深度对齐 + 按 `universal_commercial` / `investment_bank_bhc` / `custodian_or_broker_bank` 3 子类自动兼容美股大型区域/托管行（`USB / PNC / BK / STT / SCHW`）；严格拦截 `BLK / AXP / 保险 / REIT`；区域行 Category III/IV AOCI opt-out 与非美 20-F IFRS9 留待后续。 |
| **D3** | **`P/TBV` 与 `TBVPS` 主口径** | **Reported（管理层披露 / 市场惯例口径）为主口径**（GS 用 `\$341.43`，含 `7.0M` 已授未归属 RSU），并行计算 **XBRL-Standardized** 审计口径，强制附带 `TbvpsReconciliationBridge` 与 `>1%` 差异提醒，并支持 API 参数一键切换审计视图。 |
| **D4** | **HTM 浮亏红黄灯策略** | **联动升级制（高信噪比）**：单纯 `HTM 税前浮亏 / TCE >= 30%` 标 **🟡** 并输出税后 `Pro-Forma CET1 ex-HTM` 压力测试；**仅当叠加 `CET1 缓冲 < 100 bps` 或 `存款单季流失 >= 3%` 时升级为 🔴 红灯**（3Q26 实测 BAC 双压触发 🔴，WFC 留 🟡）。 |
| **D5** | **Gordon `P/TBV` 估值基准** | **统一透明基准 `K_e = 10.0% / g = 3.0%`**（保证 6 大行同业横评标尺一致且严守 FRED 禁用边界），必出 `K_e ∈ {9,10,11}% × g ∈ {2,3,4}%` 3×3 敏感性矩阵并支持 API 参数覆盖。 |
| **D6** | **V3a 银行分部深度核验** | **0-LLM 确定性 `BankSegmentSnapshot` 优先对撞**（严格隔离 GAAP 与 Managed/FTE，零幻觉/零额外耗时）+ **仅对未命中的子业务线（如 Card/Markets 细分项）按需走 `v3a_footnote_verify` 兜底**。 |
| **D7** | **基础设施顺手治理** | **`main.py` 全部 11 处非法 `ApiProblem` 枚举零破坏收口**（`503 + Retry-After + error_code="internal_error" + details`，不改 v1 schema / 前端生成物），并顺手修补 `main.py` 通用入口流向财报小组时的预算控制边界。 |
| **D8** | **集中实测验收口径** | **6 大行全量 Live 压测**：14 家实网 0-LLM SEC 确定性探针 + `JPM / BAC / WFC / C / GS / MS` 全部 6 家真实 Live 方向 A + V3a 银行电话会分部核验 + V2 6 大行横评 + 方向 B 跨季雷达回放 + `BLK`（6211）防污染对照 + 4 路并发容器内存压测（`< 550 MiB`）+ `0` 残留测试账号。 |

> 本方案严格遵守 `AGENTS.md` 纪律：100% 官方免费 SEC EDGAR 一手披露 + 现有行情源，**严禁引入 FRED**；外置规则文件驱动、零 `if symbol == "..."` 个股特例；全部数值保留 `Decimal`（100 位精度）、公式、口径（`gaap` / `managed_fte` / `standardized_basel3`）与 `as_of` 反未来截断。

---

## 一、全链路源码与真实 SEC 数据实证排雷（12 类致命/高危陷阱）

| # | 陷阱类别 | 现网源码 / SEC 实证发现 | 定稿硬性解法 |
| --- | --- | --- | --- |
| 1 | **`(Tables)` 列头崩坏 + `td.fn` 覆写清空当期营收** | 5/6 大行 10-Q `(Tables)` 用 `<td>` 做表头导致 `parse_r_html_table` 退化为 `Column 1..15`；即使换用机器生成的 `(Details)` 表（`JPM R166` / `WFC R121`），[sec_deep.py](../../apps/api/app/fin_data/adapters/sec_deep.py) 未剥离 `td.fn` 脚注列导致 `values_by_column[header]` 被右侧 `[1]` 覆写为空白，且表尾嵌套 `<table class="outerFootnotes"><tr class="outerFootnote">` 被误收入 `subtables[0]` | **分部主路径切至 `(Details)` `R*.htm`**；改造 `_StructuredHtmlParser` 实现 **R1（`outerFootnotes`/`authRefData` 强分流）** 与 **R2（`td.fn` 剥离并挂接左侧单元格 `footnote_refs`，重复表头加 `#c{col_idx}`，银行解析器只读 `cells[col_idx]`）** |
| 2 | **单 `showAR` 链接与碎片区块** | SEC 渲染器在每个 `tr.rh` 仅输出 1 个 `showAR` 链接（`JPM/BAC/C` 均为 `OperatingSegmentsMember`），不能靠 `xbrl_axis_member` 识别分部；`JPM TR[18]` 为 `VISA` 交易对手、`TR[21]/24/28` 为碎片行，`C TR[17]` 为处置组 | **R6**：从 `\|` 分隔 stub 文本剥离 `Operating Segments` 与 `[Member]` 得 `display_name`，仅收录含核心损益行区块，并用 **R7 零误差加总守恒门禁**（6/6 实测 `Delta = 0`）验真 |
| 3 | **监管资本表小数/百分数混排 + 误取子银行 `N.A.`** | `JPM R156` 同列混排 `0.115` 与 `2.50%`，`MS R115` 混排 `0.045 / 11.80% / 0.100`，`GS R163` 写 `0.124`；`JPM R158` **首个区块**是子行 `JPMorgan Chase Bank, N.A.`（`\$296.4B`）而非母行 BHC（`\$304.1B`） | 新增 **R4b（小数/百分数自动归一化）** 与 **R8（BHC 母行实体过滤，强制排除 `Bank, N.A.` / `National Association` / `Depository`）** |
| 4 | **`companyfacts` 6 大行普遍残缺 + Citigroup 滞后 9 个月** | 不仅 `C` 的 `companyfacts` 停留在 `2025-12-31`（缺 2026 Q1–Q3 且 `CommonStockSharesOutstanding` 停在 **2010 年 1 拆 10 合股前 `29,049.6M` 股**，直除会算出 `TBVPS ≈ \$6` 偏差 1600%！），其余 5 家也存在 `PreferredStockValue`（JPM/BAC/MS 缺、COF/PNC 报面值 `0`）、`Intangibles`（JPM/WFC 缺、BAC 粗略四舍五入）和 `HTM 摊余成本`（5/6 缺）空白 | 确立 **`10-Q/10-K R*.htm`（BS/IS/Segment/RegCap/HTM）为全 6 大行一等主源**；优先股反转标签优先级（账面值优先于面值）+ 日期对齐守卫 + 面值归零触发 Tier B；Citigroup 通过单次 `R2.htm`（含 9M 同比列）+ `FY2025` 以 `TTM = YTD_cur + FY_prev - YTD_prev` 零额外外呼拼出 TTM |
| 5 | **资产负债表 `R4.htm` 底部 Consolidated VIEs 重复行** | `JPM R4` / `BAC R4` 底部附有 Consolidated VIEs 表，原样重复 `Loans` 与 `Allowance for credit losses` 行标签（如 BAC 主表 ACL `\$13,005M` vs 底部 VIE ACL `\$849M`） | `R4.htm` 解析遇 `Total liabilities and stockholders' equity` 或 `Variable interest entities` 即终止，按日期列头定位数值列（避开 JPM/C 空白/脚注首列） |
| 6 | **8-K 纯图片展板 + GS 8-K 缺 TBVPS/ROTE + 扁平正则误抓** | `BAC EX-99.1/2`、`WFC EX-99.3`、`GS EX-99.2` 均为**纯 JPG 图片 HTML**（BAC 真表在 `EX-99.3`）；**GS 8-K EX-99.1 全文不含 `TBVPS (\$341.43)` 与 `ROTE (20.5%)`**；扁平去标签正则曾把 JPM CET1 `13.9%` 误抓成 `ROTCE` | `sec_deep.py` 扩展支持 `EX-99.1/2/3` 并按 **HTML 数值表密度** 选优跳过纯 `<img>` 展板（保留 `15MB` 上限覆盖 C `10.31MB` 补充表）；优先 HTML 同行表格提取 + 物理区间检查；GS 等采用 **8-K 补充表 → 10-Q MD&A Non-GAAP → `TCE_10Q / basic_shares_reported`** 三级提取（`sec_get_to_tempfile()` 256KB 流式扫描） |
| 7 | **Stage 0 门控时序错位 + `@lru_cache` 污染 `GS/MS vs BLK`** | [research_earnings_squad.py](../../apps/api/app/research_earnings_squad.py) 两处 `@lru_cache` 仅以 `sic_key` 为键，`GS`（6211）命中银行包后会直接污染 `BLK`（同为 6211）；且 [research_methods/__init__.py](../../apps/api/app/research_methods/__init__.py) `if is_sector_gated:` 短路阻断任何 L2 Pack | 保持 `evaluate_sec_sic_gate` 纯函数不变；新增 XBRL 后置准入函数 `qualify_bank_sector_applicability`；将 `active_sector_plugin` 写入两处 `@lru_cache` 键与 `compose_earnings_method_pack`，支持「工业指标门控 + 银行 L2 Pack 挂载」并拒绝跨领域覆写 |
| 8 | **银行子类分类误伤 `BK / STT / AXP / SCHW`** | 仅凭 SIC 或 `NoninterestIncome` 标签会把 `AXP`（6199）误判为投行、把 `BK / STT`（6022 托管行）强归为商行触发 LDR 红灯 | V4a 仅准入 `6020–6036` 与满足 `Deposits > 0 & Loans > 0 & Deposits/Assets >= 15%` 的 `6211`（彻底排除 `6199 / 6141 / BLK / 保险 / REIT`）；用 `(NetRev - NII)/NetRev` 统一划为 **`universal_commercial` / `investment_bank_bhc` / `custodian_or_broker_bank`** 三子类 |
| 9 | **`main.py` 11 处非法 `ApiProblem` 枚举导致 HTTP 500** | 实证复现 `POST /v2/research/peers/compare` 与 `/watch` 遇 `SecPacingError("source_unavailable")` 直接爆 HTTP 500 且丢失 `Retry-After`；全文件共检出 **11 处** 非法枚举位点 | 全部 11 处零破坏收口至 v1 合法 `ErrorResponse.error_code`（`429→rate_limited`、`404→not_found`、`503→internal_error + retryable + Retry-After`、`422→invalid_request`）并在 `details` 保留精确 reason；修复 `upsert_peer_relative_watch` 优先查快照缓存防断网外呼 |
| 10 | **方向 B 雷达 `_earnings_delta_for_sub` 传空字典空转** | [main.py](../../apps/api/app/main.py) 两处调用 `build_delta_stage0_package` 硬编码传 `new_observed_metrics={}`、`filing_text=""`，会导致银行 `WatchTrigger` 与增量指标永远不触发 | 补齐 `_earnings_delta_for_sub` 对银行订阅注入最新 0-LLM 银行快照指标与 8-K/10-Q 文本 |
| 11 | **下游 3 处硬编码阻断（V2 Brief / Watch / 雷达滞回）** | [research_peer_compare.py](../../apps/api/app/research_peer_compare.py) 只读 `matrix.companies`；`PeerWatchCreateRequest.metric_key` 仅限 3 个工业 key；[research_radar_scheduler.py](../../apps/api/app/research_radar_scheduler.py) `_bps` 滞回 `250bps` 对 CET1 缓冲失真 | 同步改造这 3 处消费者（银行滞回 `peer_cet1_buffer_bps = 25 bps`、`*_x = 0.15x`；前端保持只读不改） |
| 12 | **V3a 电话会银行分部核验 3 处门控断点** | [research_call_collision.py](../../apps/api/app/research_call_collision.py) 缺 `"fte_managed"` 且因 `is_financial_gated` 跳过轨道 1；[research_call_verification.py](../../apps/api/app/research_call_verification.py) 硬校验 `ledger.status == "same_period_10q_matched"` | 扩展 `LedgerMatchStatus` `"bank_v4a_matched"` 与 `BasisType` `"fte_managed"`，放行银行轨道 1（信贷/拨备/存款/HTM 浮亏对撞），并走 0-LLM `BankSegmentSnapshot` 优先 + 未命中子线按需 LLM 附注兜底 |

---

## 二、6 大行 3Q2026 Golden Ground Truth 对账基准（已实网核验）

### 2.1 `(Details)` 分部表零误差守恒对账（6 / 6 全部 `Delta = 0`）

| 银行 | `(Details)` 文件 | 剥离 `td.fn` 列 | Block 0 口径 | 合并营收 (`\$M`) | 经营分部明细 (`\$M`) | Corporate / All Other (`\$M`) | Reconciling / Elim (`\$M`) | 守恒偏差 `Delta` |
| --- | --- | :---: | :---: | ---: | --- | ---: | ---: | ---: |
| **JPM** | `R166.htm` | 3 | GAAP（分部 Managed） | 50,172 | CCB `21,171` / CIB `22,734` / AWM `6,909`（合 `50,814`） | Corporate `93` | Reconciling `-735` | **0** |
| **BAC** | `R113.htm` | 0 | **FTE**（GAAP=`29,958`） | 30,119 | CB `11,572` / GWIM `7,118` / GB `6,205` / GM `5,867`（合 `30,762`） | All Other `-643` | `0`（FTE adj `-161` 另行披露） | **0** |
| **WFC** | `R121.htm` | 3 | GAAP（分部 Managed） | 21,870 | CBL `10,656` / CB `3,236` / CIB `5,008` / WIM `4,104`（合 `23,004`） | Corporate `-610` | Reconciling `-524` | **0** |
| **C** | `R61.htm` | 0 | GAAP | 23,845 | Svc `6,614` / Mkt `6,113` / Bkg `1,741` / Wth `3,269` / USCC `4,664`（合 `22,401`） | All Other `1,432` | Reconciling `+12` | **0** |
| **GS** | `R175.htm` | 0 | GAAP | 18,679 | GBM `13,458` / AWM `5,051` / PS `170`（合 `18,679`） | `0` | `0` | **0** |
| **MS** | `R127.htm` | 0 | GAAP | 20,338 | IS `10,029` / WM `9,065` / IM `1,459`（合 `20,553`） | `0` | Interseg Elim `-215` | **0** |

### 2.2 6 大行 3Q2026 核心财务、资本与双口径 TBVPS 对账表（截至 `2026-09-30`）

| 指标 | **JPM** (`6021`) | **BAC** (`6021`) | **WFC** (`6021`) | **C** (`6021`) | **GS** (`6211`) | **MS** (`6211`) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **8-K 有效表格 Exhibit** | `EX-99.1/99.2` | **`EX-99.3`**（99.1/2 为图片） | `EX-99.1/99.2`（99.3 为图片） | `EX-99.1/99.2`（10.31MB） | `EX-99.1`（无 TBVPS/ROTE） | `EX-99.1/99.2` |
| **NetRev GAAP / Managed (`\$M`)** | **50,172** / 50,907 | **29,958** / 30,119 | **21,870** / 21,870 | **23,845** / 23,845 | **18,679** / 18,679 | **20,338** / 20,338 |
| **Noninterest Expense (`\$M`)** | 27,295 | 19,722 | 13,743 | 13,952 | 11,308 | 13,712 |
| **PPNR GAAP / Managed (`\$M`)** | **22,877** / 23,612 | **10,236** / 10,397 | **8,127** | **9,893** | **7,371** | **6,626** |
| **Provision (`\$M`)** | **2,743** | **1,269** | **1,029** | **2,586** | **368** | **148** |
| **Net Income to Common (`\$M`)** | 14,936 | 6,541 | 5,703 | 4,892 | 5,122 | 4,751 |
| **Efficiency Ratio (3Q26)** | **54%**（GAAP 54.40%） | **65.83%**（FTE 65.48%） | **63%**（62.84%） | **58.5%**（58.51%） | **60.5%**（10-Q） | **67%**（67.42%） |
| **CET1 Std（10-Q 终版）** | **14.0%**（8-K 13.9%） | **10.9%** | **10.03%**（8-K 10.0%） | **12.8%** | **12.4%**（Adv 13.6%） | **14.9%**（Adv 16.3%） |
| **CET1 Reg Min (`4.5+SCB+GSIB`)** | **11.5%**（`+2.5+4.5`） | **10.0%**（`+2.5+3.0`） | **8.5%**（`+2.5+1.5`） | **11.6%**（`+3.6+3.5`） | **11.4%**（`+3.4+3.5`） | **11.8%**（`+4.3+3.0`） |
| **CET1 Buffer (`Std - Min`)** | **+250 bps** 🟢 | **+90 bps** 🟡 | **+153 bps** 🟢 | **+120 bps** 🟢 | **+100 bps** 🟢 | **+310 bps** 🟢 |
| **ROTCE (3Q26 / 9M26)** | **20%** / 24% | **12.60%** / 15.20% | **16.3%** / 16.2% | **11.5%** / 12.6% | **20.5%**（10-Q MD&A） / 22.4% | **22.6%** / 25.4% |
| **TCE 管理口径 (`\$M`)** | **299,130** | **201,760** | **133,447** | **168,149** | **100,927** | **84,465** |
| **期末股本（BS vs 披露, M）** | 2,655.61 vs 2,655.6 | 6,922.70 vs 6,922.7 | 3,001.60 vs 3,001.6 | 1,655.20 vs 1,655.2 | **288.60 vs 295.6 (+7.0M RSUs)** | 1,565.20 vs 1,565.2 |
| **TBVPS 管理口径（主口径）** | **\$112.64** | **\$29.14** | **\$44.46** | **\$101.59** | **\$341.43**（10-Q / `TCE/295.6`） | **\$53.96** |
| **TBVPS XBRL 标准化（审计对照）** | \$112.45（`-0.17%`） | \$29.03（`-0.39%`） | \$44.54（`+0.19%`） | \$101.05（`-0.53%`） | **\$349.71**（**`+2.42%`** 缺 RSU） | \$53.96（`+0.01%`） |
| **HTM 税前净浮亏 (`\$M`)** | **-25,831** | **-103,885** | **-43,228** | **-14,296** | **-1,818** | **-9,545** |
| **HTM 税前浮亏 / TCE** | **8.64%** 🟢 | **51.49%** 🔴（双压升级） | **32.39%** 🟡 | **8.50%** 🟢 | **1.80%** 🟢 | **11.30%** 🟢 |

### 2.3 `P/TBV` 主口径与 GS RSU 分母统一规范（D3 锁定）

1. **主口径锁定为 `tbvps_reported_usd`（Market Convention）**：用于主视图、同业中位数、Gordon 反推 `implied_rotce_pct` 及 WatchTrigger（`peer_ptbv_vs_median_x`）。三级提取链：`8-K Financial Supplement` → `10-Q/10-K MD&A Non-GAAP` → `TCE_10Q / basic_shares_reported`（GS 精确命中 `\$341.43`）；全缺时才降级为 `tbvps_xbrl_std_usd` 并显式标 `primary_tbvps_basis = "xbrl_std_fallback"`。
2. **审计对照口径并行计算 `tbvps_xbrl_std_usd`**（`(Equity - Preferred - Goodwill - Intangibles_ex_MSR) / Ending_Common_Shares`），强制输出 `TbvpsReconciliationBridge` 并在 `|tbvps_gap_pct| > 1.00%` 或 `rsu_share_addon > 0` 时写入 `ComparabilityWarning(code="tbvps_denominator_rsu_adjustment", severity="info")`，支持 API 参数切换。

---

## 三、核心架构、Pydantic v2 契约与 `kpi_us_large_banks.json`

- **准入边界（D2）**：仅当 `sic ∈ [6020..6036]`，或 `sic == 6211` 且存在 `Deposits > 0`、`Loans > 0`、`Deposits / Assets >= 15%` 时激活 `active_sector_plugin = "us_large_bank_v4a"`；划为 `universal_commercial`、`investment_bank_bhc`、`custodian_or_broker_bank` 3 子类；严格排除 `AXP(6199)`、`BLK(6211)`、保险（`6300–6411`）、REIT（`6798`）。
- **HTM 联动升级红黄灯（D4）**：
  - 🔴 **RED**：`cet1_buffer_bps < 50` OR `provision_to_ppnr_pct >= 50.0` OR `(htm_loss_pct_of_tce >= 30.0 AND (cet1_buffer_bps < 100 OR deposits_qoq_pct <= -3.0))`。
  - 🟡 **YELLOW**：`cet1_buffer_bps < 100` OR `htm_loss_pct_of_tce >= 15.0` OR `efficiency_ratio_gaap_pct >= 68.0` OR `provision_to_ppnr_pct >= 30.0` OR `rotce_q_pct < 10.0`。
  - 🟢 **GREEN**：以上均未触发。
- **Gordon-P/TBV 相对估值锚（D5）**：`Implied Sustainable ROTCE = g + (P/TBV) × (K_e - g)`，`Fair P/TBV = (ROTCE - g) / (K_e - g)`，默认统一 `K_e = 10.0% / g = 3.0%`，必出 `K_e ∈ {9,10,11}% × g ∈ {2,3,4}%` 3×3 敏感性矩阵并支持 API 传参覆盖。
- **核心 Pydantic v2 契约**：`BankSegmentRow`、`BankSegmentSnapshot`、`TbvpsReconciliationBridge`、`BankDisclosureSnapshot`（持久仓 `BANK_DISCLOSURE_SNAPSHOTS`）、`BankDiagnosticFlag`、`BankDiagnosticsSummary`、`BankValuationResult`、`PeerBankMetrics` & `PeerComparisonMatrix`（可选默认兼容扩展）、`kpi_us_large_banks.json`（实测渲染 `4,328–4,436 / 4,500` 字符零截断）。

---

## 四、五批次施工路线与全量 Live 验收（D1 & D8）

1. **Batch 1**：`sec_deep.py`（R1/R2/R4b/`EX-99.1/2/3` 表格密度选优）+ `sec_edgar.py` & `research_peer_compare.py`（Projection v6 + `ratio` + 21+ 银行行）+ `main.py` 11 处 `ApiProblem` 零破坏收口 + 预算边界修复。
2. **Batch 2**：`app/fin_data/adapters/sec_bank_disclosure.py`（3 子类准入、`(Details)` 分部解析 R3–R7、三源融合与流式 10-Q MD&A 扫描）+ `BANK_DISCLOSURE_SNAPSHOTS` 仓储注册。
3. **Batch 3**：`v4a_bank_rules.json` + `app/research_bank_diagnostics.py`（5 支柱 + HTM 联动升级制 + Gordon 3×3 矩阵）+ `kpi_us_large_banks.json` + `research_methods/__init__.py`（`sector_plugin` + 修复 `GS → BLK` 缓存投毒）。
4. **Batch 4**：方向 A / V2 / V2.5 / **V3a（D6 0-LLM 银行分部优先 + 按需附注兜底）** / 方向 B（修复 `_earnings_delta_for_sub` 传空字典 + 雷达滞回）/ REST API（`POST /v2/research/banks/analyze`、`GET /v2/research/banks/segments`、`peers/compare` 与 `watch` 扩展）/ Agent 2 工具注册（**严禁改动 `src/apps/web/` 前端代码**）。
5. **Batch 5**：部署容器镜像 `foresightnine-api:<sha>`，运行 `eval_v4a_banks.py`（14 家实网 SEC 探针）+ **6 大行全量 Live 压测**（`JPM / BAC / WFC / C / GS / MS` 方向 A + V3a 分部核验 + V2 横评 + 方向 B 跨季雷达 + `BLK` 防污染对照 + 4 路并发内存 `< 550 MiB`），临时账号删净（Postgres 残留 `0` 行）。
