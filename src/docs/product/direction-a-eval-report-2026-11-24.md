# Direction A Phase A4 Evaluation Report: US Earnings Golden Eval Suite, Controlled Benchmark & Live SEC EDGAR Validation

- **Date**: 2026-11-24
- **Scope**: Phase A4 verification of Direction A (US Single-Company Buy-Side Earnings Squad) across 24 deterministic golden tasks, controlled Scheme 2 vs. Single-Loop Serial Baseline benchmark, and read-only live multi-sector SEC EDGAR real-data validation.
- **Compliance & Experiment Discipline (`AGENTS.md`)**:
  - Controlled benchmark fixes identical fixtures, tool payloads, simulated I/O latency (`25.0 ms` per HTTP/tool call), and simulated model turn latency (`25.0 ms` per model invocation) across both arms.
  - **Live LLM Credentials Status**: Checked environment variables (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `FORESIGHTNINE_OPENAI_API_KEY`, `FORESIGHTNINE_ANTHROPIC_API_KEY`). **None are set** (`live_llm_ab_status = "pending_external_model_credentials"`). Controlled live LLM A/B runs remain pending external model credentials; zero unexecuted live LLM results are claimed or fabricated.
  - **Release Boundary**: This report records deterministic A4 benchmark and live SEC EDGAR parser/diagnostics evidence; it does not constitute public release authorization.

---

## 1. Deliverables & File Inventory

| Artifact | Path | Purpose |
| --- | --- | --- |
| 24-Task Golden Eval Set | `src/apps/api/tests/evals/direction_a_golden_eval_set.json` | 20 core non-financial US earnings tasks (5 L2 sectors × 4) + 2 Financial/REIT SIC boundaries + 2 degradation/redaction cases |
| Controlled Benchmark & Live Validator | `src/apps/api/app/research_earnings_eval.py` | Dataset schema, offline latency-controlled Scheme 2 vs. Serial Baseline harness, 4-dim grader, `.xlsx` XML verifier, paced live SEC EDGAR validator, LLM credential detector |
| Pytest Verification Suite | `src/apps/api/tests/test_a4_golden_eval.py` | Automated tests for all 24 tasks, Q4 derivation, bear-anchor gate, SIC gating, partial survival, `export=False` redaction, rate pacing (`<= 2.5 req/s`), and opt-in live SEC EDGAR validation |
| Evaluation Report | `src/docs/product/direction-a-eval-report-2026-11-24.md` | Full quantitative benchmark results, live SEC validation findings, and actionable defects discovered |

---

## 2. 24-Task Golden Evaluation Suite Coverage

