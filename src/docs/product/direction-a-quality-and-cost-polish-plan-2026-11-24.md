# 方向 A 首轮实测精修方案：降本提速、激活 TTM FCF / Reverse-DCF 与机构级交付打磨

- **日期**：2026-11-24
- **实测基准**：`topic_11e55b1df00649e8865baf53f2b7ddd9`（`sqrun_dc4a291985b6` / `dossier_f96450a4880d`，NVDA FY2027 Q3，`gpt-5.6-terra` on `openai_responses`，16 次调用 / 0 失败 / 0 修复重试）
- **目标**：在不损失当前买方分析深度与零幻觉纪律的前提下，系统解决实测暴露的 **5 项工程与体验短板**，将单次 Token 消耗压降 **50%–65%**，稳定激活美股 **TTM FCF 与 Reverse-DCF**，消除组织图状态误标、误杀有效 Non-GAAP 证据卡及报告“工程日志感”。

---

## 总览：5 项精修与预期收益

| 编号 | 优化项 | 实测基线问题 | 核心改造 | 预期量化效果 |
| --- | --- | --- | --- | --- |
| **Q1** | **Token 降本与提速** | 总消耗 **1,095,246 tokens**（3 个 Worker 占 884,902 input，Bear 占 119,801 input），耗时 ~3.8 min | 工具返回预算封顶 + Worker 历史旧工具结果滑动摘要压缩 + Bear 输入紧凑投影 | 总 Token 降至 **350K–480K（-55%~-65%）**，墙钟耗时缩短 **25%–35%** |
| **Q2** | **TTM FCF 与 Reverse-DCF 激活** | 实时 EV 已算出（\$4.02T），但 `TTM FCF is missing` 导致 Reverse-DCF 降级 | `sec_edgar.py` 增加美股现金流 YTD→QTD 确定性差分（`Q2=6M-Q1`, `Q3=9M-6M`, `Q4=FY-9M`）+ `TTM = FY + YTD_curr - YTD_prior` | 盈利美股 Reverse-DCF 激活率从 0% 提升至 **>90%**，100% Decimal 可复算 |
| **Q3** | **Worker 状态语义修复** | `worker_guidance`（4 卡）与 `worker_segments`（5 卡）正常交付，但因 `status="partial"` 被标成 `blocked` | 修复 `research_earnings_squad.py:5559` 状态映射，区分“有效交付但有外部缺口（`completed`）”与“真正阻断/零产出（`blocked`）” | 消除组织图误报红/黄阻断，状态与实际成果 100% 一致 |
| **Q4** | **引文门控防误杀 + 单 CIK EFTS 策略对齐** | `card_nongaap_bridge_and_cash_generation` 因表格空白/符号差异被整卡剔除；线上 `sec_fulltext_search` 被旧策略拦截 | `gate_evidence_cards` 增加表格分隔符/空白/智能标点规范化匹配；对齐线上 `FORESIGHTNINE_FIN_DATA_POLICY` 放开单 CIK `filing_search_cik` | 消除有效 Non-GAAP 调节卡误杀；`worker_forensics` 跨期附注全文检索正常可用 |
| **Q5** | **报告去“工程日志感” + 语言统一** | 正文暴露 `Gate 状态: exportable: true, passed: false`、`source_d5374c44...`、`[card_q3_...]`，Bear 卡片为英文 | 强制 Bear Reviewer 输出语言匹配用户提问；Writer 禁用内部 ID/Gate 原始字段，统一改用干净脚注 `[1]` + 人类可读来源名（前端可点击穿透 `ReaderPanel`） | 达到真正机构级买方备忘录/深研报告阅读质感 |

---

## 逐项具体改造方案

### Q1. Token 降本与提速（预期压降 55%–65% Token）

