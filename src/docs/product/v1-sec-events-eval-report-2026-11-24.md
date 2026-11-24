# V1 美股重大 SEC 事件、内部人交易、举牌与 FPI 6-K 引擎 — 平台模型实测验收报告

- **日期**：2026-11-24
- **部署版本**：`foresightnine-api:e7b7fde`（`master` @ `e7b7fde`，Healthy）
- **实测模型（强制平台模型，非 BYOK）**：
  - `deep_reasoner`：`glm-5.3-flash`（`protocol=anthropic_messages`，`personal_model=False`）
  - `bear_reviewer`（Skeptic）：`glm-5.3-flash`（`personal_model=False`）
- **数据源**：真实 SEC EDGAR（`data.sec.gov` + `www.sec.gov/Archives` + `efts.sec.gov`）
- **规格基线**：[v1-sec-events-insider-ownership-design-2026-11-24.md](v1-sec-events-insider-ownership-design-2026-11-24.md)

---

## 1. 总体实测结论（15 真实美股 + FPI 案例）

| 指标 | 实测结果 |
| --- | --- |
| 案例完成率 | **15 / 15**（`0` 未捕获异常） |
| 扫描 SEC 原始申报总数 | **950 份** |
| 失败申报数（`failed_accessions`） | **0** |
| 遗留缺口数（`open_gaps`） | **0** |
| Layer 1 确定性零 LLM 压制数 | **282 份**（10b5-1 / 非公开市场代码 / 现金less行权 / 尘埃门槛 / 财报分流 / 6-K 例行通知 / 资管主体非标的 13G） |
| 进入 Layer 2 候选总数 / 覆盖率 | **56 / 56（100.0%）** |
| 最终 Disposition 分布 | `push_alert`: **26** / `log_only`: **27** / `suppress`: **3** |
| 逐字引文校验通过率（`verified_quotes`） | **190 / 190（100.0%）** |
| 同空间否定/限定语境预警（`neg_warn`） | **8**（触发 `needs_review` 复核提示） |
| Skeptic 独立复核异议/降级（`skeptic_dissent`） | **5**（META / TSLA / JPM / TSM / BABA） |
| 5 大历史陷阱案例关键事件召回率 | **5 / 5（100.0%）** |

---

## 2. 15 案例逐项实测明细

| # | 案例 ID | 类别标签 | 扫描 | L1 压制 | L2 候选 | 信号分布 (`push / log / supp`) | 引文校验 | 否定预警 | Skeptic 异议 | FPI / 免 Form 4 | 缺口 |
| --- | --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- | ---: |
| 01 | `01_NVDA_recent` | mega_cap_10b5_1 | 60 | 15 | 6 | `5 / 1 / 0` | 17/17 | 1 | 0 | `False / False` | 0 |
| 02 | `02_META_recent` | mega_cap_10b5_1 | 94 | 33 | 6 | `4 / 2 / 0` | 26/26 | 3 | 1 | `False / False` | 0 |
| 03 | `03_JNJ_recent` | pharma_8k_form4 | 48 | 6 | 0 | `0 / 0 / 0`（0 LLM） | 0/0 | 0 | 0 | `False / False` | 0 |
| 04 | `04_AAPL_recent` | consumer_tech | 61 | 27 | 3 | `0 / 3 / 0` | 10/10 | 0 | 0 | `False / False` | 0 |
| 05 | `05_MSFT_recent` | software_mega | 86 | 45 | 5 | `2 / 3 / 0` | 15/15 | 0 | 0 | `False / False` | 0 |
| 06 | `06_TSLA_recent` | high_vol_filings | 23 | 2 | 3 | `0 / 2 / 1` | 14/14 | 1 | 1 | `False / False` | 0 |
| 07 | `07_SMCI_auditor_401` | **trap_401_ex16_1** | 56 | 13 | 5 | `3 / 2 / 0` | 15/15 | 0 | 0 | `False / False` | 0 |
| 08 | `08_CRWD_outage_801` | **trap_801_qualifying** | 56 | 4 | 1 | `1 / 0 / 0` | 4/4 | 0 | 0 | `False / False` | 0 |
| 09 | `09_UNH_cyber_105` | **trap_105_material_cyber** | 77 | 30 | 6 | `4 / 2 / 0` | 12/12 | 0 | 0 | `False / False` | 0 |
| 10 | `10_SBUX_ceo_502` | **trap_502_ex10_ceo** | 36 | 2 | 1 | `1 / 0 / 0` | 9/9 | 1 | 0 | `False / False` | 0 |
| 11 | `11_INTC_ceo_restruct` | **trap_502_205_ceo** | 58 | 11 | 3 | `3 / 0 / 0` | 20/20 | 1 | 0 | `False / False` | 0 |
| 12 | `12_JPM_bank_highvol` | bank_scale_anchor | 90 | 30 | 3 | `0 / 3 / 0` | 12/12 | 0 | 1 | `False / False` | 0 |
| 13 | `13_TSM_fpi_6k` | fpi_6k_twd_ifrs | 157 | 61 | 11 | `2 / 9 / 0` | 19/19 | 0 | 1 | `True / True` | 0 |
| 14 | `14_BABA_fpi_6k_noise` | fpi_6k_buyback_noise | 47 | 2 | 3 | `1 / 0 / 2` | 17/17 | 1 | 1 | `True / True` | 0 |
| 15 | `15_ASML_fpi_6k_eur` | fpi_6k_eur_ifrs | 1 | 1 | 0 | `0 / 0 / 0`（0 LLM） | 0/0 | 0 | 0 | `True / True` | 0 |