| Bucket / Category | Count | Tickers & SEC SIC | Key Evaluation Traps Covered |
| --- | --- | --- | --- |
| **SaaS & Cloud (`kpi_saas_cloud`)** | 4 | `MSFT` (7372), `CRM` (7372), `NOW` (7372), `SNOW` (7372) | Non-calendar FY (`0630`, `0131`), Q4 single-quarter derivation (`SNOW`), cRPO/RPO, Non-GAAP SBC + restructuring ties, `SBC >= FCF` red flag, unanchored bear-case rejection trap |
| **Semis & Hardware (`kpi_semis_hardware`)** | 4 | `NVDA` (3674), `AAPL` (3571), `AMD` (3674), `AVGO` (3674) | Non-calendar FY (`0131`, `0925`, `1101`), Q4 derivation (`NVDA`, `AVGO`), Item 1A export/tariff Redline diffs, Xilinx acquisition amortization uplift tie, DIO/inventory divergence (`+15pp`), buyback payout >100% FCF |
| **Internet & Ads (`kpi_internet_ads`)** | 4 | `GOOGL` (7370), `META` (7370), `AMZN` (5961*), `NFLX` (7841) | TAC % & AI CapEx FCF compression, Critical Accounting server useful-life Redline (5y→6y), impressions vs. price-per-ad, Lead L2 override (`AMZN` SIC `5961` → `kpi_internet_ads` for AWS/Ads) |
| **Consumer & Retail (`kpi_consumer_retail`)** | 4 | `WMT` (5331), `COST` (5331), `NKE` (3021), `SBUX` (5810) | Non-calendar FY (`0131`, `0830`, `0531`, `0928`), Q4 derivation (`WMT`), SSSG traffic vs. ticket, negative CCC, `-443 bps` GM compression & inventory buildup (`NKE`), guidance cut, dividend payout >120% FCF |
| **Pharma & Biotech (`kpi_pharma_biotech`)** | 4 | `LLY` (2834), `JNJ` (2834), `MRK` (2834), `VRTX` (2834) | Product concentration & LOE cliff (`Stelara`, `Keytruda` 47%), Acquired IPR&D Non-GAAP bridges, talc litigation Redline, **negative TTM FCF Reverse-DCF degradation (`VRTX` → `unavailable`)** |
| **Financial / REIT SIC Boundaries** | 2 | `JPM` (6021 Bank), `PLD` (6798 REIT) | `evaluate_sec_sic_gate` disables gross margin, inventory/DIO/DPO/CCC, standard FCF, EV/EBITDA, and Reverse-DCF (`disabled_by_sector`), `selected_l2_pack_id=None`, recommends NIM/CET1 and FFO/AFFO/SSNOI |
| **Degradation & Redaction Cases** | 2 | `NOW` (7372), `CRM` (7372) | Missing FMP + simulated `worker_guidance` failure (partial card survival + `open_gaps`); BYOK FMP (`export=False`) + private note (`export=False`) redaction across Markdown, CSV, and 6-sheet `.xlsx` |

*\*Note on `AMZN` (`0001018724`)*: Real SEC submissions classify `AMZN` under SIC `5961` (`Retail-Catalog & Mail-Order Houses`), which maps deterministically to `kpi_consumer_retail` by SIC alone; `internet_03_amzn` explicitly validates Stage 1 Lead Coordinator L2 selection (`lead_l2_override="kpi_internet_ads"`) when researching AWS/Advertising.

---

## 3. Controlled Benchmark Results: Scheme 2 Buy-Side Squad vs. Single-Loop Serial Baseline

**Experimental Conditions**: Identical 24-task fixtures (`direction_a_golden_eval_set.json`), deterministic scripted multi-slot handlers (`build_scheme2_model_runner`), `tool_latency_ms = 25.0`, `model_latency_ms = 25.0`, executed end-to-end through `EarningsSquadOrchestrator` (parallel Stage 0 `ThreadPoolExecutor` + parallel Stage 2 workers + isolated Stage 3A `build_isolated_bear_prompt` + Stage 3B server gates + `render_deep_report` cache + `export_earnings_dossier`) vs. `run_serial_baseline_task`.

### 3.1 Summary Metrics Comparison

| Metric | Scheme 2 Buy-Side Squad | Single-Loop Serial Baseline | Delta / Ratio |
| --- | --- | --- | --- |
| **Dim 1: Transport & Partial Survival** | **24 / 24 (100.0%)** | 23 / 24 (95.83%) | +4.17 pp |
| **Dim 2: Decimal / Accounting / SIC / Redaction Accuracy** | **24 / 24 (100.0%)** | 19 / 24 (79.17%) | +20.83 pp |
| **Dim 3: Citation & Hard-Anchored Bear Rate** | **24 / 24 (100.0%)** | 0 / 24 (0.0%) | +100.0 pp |
| **Dim 4: Delivery Completeness (Locked Dossier + Exports)** | **24 / 24 (100.0%)** | 0 / 24 (0.0%) | +100.0 pp |
| **All-4-Dimensions Pass Rate** | **24 / 24 (100.0%)** | **0 / 24 (0.0%)** | **+100.0 pp** |
| **Total Wall-Clock Time (24 Tasks)** | **10,100.564 ms** | 15,186.865 ms | **1.50x speedup** |
| **Mean Wall-Clock Time / Task** | **420.857 ms** | 632.786 ms | **-33.49% latency** |
| **Critical-Path Sequential Model Turns / Task** | **7.0** | 12.0 | **-41.67% turns** |
| **Total Model Invocations / Task (incl. all parallel workers + Deep Report)** | **10.96** | 12.00 | **-8.67% total calls** |
| **Stage 0 Model Calls (24 Tasks Total)** | **0** | 288 | **-100% Stage 0 model cost** |
| **Mean Scripted Tokens / Task** | **7,031.0** | 17,560.0 | **-59.96% tokens** |
| **Deep Report 0-Retrieval Cache Hit Rate** | **100.0% (24 / 24)** | 0.0% (0 / 24) | +100.0 pp |
| **6-Sheet `.xlsx` / MD / CSV Export Validity Rate** | **100.0% (24 / 24)** | 0.0% (0 / 24) | +100.0 pp |
| **Export Redaction Integrity (`export=False`)** | **100.0% (24 / 24)** | 91.67% (22 / 24) | +8.33 pp |