#### 根因定位
1. **Worker 多轮上下文平方级膨胀**：`research_earnings_squad.py` 中每个 Worker 执行 4–5 轮工具调用（`sec_filing_tables`、`read_sources`、`sec_filing_sections`、`sec_redline_diff`），早期轮次返回的大段表格/HTML 清洗文本原封不动保留在后续每一轮 `messages` 中，导致 3 个 Worker 累计 12 次调用消耗了 **884,902 input tokens**。
2. **Bear Reviewer 输入冗余**：`build_isolated_bear_prompt`（`research_earnings_squad.py:2066`）把整份 `snapshot`（含 70+ 条 `filing_table_catalog`、长段落 `earnings_release` 原文片段、全量 `statement_periods`）原样 JSON 序列化喂给空头审查，导致单次调用高达 **119,801 input tokens**。

#### 具体改造
1. **工具返回紧凑化（`FinToolboxToolRunner`）**：
   - `sec_filing_tables`：单表最大返回行数默认封顶（如 80 行）+ 字符上限（`<= 12,000` chars），超长附注表自动返回表头 + 关键汇总行 + 截断偏移提示；
   - `read_sources` / `sec_filing_sections`：单次调用硬上限 `14,000` chars（引导模型先 `search_source` / `outline_source` 精准定位再读）；
   - `sec_redline_diff`：只返回实质 `added` / `removed` / `revised` 段落（每类上限 12 段、每段上限 600 chars），不回传未变动上下文。
2. **Worker 轮次间滑动摘要压缩（`_execute_worker_task`）**：
   - 当 `turn_index >= 2` 时，对 **2 轮以前** 的只读工具返回内容（`role == "tool"`）做确定性离线裁剪：保留 `status`、`source_id`、表名/章节名、前 600 字符摘要与已提取关键数字，把 10,000+ 字符的旧原文正文替换为 `[compacted: already inspected in earlier turn; source_id=...]`（原文仍完整保存在 `source_texts` 中供引文门控核验，不影响证据完整性）。
3. **Bear Reviewer 输入瘦身（`build_isolated_bear_prompt`）**：
   - 仅传入紧凑投影：`sector_applicability`、全部 Red/Yellow `flags`（含 `flag_id` 与数值）、核心估值与 Reverse-DCF 摘要、精简 `redline_summary`（仅实质变动标题与片段）、`open_gaps`、所有已产出的 `EvidenceCard`（摘要化，去除重复元数据）以及 `valid_anchors` 列表；**剔除** 70+ 条 `filing_table_catalog` 目录和原始大段 `earnings_release` 文本。
   - 预计 `bear_reviewer` 单次 input 从 `120K` 降至 **`18K–28K`（-75%+）**。

---

### Q2. 补齐美股现金流 YTD → QTD 差分，稳定激活 TTM FCF 与 Reverse-DCF

#### 根因定位
- 美股 SEC 10-Q 现金流量表（`operating_cash_flow`、`capex`、`sbc` 等流量科目）在 Q2（6 个月）与 Q3（9 个月）中通常**只披露年初至今累计值（YTD：~180 天 / ~270 天）**，不单独披露 3 个月单季值。
- 当前 `sec_edgar.py` 的 `classify_duration` / `last_n_quarters` 仅识别 ~90 天单季记录与 `FY - (Q1+Q2+Q3)` 的 Q4 推导，导致 Q2/Q3 现金流单季缺失 → 无法凑齐连续 4 季单季 FCF → `derive_period_metrics` 算不出 `TTM FCF` → `valuation_and_implied_expectations` 的 Reverse-DCF 报 `TTM FCF is missing`。

#### 具体改造（`src/apps/api/app/fin_data/adapters/sec_edgar.py` + `research_earnings_squad.py`）
1. **增加确定性 YTD → QTD 现金流差分器（`derive_quarterly_from_ytd`）**：
   - 对同一财年内同单位/同币种的累计流量项（`operating_cash_flow`、`capex`、`sbc`、`buybacks`、`dividends`）：
     - `Q1` = 3 个月披露值（`~90d`）
     - `Q2` = `6M_YTD - Q1`（`derived=True`, `derivation="6M YTD minus Q1"`）
     - `Q3` = `9M_YTD - 6M_YTD`（`derived=True`, `derivation="9M YTD minus 6M YTD"`）
     - `Q4` = `FY - 9M_YTD`（`derived=True`, `derivation="FY minus 9M YTD"`）