---

## 3. 5 大历史陷阱与专项能力验证

1. **`07_SMCI_auditor_401`（`as_of=2024-11-15`，45 天）**：
   - 精准捕获 EY 辞职 8-K（`Item 4.01` + 主动内联 `EX-16.1`）为 `push_alert / critical / negative / auditor_change`（3/3 引文核验）。
   - 自主触发 2 轮本地工具深挖（`drill=2`），完整提取 Cathay Bank 第三次修订协议（`1.01/2.03`）财报交付宽限期与 BofA 定期贷款提前清偿终止（`1.02`）。
   - 被动 `SC 13G/A` 与 Susquehanna 券商 `SC 13G` 准确降级为 `log_only`。
2. **`08_CRWD_outage_801`（`as_of=2024-08-15`，45 天）**：
   - 准确识别 CrowdStrike 自愿 `Item 8.01` 故障披露，定性为 `cybersecurity_voluntary_801`（`push_alert / high / negative`，4/4 引文核验），未与强制 `Item 1.05` 混淆。
3. **`09_UNH_cyber_105`（`as_of=2024-04-01`，45 天）**：
   - 准确捕获 Change Healthcare 强制 `Item 1.05`（`push_alert / critical / negative / cybersecurity_material_105`）及 3 月 7 日 `8-K/A` 进展更新（`push_alert / high / negative`），同时记录高管非 10b5-1 减持为 `log_only`、60 亿美元发债（8.01）与 CFO 兼任总裁（5.02）为 `push_alert`。
4. **`10_SBUX_ceo_502`（`as_of=2024-09-15`，45 天）**：
   - 准确捕获 Brian Niccol 出任董事长兼 CEO（`Item 5.02`，`push_alert / high / positive`，9/9 引文核验），L1 零 LLM 压制 2 份 `10b5-1` 计划减持。
5. **`11_INTC_ceo_restruct`（`as_of=2025-01-15`，60 天）**：
   - 准确捕获 Patrick Gelsinger 辞任 CEO（`Item 5.02`，`push_alert / high / mixed`）、\$7.86B CHIPS 法案直接资助协议（`push_alert / medium / positive`）及 Eric Meurice / Steve Sanghi 董事任命（`push_alert / medium / positive`），20/20 引文核验。
6. **银行规模锚与 FPI 6-K 专项**：
   - `12_JPM_bank_highvol`：`ScaleAnchor` 准确提取 `latest_total_assets=\$5.09T`、`latest_ttm_revenue=\$95.1B`、`latest_net_income=\$65.9B`、`public_float_usd=\$794.4B`（0 缺口）；Skeptic 将常规 \$7B 债发行与 2027 年有序 CFO 交接降级为 `log_only`。
   - `13_TSM_fpi_6k` / `14_BABA_fpi_6k_noise` / `15_ASML_fpi_6k_eur`：全部正确标注 `is_fpi=True`、`fpi_exempt_from_form4=True`；BABA 6-K 精准压制港交所月报表与 0.02% RSU 授予，仅推送平头哥分拆上市实质公告；TSM 精准捕获 4 位内部人首次/集群公开市场买入；ASML 正确识别 `EUR` 锚点并零 LLM 分流财报 6-K。

---

## 4. 平台模型实测暴露并现场修复的 3 个真实生产 Bug

使用真实平台思考模型（`glm-5.3-flash`）与真实 SEC EDGAR 数据实测，直接暴露了离线单测无法覆盖的 3 个生产问题，均已修复、回归测试通过并重新部署上线：

1. **思考模型输出截断与阶段超时不匹配（commit `35ae86d` + `0058e8a`）**：
   - 现象：`_AccountSlotModelRunner` 对未登记的新阶段默认回落到 `max_output_tokens=8192` 与 `timeout=90s`，导致 `glm-5.3-flash` 在多份 SEC 申报批处理时频繁触发 `Provider output was truncated` 与超时重试。
   - 修复：在 `main.py` 为 5 个 V1 SEC 阶段显式配置 `max_output_tokens=16384` 与 `240s` 超时下限。