### 3.2 Per-Task Controlled Benchmark Breakdown (`Scheme 2`)

| Task ID | Ticker | Sector / L2 Pack | Wall-Clock (ms) | Turns (Total / Crit-Path) | Flags (R/Y/G) | Reverse-DCF Status / 5Y FCF CAGR | Bears (Acc / Rej) | 4-Dim Pass |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `saas_01_msft` | `MSFT` | `kpi_saas_cloud` | 413.5 | 11 / 7 | 0R / 2Y / 8G | `computed` (`29.99%`) | 3 / 0 | PASS |
| `saas_02_crm` | `CRM` | `kpi_saas_cloud` | 407.2 | 11 / 7 | 1R / 1Y / 7G | `computed` (`4.05%`) | 3 / 0 | PASS |
| `saas_03_now` | `NOW` | `kpi_saas_cloud` | 406.1 | 11 / 7 | 1R / 1Y / 8G | `computed` (`26.05%`) | 3 / 0 | PASS |
| `saas_04_snow` | `SNOW` | `kpi_saas_cloud` | 412.2 | 11 / 7 | 4R / 1Y / 5G | `computed` (`38.17%`) | 3 / 1 | PASS |
| `semis_01_nvda` | `NVDA` | `kpi_semis_hardware` | 437.2 | 11 / 7 | 3R / 1Y / 7G | `computed` (`31.32%`) | 3 / 0 | PASS |
| `semis_02_aapl` | `AAPL` | `kpi_semis_hardware` | 407.8 | 11 / 7 | 1R / 3Y / 8G | `computed` (`21.36%`) | 3 / 0 | PASS |
| `semis_03_amd` | `AMD` | `kpi_semis_hardware` | 564.3 | 11 / 7 | 1R / 2Y / 8G | `computed` (`37.86%`) | 3 / 0 | PASS |
| `semis_04_avgo` | `AVGO` | `kpi_semis_hardware` | 408.1 | 11 / 7 | 0R / 3Y / 8G | `computed` (`26.50%`) | 3 / 0 | PASS |
| `internet_01_googl` | `GOOGL` | `kpi_internet_ads` | 413.6 | 11 / 7 | 3R / 1Y / 8G | `computed` (`17.33%`) | 3 / 0 | PASS |
| `internet_02_meta` | `META` | `kpi_internet_ads` | 464.5 | 11 / 7 | 2R / 3Y / 7G | `computed` (`19.81%`) | 3 / 0 | PASS |
| `internet_03_amzn` | `AMZN` | `kpi_internet_ads` | 406.5 | 11 / 7 | 2R / 1Y / 7G | `computed` (`62.84%`) | 3 / 0 | PASS |
| `internet_04_nflx` | `NFLX` | `kpi_internet_ads` | 404.0 | 11 / 7 | 0R / 1Y / 10G | `computed` (`28.68%`) | 3 / 0 | PASS |
| `retail_01_wmt` | `WMT` | `kpi_consumer_retail` | 407.4 | 11 / 7 | 1R / 1Y / 9G | `computed` (`34.41%`) | 3 / 0 | PASS |
| `retail_02_cost` | `COST` | `kpi_consumer_retail` | 406.0 | 11 / 7 | 0R / 1Y / 10G | `computed` (`29.73%`) | 3 / 0 | PASS |
| `retail_03_nke` | `NKE` | `kpi_consumer_retail` | 405.5 | 11 / 7 | 4R / 2Y / 7G | `computed` (`22.42%`) | 3 / 0 | PASS |
| `retail_04_sbux` | `SBUX` | `kpi_consumer_retail` | 414.3 | 11 / 7 | 4R / 3Y / 4G | `computed` (`26.03%`) | 3 / 0 | PASS |
| `pharma_01_lly` | `LLY` | `kpi_pharma_biotech` | 438.0 | 11 / 7 | 0R / 3Y / 9G | `computed` (`42.65%`) | 3 / 0 | PASS |
| `pharma_02_jnj` | `JNJ` | `kpi_pharma_biotech` | 407.3 | 11 / 7 | 0R / 0Y / 11G | `computed` (`6.47%`) | 2 / 0 | PASS |
| `pharma_03_mrk` | `MRK` | `kpi_pharma_biotech` | 414.9 | 11 / 7 | 2R / 0Y / 10G | `computed` (`3.17%`) | 3 / 0 | PASS |
| `pharma_04_vrtx` | `VRTX` | `kpi_pharma_biotech` | 441.5 | 11 / 7 | 3R / 2Y / 4G | `unavailable` (TTM FCF <= 0) | 2 / 0 | PASS |
| `boundary_01_jpm` | `JPM` | `bank` (`None`) | 398.3 | 11 / 7 | 0R / 0Y / 1G | `disabled_by_sector` | 1 / 0 | PASS |
| `boundary_02_pld` | `PLD` | `reit` (`None`) | 406.5 | 11 / 7 | 0R / 1Y / 1G | `disabled_by_sector` | 2 / 0 | PASS |
| `degrade_01_fmp_missing_worker_fail` | `NOW` | `kpi_saas_cloud` | 402.2 | 10 / 7 | 1R / 1Y / 8G | `computed` (`26.05%`) | 3 / 0 | PASS |
| `degrade_02_export_false_redaction` | `CRM` | `kpi_saas_cloud` | 413.6 | 11 / 7 | 1R / 1Y / 7G | `computed` (`4.05%`) | 3 / 0 | PASS |