2. **增加直接 TTM 流量公式作为二重保底（`_derive_ttm_rows`）**：
   - 当某最新期间缺少单季拆解但具备“最近完整财年 `FY` + 本财年 `YTD_curr` + 上财年同期 `YTD_prior`”时，直接用标准会计恒等式确定性算出：
     $$\text{TTM} = \text{FY}_{\text{latest}} + \text{YTD}_{\text{curr}} - \text{YTD}_{\text{prior}}$$
   - 标注 `derived=True` 与精确 `derivation` 字符串及输入 XBRL `accession` 溯源。
3. **验证目标**：对 NVDA（FY27 Q2 10-Q + FY26 10-K）、MSFT、META、WMT、LLY 5 家真实美股 SEC 实时数据跑通 `TTM OCF`、`TTM CapEx`、`TTM FCF`、`FCF ex-SBC` 及 **Reverse-DCF 5 年隐含 FCF CAGR + 3×3 敏感性矩阵**。

---

### Q3. 修复 Worker 完成态误标 `blocked`（组织图状态语义精准化）

#### 根因定位
- `research_earnings_squad.py:5559`：
  ```python
  runtime_state="completed" if batch.status == "completed" else "blocked"
  ```
- 当外部数据（如 FMP 一致预期、电话会纪要或尚未发布的 Q3 10-Q）客观缺失时，Worker 按规范将 `batch.status` 设为 `"partial"` 并诚实记录 `unresolved_gaps`，同时成功交付了 4–5 张高质量证据卡。上述代码把所有 `"partial"` 一刀切映射成 `"blocked"`，导致前端组织图显示受阻图标。

#### 具体改造
1. **精准映射 `runtime_state`**（`research_earnings_squad.py`）：
   - `batch.status == "completed"` 或 (`batch.status == "partial"` 且 `len(batch.cards) > 0` 且无致命阻断) → `runtime_state = "completed"`（在 `current_activity` 中清晰写明“已产出 N 张证据卡（含 M 项外部资料缺口）”）；
   - 仅当 `batch.status == "failed"` 或 (`len(batch.cards) == 0` 且存在 `blocked_tool_attempts` / 硬阻断) 时才标 `runtime_state = "blocked"` 或 `"failed"`。
2. **同步检查 `OrgGraphPanel.tsx`**：确保局部缺口不会把正常交付的圆球标红，真正失败/阻断时才显示红色错误徽章。

---

### Q4. 引文门控防误杀 + 线上单 CIK `EFTS` 策略放开

#### 根因定位
1. **Non-GAAP 调节卡误杀**：`gate_evidence_cards`（`research_earnings_squad.py:2374`）对 8-K 表格抽取出的 `quotation` 做近乎逐字符精确匹配；8-K 业绩新闻稿表格在 `strip_html_preserving_tables` 中常含多余空格、竖线 `|`、千分位空格（`$ 115,800` vs `$115,800`）、全半角标点或破折号（`–` / `—` / `-`），导致模型引用真实表格数字时被误判为“引文未核验”而整卡剔除。
2. **线上 `sec_fulltext_search` 被拒**：线上容器设置了外部 `FORESIGHTNINE_FIN_DATA_POLICY` 配置，未包含 A1 新增的 `sec_edgar/filing_search_cik` 规则。

#### 具体改造
1. **规范化引文匹配器（`gate_evidence_cards`）**：
   - 在保持严格原文存在性核验（绝不放行编造引文）的前提下，增加二级规范化比对：
     - 合并连续空白/换行与表格分隔符 `|` 为单空格；
     - 统一常见排版符号（弯引号 `“”‘’` → `""''`、长短破折号 `—–` → `-`、货币与数字间空格 `$ 115` → `$115`）；
     - 若规范化子串精确命中原文，判为核验通过并记录 `match_mode="normalized_exact"`。
   - 若单张卡有多条引文、其中 1 条引文格式瑕疵但其余核心引文全部核验通过，**剔除未核验引文并降级标注，而非整卡抹杀**（仅当一张卡 0 条有效核验引文时才整卡剔除）。