2. **FPI 6-K 二进制图片附件导致 PostgreSQL `jsonb` 写入失败（commit `0058e8a`）**：
   - 现象：BABA 6-K 包含 `ex99-2img001.jpg`，被误归类为 `ex99_press_release` 并按文本解码，JPEG 头部 `\x00` 字节触发 PostgreSQL `UntranslatableCharacter: \u0000 cannot be converted to text`。
   - 修复：`form8k.py` 的 `discover_filing_exhibits`（`index.json` 与 `index.htm` 双路径）严格仅保留 `.htm/.html/.txt/.xml` 文本附件；`pacing.py` 的 `sec_get_text` 统一清除 `\x00`。
3. **大型银行/资管机构“作为申报方”提交他司 13G 洪泛与单批上限（commit `e7b7fde`）**：
   - 现象：JPM（`CIK 0000019617`）在 45 天内作为资管机构向 SEC 提交了 27 份关于*其他公司*的 `SCHEDULE 13G/A`，导致 18 份候选挤入单个 L2 Batch 触发输出截断。
   - 修复：`form13dg.py` 提取 `<issuerCik>` 并校验 `subject_issuer_cik != target_cik` / `reporting_person.cik == target_cik` / 13G 申报人同名，在 Layer 1 零 LLM 压制为 `filer_not_subject_issuer`（JPM 27 份全部清除）；`research_event_analyst.py` 增加每批最多 `8` 份（稀疏合并最多 `9` 份）硬上限。

---

## 5. 观察到的延迟与成本特征（如实记录）

- **冷启动延迟**：
  - Layer 1（冷 SEC 抓取 + 12m Form 4 行为基线）：`2.6s – 21.4s`；热 L1 缓存：`1.5s – 3.3s`。
  - Layer 2（冷 `glm-5.3-flash` 分析 + 自主工具深挖 + 条件 Skeptic）：
    - 无候选案例（JNJ / ASML）：`0.0s`（0 token）。
    - 单候选案例（SBUX）：`115.1s`（`18,483` tokens）。
    - 中等复杂案例（BABA 3 候选 / JPM 3 候选 / INTC 3 候选 / TSM 11 候选）：`213s – 305s`（`40.8k – 104.1k` tokens，含自主 `drill=1~2` 与 Skeptic 复核）。
    - 热 L2 语义缓存命中（`sec_events_l2_semantic`）：`< 0.1s`（`0` tokens）。
- **结论**：当前 202 异步后台作业模式（`POST /v2/research/sec-events/analyze` + `GET /v2/research/sec-events/analyze/{operation_id}`）与三段式缓存完全匹配平台思考模型的延迟特征；针对本次实测暴露的冷启动耗时、高并发 429 限流及 FPI 6-K 封面页信息边界三项局限，对应的 P0/P1 加固方案已沉淀至 [v1-sec-events-insider-ownership-design-2026-11-24.md（第十四节 v6.1 加固方案）](v1-sec-events-insider-ownership-design-2026-11-24.md#十四v61-平台模型实测后的三项针对性加固方案待实施)。


---

## 6. v6.1 P0 加固落地与平台模型对照验收（`foresightnine-api:ca29276`）

围绕上述三项局限，已于 `ca29276` 完成全部 P0 改造（批量 Skeptic、平台模型全局并发闸门 + 429 长退避、6-K 封面页非 `ex99` HTML/PDF 兜底），37/37 单元测试通过并上线部署，随后在真实平台模型（`glm-5.3-flash`，非 BYOK）上完成 3 项对照验收：

| 验收项 | v6 基线 | v6.1 P0 实测（`ca29276`） | 结论 |
| --- | --- | --- | --- |
| **P0-1 批量 Skeptic（`SMCI` 冷启动 L2）** | `495.90s` / `104,509` tok / `3` 次串行 Skeptic | **`356.76s`（-28.1%）** / **`76,532` tok（-26.8%）** / **`1` 次批量 Skeptic**（审核 3 条 `push_alert`，`gaps=0`） | 提速 **139.1s**、省 **28.0k tokens**（略低于 `-30%` 预期 1.9pp，主因 Primary 2 轮自主深挖耗时固定）；EY 4.01 `critical` 等 5 条信号全覆盖（17/21 引文核验） |
| **P0-2 5 并发冷启动抗 429 压测** | 5 并发触发 BABA `Provider rate limit reached`（`gaps=5`） | `SBUX` / `CRWD` / `AAPL` / `TSLA` / `BABA` 5 线程并发冷启动：**`total_gaps=0`、`total_rl_gaps=0`**（总耗时 `483.6s`） | **完全达标**：平台并发闸门（`limit=2`）+ 429 长退避彻底消除 5 并发限流缺口 |
| **P0-3 FPI 6-K 封面页非 `ex99` HTML/PDF 兜底** | 仅匹配 `ex99` 命名附件 | 单测验证同目录非 `ex99` HTML 兜底生效；实测 `TSM` 60 天 6 份 6-K 均为 `1100–1112` 字符（`recovered_gt2k=0`） | **实证确认**：`TSM` 该 6 份 6-K 在 SEC 目录确无正文附件（仅 1 页封面），需由 P1 本土交易所/官网 IR 源补齐；引擎维持 `partial` + `log_only`、0 幻觉数字 |