- **Q4 Derivation Verification (`select_line(..., derive_q4=True)`)**: `4 / 4` passed (`SNOW`, `NVDA`, `AVGO`, `WMT`).
- **Unanchored Bear Gate (`saas_04_snow`)**: Injected generic macro platitude (`bear_generic_trap_saas_04_snow`) was cleanly rejected (`rejected_bear_count = 1`) while retaining 3 evidence-anchored bear cases.
- **Export Redaction (`degrade_02_export_false_redaction`)**: Zero leaks of `BYOK_CFO_PRIVATE_NAME`, `84321.55`, or `PRIVATE_CHANNEL_CHECK_99412` across Markdown, CSV, and OpenXML `.xlsx` (`sharedStrings.xml` + all 6 worksheet XMLs).

---

## 4. Live Multi-Sector SEC EDGAR Real-Data Validation (Post-Fix)

- **Request Policy**: Read-only public SEC EDGAR endpoints (`data.sec.gov/submissions`, `data.sec.gov/api/xbrl/companyfacts`, `www.sec.gov/Archives/edgar/data`) with declared `User-Agent: ForesightNine-EvalA4/1.0 contact@foresightnine.local` and `PacingSecRateLimiter` capped at `<= 2.4 req/s`.
- **Aggregate Live Execution (Post-Fix)**: **7 / 7 tickers (`status=ok`, `quirks=[]`)**, **70 total HTTP requests**, **32,225.1 ms total elapsed**, **2.17 req/s overall** (strictly `<= 2.5 req/s`).