2. **单 CIK `EFTS` 策略对齐（`fin_data/policy.py` + 部署环境）**：
   - 检查 `load_policy()` 加载 `FORESIGHTNINE_FIN_DATA_POLICY` 时的合并逻辑：若自定义策略允许 `sec_edgar/filings` 且未显式拒绝 `sec_edgar/filing_search_cik`，默认继承安全的单 CIK `filing_search_cik`（`export=True`）；全局无范围 `filing_search` 依然保持默认拒绝。

---

### Q5. 报告去“工程日志感” + 语言完全统一

#### 根因定位
- `build_isolated_bear_prompt` 未显式锁定输出语言跟随 `request.question`，导致中文提问下 `BearCaseItem` 结构化文本产出英文；
- `_build_writer_system_prompt` 把内部 `card_id`、`source_id`、`gate_summary` 原始布尔值（`exportable: true, passed: false`）直接写进正文，影响专业投资者阅读体验。

#### 具体改造
1. **全链路语言锁定**：
   - 在 Stage 1 / Stage 2 / Stage 2.5 / **Stage 3A Bear Reviewer** / Stage 3B Writer（Memo & Deep Report）中统一注入 `output_language`（根据 `request.question` 自动识别中文/英文）：
     - 中文提问时，Bear Case 的 `thesis_challenge`、`next_quarter_invalidation_trigger` 以及 Memo/Deep Report 全部用自然专业的中文金融术语输出（保留必要的英文专有指标缩写如 `GAAP`、`Non-GAAP`、`DSO`、`DIO`、`CCC`、`Reverse-DCF`、`RPO`）。
2. **去工程 ID 化与机构级脚注映射**：
   - Writer Prompt 明确禁止在面向用户的报告正文中输出 `Gate 状态: exportable: true, passed: false`、`source_d5374c44...`、`[card_q3_...]` 等内部标识符；
   - 由后端确定性构建一份 **人类可读来源编号表**（例如 `[1] NVDA FY2027 Q3 8-K EX-99.1 (2026-11-17)`、`[2] NVDA FY2027 Q2 10-Q (2026-07-26)`、`[3] SEC XBRL 财务数据 (CIK 0001045810)`），注入 Writer 作为唯一引文标注格式；
   - 门控剔除的未核验信息统一用自然中文简述在文末“研究边界与未核验项”中（如“部分 Non-GAAP 桥接明细因原文定位未完全核验，本报告未采信相关推论”），不再暴露内部 JSON 字段名。
   - 前端 `EarningsDossierView.tsx` 同步把 `[1]` 脚注和 `BearCase` 锚点标签美化为人类可读名称，同时保留点击穿透至 `ReaderPanel` 原文的功能。

---

## 实施与验收计划

| 步骤 | 实施范围 | 验收标准 |
| --- | --- | --- |
| **Step 1（Q2 + Q4 策略）** | `sec_edgar.py` YTD→QTD 差分 + TTM 恒等式；`policy.py` 单 CIK `filing_search_cik` 继承 | 实时 SEC 验证 NVDA/MSFT/META/WMT/LLY 5 家公司全部产出 `TTM FCF` 并成功算出 Reverse-DCF；`sec_fulltext_search` 在单 CIK 下不再被误拒 |
| **Step 2（Q1 + Q3 + Q4 门控 + Q5 后端）** | `research_earnings_squad.py`：工具上限 + Worker 历史滑动压缩 + Bear 紧凑投影 + `partial` 状态修复 + 引文规范化匹配 + 语言锁定与人类可读脚注表 | 单元测试全绿（含 8-K 表格空白/破折号引文回归测试、YTD 差分测试、Bear 中文输出测试、Token 体积缩减断言） |
| **Step 3（Q5 前端 + 回归）** | `EarningsDossierView.tsx` / `OrgGraphPanel.tsx` 人类可读锚点与状态微调 + 全量 API/Web 测试与构建 | Web `tsc` + `vitest` + `vite build` 全绿 |
| **Step 4（提交、推送、上线 + 实测对比）** | 提交 `master`、推送 `origin/master`、`aws-runtime` 上线；复跑 NVDA 财报并对比优化前后 **Token 消耗、Reverse-DCF 激活、组织图状态、Non-GAAP 保留率与报告可读性** | 线上冒烟全绿，实测指标显著优于 `sqrun_dc4a291985b6` 基线 |