| Ticker | CIK | SIC / Category / Gated | Matched L2 Pack | Latest Filing (`10-Q`) | FilingSummary Reports | Parsed `R*.htm` Table | Dual-Period 8-K Ex-99.1 (Current / Prior; Guid / Non-GAAP) | XBRL Periods & Latest Revenue / NI | Flags (R/Y/G) | `.xlsx` Bytes (6 Sheets) | Reqs / Time (ms) / Rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `MSFT` | `0000789019` | `7372` / `non_financial` / `False` | `kpi_saas_cloud` | `0001193125-26-436133` (`2026-10-28`) | 85 | `R20.htm` (5×1, USD millions) | `...-436122` / `...-323632` (1 / 5) | 6 periods (`2027-Q1`: Rev `$92,279M`, NI `$36,629M`, FCF `$18,633M`) | R1 / Y2 / G7 | 20,849 B | 8 / 3,351.8 ms / 2.39 req/s |
| `NVDA` | `0001045810` | `3674` / `non_financial` / `False` | `kpi_semis_hardware` | `0001045810-26-000075` (`2026-08-26`) | 70 | `R20.htm` (17×6, USD) | `...-000088` / `...-000073` (7 / 17) | 6 periods (`2027-Q2`: Rev `$96,221M`, NI `$59,688M`) | R3 / Y0 / G4 | 22,473 B | 12 / 4,963.9 ms / 2.42 req/s |
| `META` | `0001326801` | `7370` / `non_financial` / `False` | `kpi_internet_ads` | `0001628280-26-066773` (`2026-10-22`) | 64 | `R10.htm` (6×8, USD) | `...-066700` / `...-050596` (1 / 15) | 6 periods (`2026-Q3`: Rev `$64,601M`, NI `$9,860M`) | R2 / Y1 / G3 | 23,246 B | 8 / 3,252.2 ms / 2.46 req/s |
| `WMT` | `0000104169` | `5331` / `non_financial` / `False` | `kpi_consumer_retail` | `0000104169-26-000154` (`2026-08-28`) | 35 | `R13.htm` (36×8, USD) | `...-000168` / `...-000145` (7 / 32) | 6 periods (`2027-Q2`: Rev `$187,937M`, NI `$6,366M`) | R1 / Y0 / G7 | 22,552 B | 12 / 5,069.8 ms / 2.37 req/s |
| `LLY` | `0000059478` | `2834` / `non_financial` / `False` | `kpi_pharma_biotech` | `0000059478-26-000108` (`2026-10-29`) | 50 | `R8.htm` (4×8, USD) | `...-000104` / `...-000077` (5 / 17) | 6 periods (`2026-Q3`: Rev `$24,397M`, NI `$6,451M`) | R2 / Y0 / G3 | 24,616 B | 10 / 4,190.5 ms / 2.39 req/s |
| `JPM` | `0000019617` | `6021` / `bank` / **`True`** | **`None`** | `0001628280-26-069523` (`2026-11-03`) | 166 | `R13.htm` (7×8, USD) | `...-065582` / `...-048078` (2 / 11) | 6 periods (`2026-Q3`: Rev `None` bank tags, NI `$15,305M`) | R0 / Y0 / G1 | 19,131 B | 10 / 7,318.9 ms / 1.37 req/s |
| `PLD` | `0001045609` | `6798` / `reit` / **`True`** | **`None`** | `0001193125-26-436160` (`2026-10-28`) | 79 | `R23.htm` (54×8, USD) | `...-422375` / `...-305416` (15 / 3) | 6 periods (`2026-Q1`: Rev `$2,297.7M`, NI `$982.0M`, OCF `$1,288.1M`) | R0 / Y0 / G2 | 14,691 B | 10 / 4,058.4 ms / 2.46 req/s |

---

## 5. Real-Data Defects Caught, Fixed & Verified

Initial live SEC EDGAR validation and `export=False` testing surfaced 3 upstream defects (all fixed and covered by targeted regression tests) plus 1 export defense-in-depth note:

1. **FIXED — `sec_deep.py:1500` (`-index.htm` vs. `-index.html` in `sec_earnings_release`)**: Real SEC `index.json` directories for `NVDA` (`0001045810-26-000088`), `WMT` (`0000104169-26-000168`), and `LLY` (`0000059478-26-000104`) list `*-index.html` and store `EX-99.1` under issuer filenames (`q3fy27pr.htm`, `earningsreleasefy27q3.htm`, `q326lillysalesandearningsp.htm`). Updated `_identify_exhibits_from_index_json` to match `("-index.htm", "-index.html")`; verified by `test_sec_earnings_release_resolves_dot_html_index_page` in `test_sec_deep.py` and live SEC extraction (`NVDA` 7 guidance / 17 Non-GAAP, `WMT` 7 / 32, `LLY` 5 / 17).
2. **FIXED — `research_earnings_squad.py:1592-1657` (`_fetch_sec_submissions_meta` `rows[:30]` truncation)**: Slicing newest 30 rows across all SEC forms dropped `10-Q`/`10-K` filings on active filers (`META` at index `55`, `WMT` at index `55`, `JPM` at index `1225` due to Form `4` and `424B2` prospectuses). Updated `_fetch_sec_submissions_meta` to filter and retain up to 15 periodic (`10-Q`/`10-K`/`20-F`/`40-F`) and up to 15 earnings (`8-K`/`6-K`) filings separately before merging; verified by `test_fetch_sec_submissions_meta_and_build_periods_from_sec_regressions` in `test_research_earnings_squad.py` and live `META`/`WMT`/`JPM` runs (`quirks=[]`).
3. **FIXED — `research_earnings_squad.py:1680-1735` (`_build_periods_from_sec` argument order & `derived_metrics` dict)**: Fixed swapped positional args to `period_financials_from_statement_lines(grp, pend, period_label=label, period_type="quarter")`, grouped by `period_end` so `2026-Q3` flow rows and `2026-FY` balance-sheet rows merge cleanly, added `source_too_large` per-concept fallback, and converted `derive_period_metrics(lines)` (`list[StatementLine]`) into a strict `dict[str, Any]` for `EarningsPrefetchSnapshot.derived_metrics`; verified by `test_fetch_sec_submissions_meta_and_build_periods_from_sec_regressions`.
4. **Hardening Note — `research_earnings_export.py:397-413` (mixed-citation cards with `export=False`)**: If a single `EvidenceCard` mixes an `export=True` citation (e.g., 8-K) and an `export=False` citation (e.g., BYOK transcript) while quoting the restricted source inside `card.finding`, the partial-redaction branch drops the restricted citation but retains `card.finding` verbatim. Keeping one source provenance class per `EvidenceCard` prevents leakage cleanly.

---

## 6. Test Execution Summary & Stage B Readiness

- **Full A1 + A2 + A4 Regression Matrix (`FORESIGHTNINE_RUN_LIVE_SEC_EVAL=1`)**:
  Executed `pytest -v` across all 9 test modules (`test_sec_deep.py` 14, `test_fmp_earnings_data.py` 14, `test_research_earnings_diagnostics.py` 12, `test_a1_tools_wiring.py` 6, `test_a2_method_packs.py` 56, `test_research_model_slots.py` 9, `test_research_earnings_squad.py` 14, `test_a2_squad_api_and_export.py` 11, `test_a4_golden_eval.py` 9 including live 7-ticker SEC EDGAR validation): **145 passed, 0 failed in 60.36s**.
- **Remaining Stage B Prerequisite**: Live multi-slot LLM A/B experiments (`fast_extractor`, `deep_reasoner`, `bear_reviewer`, `writer` vs. legacy single coordinator and general AI + search under frozen budgets) remain **pending external model API credentials**.
