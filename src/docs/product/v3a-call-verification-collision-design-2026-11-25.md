# V3a 电话会 Q&A 核验与预期差引擎（零外部授权版）— 深度架构与实施方案（v3 终审定稿）

更新日期：2026 年 11 月 25 日  
状态：✅ **v3 终审定稿**（已完整融合两轮 `/grill-me` 14 项核心决策 D1–D8 / R2-D1–R2-D6、全链路源码逐行 `/boost` 审查检出的全部 P0/P1 排雷方案、8 大架构目标升级，以及 4 项运维/边界工程闭环，可直接指导 `Step V3a-0 → V3a-3` 编码）  
前置基线：`master @ 00b1331`（方向 A 美股季报深研 Squad、V1 SEC 重大事件引擎、方向 B 假设台账与 P1-B 雷达、V2 同业对齐与相对估值、V2.5 治理激励与资本配置审计均已上线验收）  
上游总纲：[product-paradigm-a-engine-b-radar-2026-11-24.md](./product-paradigm-a-engine-b-radar-2026-11-24.md)  
模型协议纪律：遵守 [AGENTS.md](../../../AGENTS.md)，业务代码按槽位（`slot="deep_reasoner"`）、具体 wire API 协议与显式能力配置接入，不硬编码模型品牌；受控实验与入口探针以当前部署配置为准并如实记录。

---

## 1. 产品定位、前沿对标与核心差异化

V3a 在**零外部付费数据/版权依赖**的前提下，把用户手里的电话会纪要（粘贴文本 / PDF / HTML / TXT / MD / `.docx` / `.vtt`·`.srt` 字幕）、业绩 PPT（`.pptx` / IR PDF）与卖方研报 PDF 从“静态阅读材料”升级为**零微操作自动布防、可跨 Topic/跨季穿透原文页码·Slide·时间戳、可与同期首发 SEC 10-Q 账本算术对撞、可跨季自动验真口头承诺的买方审计引擎**，同时为 V3b（BYOK 一致预期与付费纪要）预留干净扩展缝。

### 前沿产品、竞品与 2025–2026 学术 SOTA 选型

| 标杆产品 / 学术范式 | 吸收的核心长板 | 规避的核心短板 | ForesightNine V3a 选型与超越点 |
| --- | --- | --- | --- |
| **AlphaSense (`SuperAnalyst`+Tegus+Quartr)** | 跨季语义对比、句级原文锚定、管理层基调追踪 | 高昂企业级版权门槛；偏全文检索综述，缺 10-Q 确定性算术打假 | **零版权壁垒**：用户自有纪要/PPT/字幕/研报即传即审；以“确定性结构化切片 + 10-Q Decimal 碰撞”替代昂贵全文 RAG。 |
| **Quartr** | 纪要 + Slide Deck 三件套、口述 Slide 联动（`"turning to slide 14"`）与时间戳锚定 | 定位高效阅读器，无系统化 Q&A 回避审计与跨季口头承诺台账 | **阅读升级为买方审计**：同季支持纪要 + PPT + 研报分权威增量融合互撞，Layer 0 自动建立 Slide-to-Transcript 双向锚点并沉淀跨季承诺追踪。 |
| **Hebbia (Matrix)** | 多文档矩阵交叉核验、逐格溯源 | 单元格级多轮 LLM 延迟高、Token 贵，无法进 `512M/768M` 单容器 | **1-Shot 账本无关抽取 + 0-LLM 7 轨矩阵对撞**：延迟与 Token 成本降低 1–2 个数量级；追加卖方研报零重跑主纪要。 |
| **Daloopa / Visible Alpha / Marvin Labs** | 非标经营 KPI 行级溯源、图表抽数与管理层指引记分卡 | 偏 Excel 填数或强依赖商业数据源，缺逻辑证伪与回避审计 | **OOXML Chart XML 抽数（含 `<c:numFmt>` 与 `<c:dispUnits>` 单位缩放）+ `operating_kpi_next_call` 非标 KPI 追踪 + 账号严格隔离的口头承诺台账**。 |
| **Rogo / BamSEC / Perplexity Finance** | BamSEC 10-Q 红线极快；Rogo 贴合买方工作流 | Perplexity 无服务端引文/口径硬门控，易把 Non-GAAP 错撞 GAAP | **零幻觉双门控**：`normalize_with_raw_map` 局部窗优先反投影 + 否定/限定语境 2-tuple 扫描 + 开场白隐式 Non-GAAP 口径锁 + 首发口径防重述假打假。 |
| **EvasionBench (`arXiv:2601.09142`) + FinNLP 2025 / Bavelas + DualEvasion (`EMNLP 2026`, `arXiv:2608.28040`)** | EvasionBench（22.7M Q&A）确立三档回避谱系（`direct` / `intermediate` / `fully_evasive`）与策略子类；`DualEvasion` 证伪纯文本做声学测谎并揭示声学置信度受发言人个体基线干扰 | `Eva-4B` 需自托管 GPU；单纯单标签分类漏掉复合问题选择性漏答 | **零 GPU 规则化 + 买方增强**：Layer 0 算语言学先验入窗；Layer 2 输出三档 + 含 `selective_part_omission` 的策略子类 + 问/答双逐字引文；累计 `>=2` 季启用买方实践的**高管自身文本基线漂移（Speaker-Baseline Shift）**；面板显式声明仅做文本语义响应核验，不做声学测谎。 |
| **`ECTs-100` (`arXiv:2610.00969`) + ICAIF '26 Availability Contract (`arXiv:2610.21659`)** | `ECTs-100` 确立 **Conscious Incompetence（自觉不足）** 弃权原则；ICAIF '26 严格区分 `period_end` / `8k_filed_at` / `call_date` / `10q_filed_at` 四时间轴防时点错位 | 通用 LLM 常把管理层模糊定性强行补全为数字承诺或拿重述 10-Q 假打假 | Layer 2 强制标 `evidence_sufficiency`（仅 `explicit` 进红/黄硬对撞）；Layer 1 与 `P+1` 回测强制执行四时间戳可用性契约与首发 10-Q 口径重建。 |

---

## 2. 两轮 `/grill-me` 14 项核心决策（v3 源码实证修订定稿）

### 第一轮（产品边界、核验语义、自动化与交付节奏）

| 编号 | 决策主题 | v3 最终锁定方案 |
| --- | --- | --- |
| **D1** | **非 SEC 公司覆盖边界与多语言别名归并** | 首版聚焦 SEC 美股/FPI 完整 7 轨对撞；非 SEC 公司（A 股/港股/未上市纪要）自然降级为「**轨道 0（同场自相矛盾）+ Q&A 回避审计 + 跨季口头承诺台账**」，暂不耦合 BaoStock/Tushare 财务源。代码化证券复用 `fin_data/cn.py` / `entity.py` 规范化为 `nonsec:HKEX:0700` / `nonsec:SSE:600519`；纯中英文名称（如“腾讯控股” vs “Tencent”）支持账户隔离别名表 `v3a_entity_aliases` 与**角落纠偏 chip 一键合并历史 `entity_key`**，保障跨季 Silent Drop 与基线追踪不断链。 |
| **D2** | **Non-GAAP 口径核验边界（含开场白隐式继承）** | 严格执行 **GAAP/Non-GAAP 口径锁**：Layer 0 侦测开场白 `call_default_basis = "non_gaap"`，Layer 3 对未显式声明 `"GAAP"` 的利润率/OpEx/EPS 声明自动继承 `non_gaap`；Non-GAAP 量化承诺标 `8k_nongaap_manual`（绝不误撞 GAAP XBRL），同季对比仅展示 `basis_divergence_nongaap_gap` 调节差 bps 与原文摘录；暂不做脆弱的启发式 8-K 调节表正则抽取器，留待后续或 V3b BYOK 扩展。 |
| **D3** | **Q&A 回避审计降噪（双条件商业保密门控）** | 合理商业保密白名单（单客户定价/份额、未公告 M&A、在审诉讼、多年精确 EPS 预测）采用**双条件门控**：必须同时满足 `question_asks_sensitive_detail == True` **AND** `exec_cites_confidentiality_policy == True` **AND** `behavior_subtype != "topic_pivot"`，Layer 3 才标 `legitimate_confidentiality=True` 并降级为 `info`（不计红/黄统计、默认折叠可展开）；`direct` 轮次默认折叠，突出 `fully_evasive` / `intermediate`。 |
| **D4 / D5** | **延迟 10-Q 双保险自动触发与轨道 5 门控两段式补齐** | `call_commitment_verify` 通道默认纳入订阅（并显式 upsert 至老订阅），置于雷达 **Phase 2c**，配合**“打开 Topic/Inbox 30m 冷却静默 0-LLM `submissions` 指纹检查 + API `lifespan` 可开关后台定时巡检协程（短事务租约锁）”**双保险自动触发。发现新 `10-Q/10-K/20-F`（或 `NT 10-Q/10-K` 延期归档告警）后走门控两段式：Phase 1 纯 0-LLM 重算量化对撞、承诺打分与 Item 1A **风险类别桶 + 词干实体覆盖**双因子初筛；**仅当**筛出实质新增未提风险且模型槽位可用（未触碰用户显式预算上限）时，Phase 2 自动静默补 1 次极轻量语义核验（`<=8KB`）并更新报告与 `/inbox`；否则纯 0-LLM 完结并保留原文摘录。 |
| **D6** | **前端一步到位与双向协同（保留真实权限）** | 点击「开始核验」直接启动异步全流程（通过三段式 SSE 在 `<1.0s` 内渐进渲染 Layer 0 / Layer 1 骨架再平滑合入 Layer 2/3，无中间确认页）；`/inbox` 深链 `call_audit_url` 直达展开 `CallVerificationPanel`；Direction A 财报小组无 FMP BYOK 时自动复用本 Topic 用户上传纪要的 `parse_transcript_v2` 填充 `transcript_index`，**严格保留原 `retrieval_method`、`kind="user_supplied"` 与真实 `permissions`**（不伪造为 `fmp_transcript` / `export=False`，同时确保真实 FMP BYOK 纪要仍 100% 脱敏）。 |
| **D7** | **零前置微操作自动布防（opt-out）与跨租户隔离** | 取消前置“手动转入触发器 / 确认草稿”按钮：① 自动识别文档原型、公司实体与会计期直接开跑（角落留静默纠偏与实体合并入口）；② 核验完自动将管理层一手承诺写入**严格账号隔离**的 `v3a_verbal_commitment_ledger`（**严禁回写全局共享的 V2.5 `GUIDANCE_CREDIBILITY_LEDGER`**），并以**只读伴生监控信号**映射至匹配 `ThesisLedger` 支柱（绝不篡改已确认 `ThesisLedger` 版本）；③ 缺当季 10-Q 时自动激活 `call_commitment_verify` 监控；④ 兑现静默转绿，仅违背/矛盾/Silent Drop 推 `/inbox`；角落保留「忽略（opt-out）」。 |
| **D8** | **分阶段实施与受控实测验收** | 按 `Step V3a-0 → V3a-1 → (deep_reasoner 最小运行态探针) → V3a-2 → V3a-3` 连续推进（每步独立单测回归与独立提交），最后部署到现网容器并用真实美股纪要/PPT/巨型 10-K/延迟 10-Q 样本完成受控端到端验收统一汇报。 |

### 第二轮（零摩擦入口、会计期对齐、两段式 OCR、防刷屏与上下文闭环）

| 编号 | 决策主题 | v3 最终锁定方案 |
| --- | --- | --- |
| **R2-D1** | **常规资料区自动感知 + 全量流式多格式上传** | 常规 `AddSourcesPanel` 与 `CallVerificationBar` 共享同一份源列表；**所有 `pdf \| pptx \| docx \| text`（含 `<=5MB` PDF）全部走 `POST /v2/research/sources/upload` 流式管线**并支持 `.md`/`.vtt`/`.srt`。新挂载文件后经 `/call-audit/preview`（传 `source_ids`，`<150ms` 纯 0-LLM）加**负向原型过滤**（`form_10k_10q` / `macro_report` / `legal_contract` 硬拒判），仅当 `archetype in ("transcript", "ir_slide_deck")`、`confidence >= 0.75` 且命中 `>=2` 项结构强特征时幂等静默触发 V3a 核验（前端附可点击「取消」的轻提示）。 |
| **R2-D2** | **双锚定会计期锁 + 首发口径重建 + 即时回测** | Layer 0 提取 `call_date` 并与 SEC `submissions` 前 `0–65` 天内的 `reportDate` 及 XBRL `(fy, fp)` **双锚定校准会计期 `P`**（消除非自然财年错位）；轨道 1–3 严格锁定 `P` 同期 10-Q，并在调用 `_derive_ytd_quarters` 前按 `filed <= period_P_initial_10q_filed_at` 重建**首发口径**（杜绝事后重述/拆股假打假，`>1%` 变动旁注 `restated_later`）；在四时间戳与 `as_of` 约束内存在 `P+1` 10-Q 时，轨道 6 顺手即时回测前瞻承诺；多季批传按时间序串联即时检出 Silent Drop。 |
| **R2-D3** | **混合型 IR PDF 智能挑页 + 两段式不可变 OCR** | 引入 `"text_partial"` 页面状态（`<120` 有效字符时保留短页眉供挑页排序而不清空），单路 150 DPI（`FORESIGHTNINE_OCR_WORKERS=1`，保留 `OMP_THREAD_LIMIT="1"`，不盲降 `prlimit --as`）。为彻底规避 Cloudflare 100s `524`，**同步 `/upload` 硬限 `<=25s / <=4` 页**优先高价值页 OCR；剩余高价值待扫页（再补 `<=8` 页）在 V3a 异步 `202` Operation 的 **Layer 0b（带 SSE 15s 心跳）** 复用 `build_ocr_pages_callback` 持久化为**不可变新版 `ResearchSource`（`ocr_source_id`, `supersedes_source_id`）**，V3a 所有引文指向 `ocr_source_id` 确保 L3 偏移零误差；未 OCR 页诚实记入 `open_gaps`。 |
| **R2-D4** | **10-Q 同报告期 `/inbox` 两级聚合与跨通道合并** | 同一 `(account_id, cik, report_period)` 最多产 1 条 V3a 聚合摘要；因 `earnings_delta` 持 Day 0 `8-K` accession 而 V3a 触发于 `10-Q` accession，在 `ChangeInboxItem` 新增可选字段 `report_date` / `fiscal_period_key` / `has_confirmed_ledger`（均 `= None`），合并键升级为**两级匹配**：精确 `(account_id, cik, accession)` **OR** 同报告期 `(account_id, cik, report_date / fiscal_period_key)`（兜底同 `cik` 在 `[10q_filed - 45d, 10q_filed + 7d]` 内未读 `earnings_delta` 卡）。合并时抬升 `impact_level = max(old, v3a)`、合并 `affected_pillars` 并追加主摘要可见徽章；若同报告期 `earnings_delta` 卡已 `accepted / dismissed_with_note` 或不存在，则独立发出 1 张 `call_commitment_verify` 聚合卡。 |
| **R2-D5** | **Topic 全轮次追问上下文自动共享 + 撤销隔离 + Markdown 导出** | 当前 Topic 存在 V3a 核验结果时，**覆盖 Turn 1 及所有后续自由对话轮次**自动静默注入 `<=2,500` 字符已验真 `prior_research.call_verification_digest`（含 `source_hashes`，过 `model_input` / `not is_simulated` / `as_of` 三道门控）；同步接入 `research_context_manager.py:1674-1692` 撤销校验（当前 Topic 源入 `used_ids`，跨 Topic/衍生 OCR 源独立做 owner 级校验以免误杀会话 `history`），撤权续跑零泄漏；`FinDataToolbox.call_qa_verification` 放行本地账户数据的 `as_of` 安全过滤；面板配 1 键 **「复制 Markdown 审计简报」**（严守 `permissions.export` 脱敏）。 |
| **R2-D6** | **4 级权威隔离 + 分权威增量缓存融合** | 严格 4 级权威序：**`SEC 10-Q/10-K` > `8-K Ex-99.1` > `电话会纪要 & IR PPT (management_first_party)` > `卖方研报 (third_party_opinion)`**；管理层真伪对撞（Tab 1）与卖方预期差（Tab 3）物理分轨。Layer 2 缓存拆分管理层主抽取（`v3a_l2`）与单份卖方研报抽取（`v3a_sellside`）：**同季追加卖方研报时 0 次重跑管理层主纪要**（仅 `<=8KB` 轻量抽卖方预期 + Layer 3 `<10ms` 融合，延迟与 Token 降 75%）；卖方研报按日期标 `pre_earnings_preview` / `post_earnings_review`。 |

---

## 3. 全链路源码实证排雷与 `Step V3a-0` 前置底座原子修复清单

逐行对照 `master @ 00b1331` 源码审查，共锁定 **9 类必须在业务编码前（`Step V3a-0`）或紧随底座阶段彻底修补的 P0/P1 隐患**。所有引用行号均经现网源码核实：

| 类别 | 现网位置（实证行号） | 实证缺陷 / 隐患 | `Step V3a-0` 原子修复方案 |
| --- | --- | --- | --- |
| **P0-1 · 跨租户隐私红线** | `main.py:8254, 8930`；`research_fin_tools.py:1900`；`research_governance_audit.py:1302-1399` | V2.5 `GUIDANCE_CREDIBILITY_LEDGER` 为**全局共享集合（`gcl:{cik10}`，无 `owner_id`）**，且仅覆盖 5 项指标单一中点 Beat/Miss。若 V3a 回写，将直接把账号 A 私有上传纪要/模拟笔记泄漏给账号 B。 | **严禁 V3a 回写 `GUIDANCE_CREDIBILITY_LEDGER`！** `v3a_verbal_commitment_ledger` 坚持 `v3a_vcl:{account_id}:{entity_key}` 账号隔离；抽离**无状态纯函数 XBRL 实绩查询库**（支持 12 指标 + 首发口径 + `as_of`）供 V2.5 与 V3a 代码复用；V3a Tab 3 仅**只读**引用 V2.5 公共台账作历史信用先验。 |
| **P0-2 · 现网雷达二次巡检必崩** | `main.py:8118-8125`；`sec_events/__init__.py:144-158, 595`；`research_radar_scheduler.py:694` | `_scan_sub` 将 `seen_v1: list[str]` 传给 `scan_sec_material_events(..., seen_accessions=seen_v1 or None)`，底层调用 `.get(accession)`。第 1 次因空列表传 `None` 侥幸通过，**第 2 次巡检必抛 `AttributeError` 导致所有订阅标 `error`**。 | 在 `_scan_sub` 将 `seen_v1` 转为 `{acc: {"sec_events": "seen"} for acc in seen_v1}`；同时加固 `_is_seen_for_channel` 兼容 `Sequence/Set[str]`，并加二次巡检回归单测锁死。 |
| **P0-3 · 雷达 4 处断链与 R2-D4 0% 命中** | `sec_events/__init__.py:161-171, 184-203`；`research_radar_scheduler.py:33-35, 92-121, 175-184, 264-302, 716-722, 746-805, 916-941`；`research_delta_review.py:525-549`；`main.py:7754, 8067, 8269` | ① `scan_sec_material_events` 对 `10-Q/10-K/20-F` 返回 `None` 且不含 `NT 10-Q/10-K`；② `L805` 早退守卫在仅有延迟 `10-Q` 时跳过 Phase 2；③ `earnings_delta` 持 Day 0 `8-K` accession 而 V3a 持 `10-Q` accession，按 `(cik, accession)` 合并命中率 **0%**，且 `ChangeInboxItem`（`extra="forbid"`）当前缺少 `report_date`/`fiscal_period_key`/`has_confirmed_ledger`；④ `main.py:7754` 默认通道缺 `governance_audit`/`call_commitment_verify` 且老订阅无回填；⑤ `upsert_inbox_item_with_folding` 不区分 no-op 与修改，反复写库并虚增 `inbox_items_updated`。 | ① 零额外请求输出 `periodic_filings: list[PeriodicFilingRef]`（含 `10-Q/10-K/20-F` 及其 `/A` + `NT 10-Q/10-K`）；② `seen_accessions_for_channel` 扩 `"call_verify"`、`WatchChannel`/`InboxSourceChannel` 扩 `"call_commitment_verify"`、`ChangeInboxItem` 增可选 `report_date`/`fiscal_period_key`/`has_confirmed_ledger`（均 `= None`）；③ 放宽 `L805` 含 `new_periodic_filings` 并置于 **Phase 2c（Phase 2b 之后）**；④ R2-D4 改为**两级报告期/时间窗匹配**（见 §7.3）；⑤ 更新 `main.py:7754` 默认通道并在 V3a 自动布防时显式 upsert 老订阅；⑥ `upsert_inbox_item_with_folding` 改为 3-tuple `(item, is_new, is_modified)` 修掉虚增计数。 |
| **P0-4 · `<=5MB` PDF 绕过 OCR、`"text_partial"` 漏改与 Blob 回读断链** | `lib/sources.ts:6, 268-283`；`lib/sources.test.ts:123, 128`；`AddSourcesPanel.tsx:139, 203-212, 295`；`copy.ts:456-470`；`RecordPanel.tsx:202, 219, 284`；`api/types.ts:254-255`；`research_documents.py:42, 80-91, 104, 192, 208, 247, 299, 343`；`research_fetch.py:1220, 1226, 1249, 1255, 1285, 1301-1306`；`research_ocr.py:40-41, 264, 465-514, 684, 815-819, 866-893`；`research_tool_executor.py:759`；`research_upload_stream.py:98-116`；`main.py:2235-2238, 2725-2728, 2871, 2910-2927, 2958-3005`；`research_sources.py:232`；`research_analysis.py:2710` | ① `usesStreamingUpload` 导致 `<=5MB` PDF（>90% IR PDF/纪要）走旧 `/sources/pdf`，零 OCR、零 `document_pages` 且 6.7MB Base64 进 Postgres JSONB；② `AddSourcesPanel` `<input accept>` 拒 `.pptx/.docx/.md/.vtt/.srt`，`lib/sources.test.ts:123` 断言 `.docx` 为 `null`；③ `classify_page_text` 1 字符判 `"text"` 且 `L104/L299` 清空非 `"text"` 页；若引入 `"text_partial"` 但漏改 `research_fetch.py:1220,1226,1301,1303,1306` 与 `research_documents.py:208`，全页带页眉的混合 IR PDF 将跳过 `_ingest_ocr` 并直接报 `422 ocr_required`；④ `extract_pdf_pages` 硬编码 `-layout` 未透传 `text_order`；⑤ `/upload` 把 `text` 存成 `user_html`，且 `research_upload_stream.py:102-110` 在 BlobStore 已配置时仍双写 Base64；⑥ 原件可见性/重提/下载 **4 处**（含 `reextract_research_pdf` `main.py:2910`！）仅查 Base64 不认 `original_blob_sha256`，且 `main.py:2968` 与 `RecordPanel.tsx:202,219` 拒 `.pptx/.docx` 下载；⑦ `\f` 分页硬编码 `user_pdf`/`_PDF_RETRIEVAL_METHODS` 漏 `user_pptx`；⑧ `DocumentBlobStore.prune()` 无 `min_orphan_age_seconds` 且未接 `retention.py`，`OcrCache` (`ocr-cache/*.json`) 亦无磁盘回收。 | ① 前端 `usesStreamingUpload` 改为 **`pdf \| pptx \| docx \| text` 全量走 `/upload`**，同步更新 `UploadKind`、`AddSourcesPanel`、`copy.ts`、`RecordPanel.tsx`、`api/types.ts`、`lib/sources.test.ts:123,128`；② 定义共享集合 `OCR_CANDIDATE_STATUSES = frozenset({"needs_ocr", "text_partial"})` 与 `TEXT_STATUSES = frozenset({"text", "ocr", "text_partial"})`，**一次性对齐全部 14 处页状态判断**（`research_documents.py:42,80-91,104,208,299,343`、`research_fetch.py:1220,1226,1249,1301,1303,1306`、`research_ocr.py:815,819`、`research_tool_executor.py:759`），保留短页眉文本并贯通 `text_order`（`reading` 不传 `-layout` 替代 `-raw`）；③ `_ocr_one` 透传 `dpi=150`（保留 `OMP_THREAD_LIMIT="1"`，勿盲降 `prlimit --as`），同步 `/upload` 压至 `<=25s/<=4页`，剩余高价值页走异步 Layer 0b 经 `build_ocr_pages_callback` 落不可变新版 `ResearchSource`；④ `research_upload_stream.py:101-112` 在 `configured_blob_store()` 已配置时仅落 `DocumentBlobStore`（`original_base64=None`，零 Base64 进 Postgres；仅无 BlobStore 的单测环境保留小文件 Base64 回退），修复 `main.py:2871` 分发、**4 处 `original_blob_sha256` 回读**（含 `reextract_research_pdf` `main.py:2910-2927`）及 `main.py:2966-2973` `.pptx`/`.docx` MIME 放行；⑤ **新增共享常量 `_PAGED_RETRIEVAL_METHODS = frozenset({"user_pdf", "web_pdf", "user_pptx"})`** 替换 `research_sources.py:232`、`research_analysis.py:2710` 与 `main.py:2990,3003-3004`；⑥ `DocumentBlobStore.prune()` 增 `min_orphan_age_seconds` 并连同 `ocr-cache` 清理接入 `retention.py`（见 §7.1）。 |
| **P0-5 · 模型 90s 超时与截断缩容被 `_turns` 静默吞掉** | `main.py:5212-5253, 5501-5524, 5568-5614, 5727`；`research_llm_primitives.py:228-257` | ① 未注册 stage 回退 `90.0s` / `8192` tokens，思考模型 32KB JSON 抽取必超时或截断；② `invoke_and_parse_with_repair` 在 `finish_reason=="length"` 时拼接截断残骸续写，且 `_AccountSlotModelRunner.invoke` 仅在 `turn_index==0 or key not in self._turns` 才重读 `messages[1]`，直接替换 `messages` 会被旧 `_turns` 粘滞吞掉导致连爆 3 轮！ | ① 注册 `v3a_call_extract`（`240.0s` / `16384`）、`v3a_call_map_doc`（`180.0s` / `8192`）、`v3a_sellside_extract`（`180.0s` / `8192`）、`v3a_omission_verify`（`180.0s` / `8192`）；② 扩展 `invoke_and_parse_with_repair` 检测 `finish_reason == "length"` / 末尾 JSON 截断，触发**缩容重置模式**（显式传 `reset_conversation=True` → `turn_index=0` + 新 `conv_id` 后缀，彻底清空 `_AccountSlotModelRunner._turns` 并约束条数与引文长度）。 |
| **P0-6 · `512M` 内存穿孔与巨头 10-K/10-Q 失明** | `compose.yaml:66`；`sec_events/pacing.py:459-473`；`sec_deep.py:398-414, 2496, 2712, 2872`；`research_peer_compare.py:57, 602`；`research_upload_stream.py:178`；`research_uploads.py:25`；`research_ocr.py:106` | ① 4 套互不感知并发信号量叠加极易突破 `512M` OOM；② `sec_get_json` 先整读 60MB `raw_body` + `decode` 再 `json.loads`（峰值 ~250MB）；③ `sec_filing_sections` 60MB 整读转 `str`（且用旧非等待限流器），若简单硬限 8MB 会牺牲 MSFT/META/AMZN/JPM 等 10–45MB 巨头 10-K/10-Q 的轨道 5。 | ① 引入进程级 **`HEAVY_MEM_GATE = BoundedSemaphore(1)`**（`FORESIGHTNINE_HEAVY_MEM_CONCURRENCY=1`）统一串行化 4 类重内存操作；② 在 `sec_events/pacing.py` 新增 **`sec_get_to_tempfile()`**（走共享 `SecPacingLimiter` + `stream_transport` 直接落磁盘临时文件，零整份 60MB `bytes`/`str` 驻留内存）；③ `companyfacts` 基于临时文件运行纯 stdlib **45 科目全量对应 XBRL Tag（`us-gaap`/`ifrs-full`/`dei` 约 120+ 个，从 `_V2_PROJECTION_LINES` 动态推导）切片增量 `raw_decode` 投影器**（峰值 **~25–35MB**）；④ 10-Q/10-K 基于临时文件跑 **64KB 分块流式 `HTMLParser`** 跳过 `<ix:header>`/`<style>`/`<script>`（45MB 巨头 10-K 峰值 **`<5MB`**，轨道 5 100% 覆盖）；⑤ `fetch_filing_table` 新增 `max_bytes` 参数供 `R*.htm` 限 `1.5MB`。 |
| **P1-7 · R2-D5 Turn 1 漏注入、撤销泄漏/误杀与 `as_of` 封锁** | `main.py:3779-3796, 3917-3920`；`research_context_manager.py:1660-1692` | ① 新 Topic Turn 1 时 `prior_research_input(version_obj)` 为 `None`，`L3919` 直接返回导致首轮提问吃不到 V3a 摘要；② `L1676` `used_ids` 仅扫 `('facts', 'claims', 'read_ranges')`，撤权续跑会泄漏已撤纪要摘要；**反面隐雷**：`L1660` `eligible` 仅含当前 Topic 源，若把跨 Topic 历史 Q2 源或锁定 Topic 下未挂载的 Layer 0b `ocr_source_id` 直接塞进 `used_ids`，会导致每轮误判 `revoked=True` 清空整个会话 `history`！③ Topic 设 `as_of` 时 `main.py:3779` 以 `'historical_cutoff'` 全局禁用 `FinDataToolbox`。 | ① `_prior_with_failed_sources` 在存在合规 V3a 摘要时即使 `prior_data is None` 也初始化 `{}` 注入（覆盖 Turn 1）并附 `source_hashes: dict[str, str]`；② `L1674-1692` 将**当前 Topic** 的 `call_verification_digest` 源纳入 `used_ids` 校验，对**跨 Topic / 衍生 OCR 源**经 owner 级查询独立核验权限与 `content_sha256`（仅撤权时剥离该 digest，绝不误杀无关 `history`）；③ `FinDataToolbox.call_qa_verification` 放行本地账户已验真数据的 `as_of` 安全过滤。 |
| **P1-8 · `RetrievalMethod` 拒收、引文偏移漂移与 FPI/OpEx 精度漏洞** | `research_sources.py:65-84`；`research_fin_tools.py:1788, 1942`；`research_llm_primitives.py:52-118`；`research_governance_audit.py:1844`；`research_peer_compare.py:443-450`；`sec_edgar.py:91-98, 404-425` | ① `RetrievalMethod` 严格 `Literal` 拒收 V2/V2.5/V3a 工具产物与新上传类型；② `find_quotation_span` 在归一化空间返回偏移，且连字 `ﬁ→fi` 与表容忍加空格破坏简单映射；③ `check_negation_context` 未解包 2-tuple；④ `project_companyfacts_to_snapshot` 缺 `per_share` 过滤、仅硬编码 9 种货币（KRW/HKD 等错标 USD）、缺拆分 OpEx（`selling_expense`/`admin_expense`/`total_operating_costs`）。 | ① 原子扩容 `RetrievalMethod`（补 `user_pptx`/`user_docx`/`user_text`/`peer_relative_valuation`/`governance_capital_audit`/`call_qa_verification`）并同步全部 **8 处下游分发**（`main.py`、`research_earnings_squad.py`、`research_coverage_map.py`、`research_reconciliation.py`、`research_table_provenance.py`、`EvidenceViews.tsx`、`RecordPanel.tsx`、`copy.ts`）；② 实现精确字符映射 **`normalize_with_raw_map(raw_text, mode)`** + 局部 `turn_id`/`slide_index` 窗优先反投影；③ 修复 `check_negation_context` 2-tuple 解包；④ 复用 `sec_edgar._currency`（ISO-4217）与 `_pick_unit`（`per_share`）并增投 3 项 OpEx。 |
| **P1-9 · 其余底座清理（`.items()` / 删号 / 导出权限 / 体限 / UI 挂载）** | `main.py:1764-1783, 7810-7854, 8283-8298, 8854, 8877`；`storage.py:300-307, 531-538`；`research_peer_compare.py:540`；`research_delta_review.py:337`；`research_earnings_squad.py:4739-4773`；`research_earnings_export.py:289-305`；`EarningsDossierView.test.tsx:470,561,739`；`security.py:301-310`；`TopicPage.tsx:1546-1571`；`ReaderPanel.tsx:20, 88, 104-108`；`vite.config.ts:46` | ① `_earnings_delta_for_sub` 签名/返回值错配；② `main.py:7810-7854, 8854, 8877` 与 `research_peer_compare.py:540` 残留 `.items()` 全表扫描——**且 `ThesisLedger`/`FilingWatchSubscription`/`ChangeInboxItem` 顶层字段是 `account_id` 而非 `owner_id`，直接误用 `owner_items()` 会返回空列表！** ③ 删号未清理上述三集合及 V3a 活跃任务；④ Direction A 复用用户纪要硬编码 `fmp_transcript` + `export=False`；⑤ 非上传 `POST` 受 `max_body_bytes` 限制；⑥ `GovernanceAuditBar` 锁在 `{squadDossier}` 内；`ReaderPanel` 跨 Topic/锁定时 404 且缺跨 Topic 标题元数据；`vite.config.ts:46` 强制 `style-src 'self'`。 | ① 对齐 `_earnings_delta_for_sub` 完整构造 `DeltaStage0Package`；② 清除残留 `.items()`：`EARNINGS_SQUAD_STATES`（`L7827`）改 O(1) `.get(_squad_state_key(...))`、`PEER_COMPANY_SNAPSHOTS`（`L8854` / `research_peer_compare.py:540`）走 `_TICKER_MAP_CACHE`/`resolve_ticker_to_cik` + `.get(f"snap:{cik10}")`、`THESIS_LEDGERS`/`WATCH_SUBSCRIPTIONS`/`CHANGE_INBOX_ITEMS` 在 `PostgresDocumentRepository` & `InMemoryDocumentRepository` 新增 **`items_for_account(account_id)`**（SQL `COALESCE(document->>'owner_id', document->>'account_id') = %s`）；③ 删号复用 `items_for_account` 清理上述集合 + `with_advisory_lock(f"v3a_man:{account_id}")` O(K) 清理 V3a 并取消活跃任务；④ Direction A 复用用户纪要保留真实 `retrieval_method`/`kind`/`permissions`；⑤ `/call-audit/preview` 只传 `source_ids`；⑥ 新增 owner-scoped `GET /v2/research/sources/{source_id}/text`（支持 `?topic_id=` 校验 `as_of` 并返回标题/权限元数据）；`CallVerificationBar` 挂 `{squadMode}` 外部且 100% 零内联样式。 |

---

## 4. 四层解耦架构：事实 / 抽取 / 规则三权分立

```text
[零授权统一输入（严格 owner_id 隔离，AddSourcesPanel 与 CallVerificationBar 共享）]
  ├── 粘贴纪要/笔记 (<=2MB) ──────────────► POST /v2/research/sources
  ├── 全量 PDF / HTML / TXT / MD / VTT / SRT ► POST /v2/research/sources/upload (全量流式，同步 OCR <=25s/<=4页，原件落 DocumentBlobStore)
  ├── PPTX / DOCX (<=25MB) ───────────────► POST /v2/research/sources/upload (纯 stdlib 防炸弹 OOXML，原件落 DocumentBlobStore)
  └── 常规挂载自动感知（R2-D1） ───────────► POST /v2/research/call-audit/preview (传 source_ids，<150ms 0-LLM + 负向原型过滤)
        │
        ▼  Layer 0（0 LLM，<1.0s 首屏推 SSE layer0_ready；持久化 v3a_doc_parsed_facts，键含 parser_version）
[保偏移多格式解析 · 4 级权威锁定 · 开场白口径侦测 · 双锚定会计期 P · 语言学先验]
  ├── 权威度与角色分类（R2-D6）：transcript / slide_deck → management_first_party；sellside_note → third_party_opinion (前瞻/点评)
  ├── 开场白前 1,000 字口径声明保护（call_default_basis = "non_gaap" | "gaap" | "unspecified"）
  ├── 双锚定会计期 P = (FY, FQ, call_date) + SEC submissions 0–65 天 reportDate 校准；多季批传按时间序串联
  ├── parse_transcript_v2：非破坏性 boilerplate 掩码 → 名录先验 → 6 格式多语言状态机 + VTT/SRT 滚动去重(保留时间戳)
  │     → Q&A Exchange Pairing → Slide-to-Transcript 交叉锚定 → 0-LLM 语言学回避先验 & 高管自身基线特征
  ├── research_pptx.py / research_docx.py：.rels 顺序 + Notes + Chart(numFmt & dispUnits 缩放) + 隐藏页/合并单元格 / 跳 <w:del>
  └── Layer 0b（异步 202 内按需触发）：对剩余 <=8 页高价值 "text_partial"/"needs_ocr" 页经 build_ocr_pages_callback 落不可变新版 ocr_source_id
        │
        ▼  Layer 1（0 LLM，热 <80ms / 温 <1.5s，推 SSE layer1_ready；共享 SEC 缓存 + HEAVY_MEM_GATE）
[会计期 P 首发口径对齐的 10-Q / 8-K 确定性账本底座 (Sec10QCollisionLedger)]
  ├── PEER_COMPANY_SNAPSHOTS v4（55 项 STANDARD_LINES / 45 科目投影 + 拆分 OpEx + 通用 ISO-4217 + per_share；sec_get_to_tempfile + 动态全量 Tag 切片 raw_decode 峰值 ~30MB）
  ├── 四时间戳契约 + 首发口径重建：核验历史季 P 先按 filed <= initial_10q_filed_at 过滤再 _derive_ytd_quarters（>1% 标 restated_later）
  ├── 无状态共享 XBRL 实绩查询纯函数库（V2.5 & V3a 复用代码，严禁 V3a 回写全局 V2.5 GUIDANCE_CREDIBILITY_LEDGER）
  ├── 轻量 R*.htm 附注兜底（FilingSummary.xml 最多 3 张 <=1.5MB）+ 64KB 流式 HTMLParser 10-Q/10-K 红线（45MB 巨头 <5MB RAM）
  └── 时点状态机：same_period_10q_matched | awaiting_current_10q_using_8k_and_prior_10q | fpi_20f_6k_limited | financial_sic_gated | non_sec_entity
        │
        ▼  Layer 2（异步 202 Operation + 三段式 SSE + 15s 心跳 + 轮询兜底；单主调 <=32KB，tools=()）
[账本无关 · 分权威增量缓存 · 有界思考模型客观抽取器 (slot="deep_reasoner", timeout=240s, max_output_tokens=16384)]
  ├── 纯静态 system（跨公司字节不变，最大化 Prompt Cache）+ 动态 user 前缀（<=2.5KB：未结承诺 + Thesis 支柱 + call_default_basis）
  ├── Zone B（<=16KB）65/35 双桶配额（65% 回避/争议 + 35% 量化指引/集群热点 + 尾部未入窗目录）+ Zone C（<=10KB 去重保口径）
  ├── 分权威缓存拆分：v3a_l2（管理层一手材料） vs v3a_sellside（单份卖方研报独立键 → 追加研报 0 次重跑主纪要）
  ├── 截断感知缩容重置循环（finish_reason=="length" → reset_conversation=True / turn_index=0 清空 _turns）
  └── 仅输出 6 类原子客观事实（含 evidence_sufficiency / sub_questions / speaker_role / period_scope，绝不落盘红黄绿灯）
        │
        ▼  Layer 3（0 LLM，<10ms 实时内存裁决，推 SSE completed；改 evaluation_rules_version 零重建！）
[零漂移局部优先反投影 + 7 轨 Decimal 对撞 + 外置规则引擎 (v3a_call_verification_rules.json)]
  ├── normalize_with_raw_map（局部 turn/slide 窗优先 → 全文兜底）→ page_for_offset → 2-tuple check_negation_context
  ├── 7 轨对撞（Conscious Incompetence 门控 + 口径锁 + 期间锁 + 币种锁 + 复合问题漏答 + CEO/CFO 分歧 + 双条件保密白名单 + Item 1A 双因子初筛）
  └── 零微操作自动闭环：
        ├── 自动写入账号隔离 v3a_verbal_commitment_ledger + 只读伴生映射 ThesisLedger 支柱（角落 opt-out 忽略）
        ├── 自动挂载 Topic 全轮次（含 Turn 1）<=2,500 字已验真摘要（含撤销校验）+ 1 键复制 Markdown 简报
        ├── 缺当季 10-Q 自动激活 call_commitment_verify 监控（老订阅显式 upsert）
        └── 延迟 10-Q 双保险自动触发 → 雷达 Phase 2c 门控两段式重算 → 两级报告期合并未读 earnings_delta 卡或发 1 张聚合卡（R2-D4）
```

---

## 5. 各层核心工程规格（直接指导编码）

### 5.1 Layer 0：防炸弹多格式解析、两段式不可变 OCR 与 `parse_transcript_v2`

1. **纯 stdlib OOXML 解析（`research_pptx.py` & `research_docx.py`，零三方依赖）**：
   - **生产级 Zip 炸弹与 XXE 防线**：上传硬限 **`25MB`**；**绝不信任 `ZipInfo.file_size` 头部声明**，通过流式 `_read_zip_member_capped(zf, name, max_bytes=4_194_304, running_total_budget=52_428_800)` 按实际解压字节硬中断；成员数 `<=500`、Slide 数 `<=300`、压缩比 `<=50x`；`.rels` 相对目标经 `posixpath.normpath` 后必须匹配白名单前缀 `("ppt/", "word/")` 且无 `..`；解析前拒收 `b"<!DOCTYPE"` / `b"<!ENTITY"`；使用 `ET.iterparse` 逐元素 `elem.clear()`，限制 `<p:grpSp>` 嵌套深度 `<=16`、单文件 XML 节点 `<=200_000`。
   - **格式嗅探与 Blob 存储**：`sniff_upload_kind` 以 ZIP 魔数 `PK\x03\x04` + `[Content_Types].xml` + `ppt/presentation.xml` / `word/document.xml` 判定 `"pptx"` / `"docx"`，将 `.md`/`.markdown`/`.vtt`/`.srt` 纳入 `"text"`；`configured_blob_store()` 已配置时**全部 `.pptx`/`.docx` 与流式上传 PDF 原件直落 `DocumentBlobStore`（`original_base64=None`，零 Base64 进 Postgres JSONB）**，仅无 BlobStore 的本地单测环境保留小文件 Base64 回退。
   - **PPTX 高保真提取**：按 `ppt/presentation.xml` (`p:sldIdLst`) + `ppt/_rels/presentation.xml.rels` 还原真实放映顺序；每页经 `ppt/slides/_rels/{slide}.rels` 绑定 `notesSlide`（`[Slide N Notes]`）、SmartArt (`diagramData`) 与 `chart*.xml`。Chart 必同时解析 **`<c:numFmt formatCode="..."/>`**（含 `%` 时 `Decimal * 100` 显式输出 `%`）与 **`<c:valAx><c:dispUnits><c:builtInUnit val="thousands|millions|billions"/>`**（防 `$45.2B` 误抽成 `45.2`）；标记隐藏页 `<p:sld show="0">` 为 `[Hidden Slide]`；正确展开表格合并单元格 `gridSpan`/`rowSpan`；`\f` 分页兼容 `document_pages`（`method="pptx_slides_v1"`）；纯图页（文本 `<30` 字符且含 `<a:blip>`）标 `visual_only` 写入 `open_gaps`。
   - **DOCX 修订洁净提取**：流式读取 `word/document.xml` 段落 `<w:p>` 与表格 `<w:tbl>`（转 Markdown 表格），**显式跳过 `<w:del>` 删除修订、仅保留正文与 `<w:ins>` 插入修订**。
2. **混合型 IR PDF 智能挑页与两段式不可变 OCR（R2-D3）**：
   - **页面状态升级**：`classify_page_text` 将 `<120` 有效字符（且非纯空白/纯页码）标为 **`"text_partial"`**，保留原始短页眉/页脚文本不清空；定义 `TEXT_STATUSES = frozenset({"text", "ocr", "text_partial"})`（使 `page_for_offset`、`page_range_to_chars`、`page_map_from_text`、`_extract_small_pdf`/`_extract_large_pdf` 可用页检查全部正常命中，杜绝全页页眉 IR PDF 报 `422 ocr_required`）与 `OCR_CANDIDATE_STATUSES = frozenset({"needs_ocr", "text_partial"})`（同步接入 `_extract_small_pdf`、`_extract_large_pdf`、`_ingest_ocr`、`merge_ocr_into_document`、`run_source_ocr` 与 `outline_source`）；贯通 `text_order: Literal["layout", "reading"]`（`reading` 不传 `-layout`，统一替代 `research_uploads.py:180` 的 `-raw`）。
   - **智能挑页优先级**：全页极速文本层扫描后，跳过第 1–2 页封面/Safe Harbor/目录等低价值页，优先挑页眉或邻近页命中 `Guidance / Outlook / Financial / Margin / Reconciliation / Non-GAAP / Capital / CapEx` 或数字表格密度的关键页；对 **0 文本全扫描 PDF** 采用“跳过前 2 页 + 中段核心与末段财务附录战略采样”。
   - **两段式防 `524` + 不可变源版本一致性**：
     - **Stage A（同步 `/upload`）**：单路 150 DPI（`FORESIGHTNINE_OCR_WORKERS=1`，`_ocr_one` 透传 `dpi=150`，保留 `OMP_THREAD_LIMIT="1"`，**不盲降 `prlimit --as`**），硬上限 **`<=25s / <=4` 页**优先高价值页 OCR；
     - **Stage B（V3a 异步 Operation Layer 0b）**：若仍存在未扫高价值 `"text_partial"` / `"needs_ocr"` 页，在带 SSE 15s 心跳的 Layer 0b 补扫剩余 **`<=8` 页**，通过 `build_ocr_pages_callback` 持久化为**不可变新版本 `ResearchSource`（`ocr_source_id`, `supersedes_source_id`）**（Topic 未锁定时自动挂载）。V3a 所有解析事实与引文均绑定该 `ocr_source_id`，彻底保证 L3 `ReaderPanel` 偏移零误差；仍未扫页码完整写入 `open_gaps`。
3. **`parse_transcript_v2`（多格式多语言、口径侦测、字幕去重与负向原型过滤）**：
   - **非破坏性偏移不变量**：页眉/页脚/版权/Safe Harbor 仅写 `boilerplate_spans` 掩码，不修改 `source.text`；**硬保护开场白前 1,000 字符内的口径声明句**（如 `"Unless otherwise stated, all margins, OpEx, and EPS are Non-GAAP"`），提取 `call_default_basis: Literal["non_gaap", "gaap", "unspecified"]`，绝不被 Safe Harbor 掩码误吞。
   - **`.vtt` / `.srt` 字幕清洗**：剥离序号/时间轴、解析 `<v Name>`/`Name:` 发言人标签，并对相邻 cue 执行 **rolling-line dedup（滚动字幕去重）**消除 YouTube/Zoom 自动字幕 2–3 倍重复，同时在每个 `TranscriptTurn` / `QaExchange` 保留 `timestamp_start`。
   - **参与者名录先验 + 6 格式多语言状态机 + Slide 交叉锚定**：支持单行中英冒号、Motley Fool/Seeking Alpha/Quartr 多行格式、时间戳 `[00:14:22]`、Markdown 粗体/标题、中文 `【提问】`/`【回答】`/`Q1:` 与 Operator 叫号解析；无结构纯文本诚实降级 `unstructured_fallback`；正则提取口述 `"turning to slide 14"` / `"第 14 页"` 与同季 PPTX/IR PDF 建立双向锚点。
   - **常规上传嗅探与负向原型过滤（R2-D1）**：`POST /v2/research/call-audit/preview`（传 `source_ids`，`<150ms` 0-LLM）先跑负向原型过滤（`form_10k_10q`、`macro_report`、`legal_contract` 直接拒判），仅 `archetype in ("transcript", "ir_slide_deck")`、`confidence >= 0.75` 且命中 `>=2` 项强结构特征（发言人交替+Q&A 段 / 季度财报标题+财务表 / PPTX 含 Guidance·Financial·Q1–Q4）才允许静默自动触发。
   - **非 SEC 公司规范化与别名归并（D1）**：无 SEC CIK 时先走 `fin_data/cn.py`（`parse_cn_code`/`parse_hk_code`）规范化为 `nonsec:HKEX:0700` / `nonsec:SSE:600519`；纯名称先查账户级别名表 `v3a_entity_aliases` 映射至规范 `entity_key`，未命中则生成 `nonsec:{slug}`，并支持前端角落纠偏 chip 一键调用 `POST /v2/research/call-audit/entities/merge` 合并历史 `entity_key`。
   - **0-LLM 语言学先验特征**：为每轮 `QaExchange` 计算 `asked_quantitative` vs `answered_quantitative`、`entity_overlap_ratio`、`hedge_density`、`verbosity_ratio`、`numeric_guidance_density`、`multi_part_question_hit`、`analyst_cluster_topic_hit`、`thesis_or_flag_hit`。

### 5.2 Layer 1：首发口径对齐的 10-Q 账本底座与无状态共享 XBRL 库

1. **`STANDARD_LINES`（49 → 55 项）与 `PEER_COMPANY_SNAPSHOTS v4`**：
   - 新增 6 项通用科目（带备选标签链，缺失标 `footnote_unstructured_manual`，绝不当 `$0`）：
     - `rpo` (`footnote`, `flow=False`, `money`): `RevenueRemainingPerformanceObligation`, `UnsatisfiedPerformanceObligation`
     - `deferred_revenue_current` / `deferred_revenue_noncurrent` (`balance`, `flow=False`, `money`): `ContractWithCustomerLiabilityCurrent/Noncurrent` + IFRS 对应项 + 总计 `ContractWithCustomerLiability` 兜底
     - `restructuring_charges` / `impairment_charges` (`income`, `flow=True`, `money`): 覆盖 `RestructuringCharges`, `RestructuringSettlementAndImpairmentProvisions`, `AssetImpairmentCharges`, `GoodwillImpairmentLoss`
     - `purchase_commitments` (`footnote`, `flow=False`, `money`): `UnrecordedUnconditionalPurchaseObligationBalanceSheetAmount` (ASC 440), `PurchaseObligation`, `LongTermPurchaseCommitmentAmount`
   - `_V2_PROJECTION_LINES` 升级至 `projection_version = 4`：增投 `accounts_receivable`, `inventory`, `accounts_payable`, `rd_expense`, `sga_expense`, **`selling_expense`, `admin_expense`, `total_operating_costs`**（解决 META/NVDA/MSFT/GOOGL 拆分 OpEx 漏算）、`depreciation_amortization`, `ppe_net`, `eps_diluted`, `eps_basic` 及上述 6 项；
   - **FPI 币种与每股单位加固**：`project_companyfacts_to_snapshot` 复用 `sec_edgar.py:404-425` 的通用 ISO-4217 3 字母正则 `_currency()` 与 `_pick_unit(..., "per_share")`，根除非 9 大币种 FPI 被静默标成 USD 及 `eps` 脏单位放行。
2. **极限低内存拉取与解析（适配 `512M` cgroup）**：
   - **共享流式落盘辅助 `sec_get_to_tempfile()`（`sec_events/pacing.py:459-473`）**：走共享 `SecPacingLimiter` + `stream_transport` 直接流式落磁盘临时文件，避免 `sec_get_json`/`sec_get_bytes` 将 60MB `raw_body` + 60MB `str` 驻留内存；
   - **`companyfacts` 45 科目全量对应 XBRL Tag 切片增量 `raw_decode` 投影器**：纯 stdlib 动态汇总 `_V2_PROJECTION_LINES` v4（45 项科目）映射的全部 `us-gaap`/`ifrs-full` tag（约 120+ 个）及 `dei.EntityCommonStockSharesOutstanding`，直接在临时文件上按 tag 定位字节区间增量 `raw_decode`（跳过其余 500+ 无用 tag），将 60MB `companyfacts` 峰值内存从 **~250MB 压至 ~25–35MB**；
   - **10-Q/10-K 64KB 流式 `HTMLParser`（巨头 100% 覆盖轨道 5）**：复用 `sec_get_to_tempfile()` 落盘后以 64KB 分块 `HTMLParser` 实时丢弃 `<ix:header>`/`<style>`/`<script>` 与内联 XBRL 冗余属性，45MB MSFT/JPM 10-K 峰值内存 **`<5MB`**、输出 ~400KB 纯文本交 `split_filing_sections(is_html=False)`，优先复用 `EARNINGS_SQUAD_STATES` / `sec_10q_redline_cache`；
   - **附注兜底**：`companyfacts` 标量缺失时拉 `FilingSummary.xml`（~40KB）+ 最多 **3 张 `R*.htm`**（`fetch_filing_table` 传 `max_bytes=1_500_000`）。
3. **四时间戳可用性契约、双锚定会计期 `P` 与首发口径重建（R2-D2）**：
   - **双锚定会计期 `P`**：先由 Layer 0 抽 `call_date`，再查 SEC `submissions` 中 `call_date` 前 **0–65 天内**对应的 8-K Item 2.02 / 10-Q / 10-K 的 `reportDate` 与 XBRL `(fy, fp)` 双向校准期 `P`（解决 NVDA/MSFT/AAPL/WMT 非自然财年错位）；
   - **历史季首发口径重建（防重述/拆股假打假）**：核验历史季 `P` 时，先对 `PeerCompanySnapshot.facts_by_line` 原始 rows 按 **`filed <= period_P_initial_10q_filed_at`** 过滤，再调用 `_derive_ytd_quarters`（`sec_edgar.py:283-370`），一次性解决剥离业务重述、10:1 等拆股后复权 EPS 单位错配、以及 6M/9M YTD 现金流单季反推时点一致性；若最新归档值相对首发值变动 **`>1%`**，旁注 `restated_later` 并同时展示首发值与重述值；
   - **`P+1` 即时回测可用性契约**：严格要求 `p1_10q.filed_at <= as_of` 且 `p1_10q.filed_at > call_date`；
   - **无状态共享 XBRL 实绩查询纯函数库**：抽离 `xbrl_actuals_lookup(snap, metric_canonical, target_fy, target_fq, *, basis_lock, initial_filing_cutoff, as_of)` 供 V2.5（5 指标）与 V3a（12 指标）共同调用代码，**保持存储层绝对物理隔离（V3a 绝不回写全局 V2.5 `GUIDANCE_CREDIBILITY_LEDGER`）**。

### 5.3 Layer 2：Prompt Cache 最大化、65/35 双桶上下文、分权威增量缓存与截断重置

1. **Prompt Cache 友好拆分（同时兼容 `anthropic_messages` `explicit_cache_control` 与 `openai_chat_completions` 隐式前缀缓存）**：
   - **`system` 消息（100% 纯静态，跨所有公司/季度字节级不变）**：仅放角色定义、EvasionBench 三档与策略子类（含 `selective_part_omission`）、`Conscious Incompetence` 弃权纪律、原子事实 JSON Schema 契约与反注入规则；
   - **`user` 消息首段（动态前置上下文，`<=2,500 chars`）**：放该公司历史未结口头承诺（含上季高调主题供 Silent Drop 排查）+ `ThesisLedger` 3–5 条支柱关键词 + `call_default_basis`。
2. **4 区确定性预算（单主调硬上限 `<=32,000 chars`，由规则 `context_budget` 驱动）**：
   - **Zone B（`<=16,000 chars` · 65/35 双桶分层配额）**：
     - **A 桶（65% ≈ 10.4KB · 回避与核心争议优先）**：按 `evasion_prior_score + thesis_or_flag_hit + followup_count` 降序选入**完整 Q&A 回合原文**（绝不截断单回合）；
     - **B 桶（35% ≈ 5.6KB · 量化指引与集群热点优先）**：从未入选轮次中按 `numeric_guidance_density + multi_part_question_hit + analyst_cluster_topic_hit` 降序补入完整回合（防止漏掉管理层爽快给出的 CapEx/利润率硬承诺）；
     - 尾部附全量未入窗 Q&A 索引目录（`Q# | 分析师 | 主题词 | 约 X 字符`），明确告知边界防编造未读轮次。
   - **Zone C（`<=10,000 chars` · 去重保真）**：与同期 8-K Ex-99.1 做段落级 5-gram Jaccard 去重（`>75%` 仅留 1 行锚点），优先保留 CFO 自由表述、PPTX 表格/Chart XML 与 Speaker Notes；**强制保护开场白前 1,000 字符口径声明句**。
   - **Zone D（`<=2,000 chars` · 同季管理层附加材料摘要）**：仅承接同季多份管理层一手材料（如 PPTX/IR PDF）的 Map 摘要（入主调前必经 `normalize_with_raw_map` 引文验真）；不塞大段 10-Q 全文。
3. **分权威增量融合（R2-D6 延迟 & Token 双降 75%）**：
   - 管理层一手材料主抽取缓存在 `v3a_l2:{account_id}:{entity_key}:{mgmt_bundle_sha256}:{l2_prompt_version}:{context_budget_version}`；
   - 每份卖方研报独立缓存在 `v3a_sellside:{account_id}:{entity_key}:{source_id}:{content_sha256}:{sellside_prompt_version}`（单次轻量抽 `<=8KB`，`<10s`，提取 `sellside_expectations` 数值、前瞻/点评类别与核心质疑点）；
   - **追加卖方研报时 0 次重跑管理层主纪要抽取**，仅由 Layer 3 `<10ms` 内存融合。
4. **有界调用、截断感知缩容重置与反 Prompt Injection**：
   - 仅多文档管理层联合或 Q&A `>28KB` 时触发 2-Stage Map-Reduce（执行安全上限 `<=3` 次调用；遵循 `AGENTS.md`，不向用户静默设置运行预算，如实记录实际调用数、Token 与耗时）；
   - **`invoke_and_parse_with_repair` 截断缩容重置**：检测到 `finish_reason == "length"` 或末尾 JSON 截断时，显式传递 **`reset_conversation=True`**（切 `turn_index=0` + 新 `conv_id` 后缀绕过 `_AccountSlotModelRunner._turns` 粘滞），下发单轮精简指令（`claims<=12`、`evasions<=12`、单条引文 `<=200 chars`）；
   - **安全隔离**：12 位随机 `nonce` 不可信边界 + 转义伪造边界 + Unicode `Cf/Cc` 清洗 + **`tools=()`** + 服务端白名单验证 `source_id` 归属当前 `account_id`。
5. **原子化 6 类客观事实 Schema（严禁落盘红/黄/绿灯或最终白名单裁决）**：
   - ① `management_factual_claims` / ② `verbal_commitments`：`metric_canonical`、`kpi_key`（非标经营 KPI）、`basis`（`gaap | non_gaap | operating_kpi | unknown | inherited_call_default`）、`period_scope`（`single_quarter | ytd | full_year | ttm | multi_year | point_in_time`）、`target_fy`、`target_fq`、`operator`（7 类）、`value_low`、`value_high`、`unit`、`scale`、`evidence_sufficiency`（`explicit | implied | ambiguous`）、`speaker_role`、`source_id`、`verbatim_quote`；
   - ③ `qa_evasions`：`exchange_index`、`analyst_name`、`executive_name`、`executive_role`、`sub_questions`、`unanswered_sub_questions`、`topic_tags`、`question_asks_sensitive_detail` (bool)、`exec_cites_confidentiality_policy` (bool)、`evasion_level`（`direct | intermediate | fully_evasive`）、`behavior_subtype`（`omission | selective_part_omission | vagueness_obfuscation | topic_pivot | restatement`）、`followup_count`、`question_quote`、`answer_quote`；
   - ④ `internal_contradictions`：双逐字引文 + `speaker_a_role` vs `speaker_b_role`（供轨道 0 识别 CEO vs CFO 跨角色分歧）；
   - ⑤ `expectation_signals`：`>=2` 位分析师集群追问热区 + 卖方预期数值（仅来自 `v3a_sellside`）；
   - ⑥ `skeptic_notes`：每项冲突/高危回避/Silent Drop 的中性反方解释。

### 5.4 Layer 3：零漂移反投影、7 轨买方高精度对撞与预期差隔离

1. **零漂移引文门控**：
   - `normalize_with_raw_map(raw_text, mode)` 为每个规范化字符精确记录 `(raw_start, raw_end)`（精准处理 `ﬁ→fi` 连字扩张与 `norm_quote_table_tolerant` 增空格）；
   - **局部窗优先匹配**：先在引文所属 `turn_id` / `slide_index` 偏移窗内匹配，未中再退回全文档匹配，根除短数字引文跳回第 1 页误高亮；
   - 经 `page_for_offset(document_pages, raw_start)` 解析页码/Slide/时间戳，并以严格 2-tuple 解包 `warn, surr = check_negation_context(match, markers=DEFAULT + rules.negation_context_markers)` 拦截否定/条件语境误判。
2. **7 轨确定性对撞（6 项买方高精度增强，严格与卖方预期差隔离）**：
   - **全局 `Conscious Incompetence` 门控**：仅 `evidence_sufficiency == "explicit"` 允许触发红/黄硬对撞，`implied / ambiguous` 降级为低权重观察线索，严防模糊定性被脑补打假；
   - **轨道 0（当天生效）· 同场自相矛盾 & CEO vs CFO 跨角色分歧**：Remarks vs Q&A / PPT vs Q&A 内部退缩，其中 **CEO vs CFO 跨角色口径分歧**（如 CEO 讲高景气、CFO 在 Q&A 下调毛利/现金流口径）优先置顶；
   - **轨道 1 · 需求真实性与渠道健康**：AR/Rev、Inv/COGS、DSO/DIO、RPO/递延收入 YoY（金融 SIC 豁免）；
   - **轨道 2 · 盈利质量与现金转化**：OCF/NI、FCF/NI、毛利率/经营利润率 YoY bps、连续重组/减值；**严格 GAAP/Non-GAAP 口径锁（含 `call_default_basis` 继承）+ 期间口径锁（`period_scope_mismatch` 透明披露）**；
   - **轨道 3 · CapEx 与供应承诺**：CapEx YoY、PPE 斜率、ASC 440 无条件采购承诺（周期锁 + 通用 ISO-4217 币种锁）；
   - **轨道 4 · 资本配置与真实回购**：直连 V2.5 `CapitalAllocationAudit`（零额外拉取）；
   - **轨道 5 · 10-Q 悄悄新增风险（Omission）**：仅对 `sec_redline_diff` 判为实质新增的段落执行 **`风险类别桶（Regulatory / Customer Concentration / Supply / Export Control / Litigation / Liquidity / Cyber / Going Concern）+ 词干实体覆盖`** 双因子 0-LLM 初筛（消除 `"tariff"` vs `"customs duties"` 同义误报），命中后触发 D4/D5 门控自动 Phase 2；
   - **轨道 6 · 跨季口头承诺兑现、全年指引漂移、Silent Drop 分层 & 高管文本基线漂移**：
     - 覆盖 **12 项 XBRL 量化指标**（`revenue`、`gross_margin`、`operating_margin`、`operating_income`、`net_income`、`eps_diluted`、`operating_cash_flow`、`free_cash_flow`、`capex`、`rd_expense`、`sga_or_opex`、`rpo_or_deferred_revenue`，含 `P+1` 即时回测）× **7 类算子**（`ge` / `le` / `between` / `approx` / `growth_yoy_ge` / `growth_yoy_le` / `direction_up_down`）× **3 类核验级别**（`xbrl_auto` / `8k_nongaap_manual` / `next_call_check`）+ **`operating_kpi_next_call`**（结构化追踪 ARR/NRR/Data Center/产能等非标经营 KPI）+ **`qualitative_milestone`** + **全年指引漂移/区间拓宽跟踪（Guidance Range Drift）**；
     - Silent Drop 分层为 **`silent_drop_high_risk`**（上季高调提出或未到期承诺本季 Remarks+PPT 零提及且无收官说明）vs **`likely_concluded_or_unasked`**（附 Skeptic 中性解释）；
     - 累计 **`>=2` 季**后启用 0-LLM **高管自身文本基线漂移（Speaker-Baseline Shift）**（`hedge_density_delta` / `verbosity_ratio_delta` / `evasion_rate_delta`）。
3. **Q&A 回避审计增强（Tab 2）**：
   - 精准展示**复合问题选择性漏答**（`sub_questions` vs `unanswered_sub_questions`、`selective_part_omission`）；
   - 执行 **D3 双条件商业保密门控**（`question_asks_sensitive_detail AND exec_cites_confidentiality_policy AND behavior_subtype != "topic_pivot"`），防高管滥用 `"confidential"` 挡箭牌。
4. **4 层预期差参照（`ExpectationBenchmarkProvider` Protocol，物理隔离于 Tab 3）**：
   - ① 只读引用 V2.5 公共 `GUIDANCE_CREDIBILITY_LEDGER` 历史信用先验；② Q&A `>=2` 位分析师集群追问热区；③ 上传卖方研报（标明 `third_party_opinion` 与 `前瞻/点评`）；④ V3b BYOK 扩展缝（`ExternalEarningsService.get_consensus()`，严守 `export=False`）。

---

## 6. 规则改变零重建：存储集合与外置规则引擎

### 6.1 零 SQL 迁移 Postgres `research_state_documents` 集合

全部用户集合强制携带顶层 `"owner_id"`（兼容 `items_for_owner`）、所有 `T | None` 字段显式 `= None`（兼容 `_drop_legacy_null_fields`）、热路径严格主键 `get`/`get_many` **零 `.items()` 全表扫描**；多键索引与清单写操作一律走短事务 `with_advisory_lock(f"v3a_man:{account_id}")`。

| 集合名 | 主键格式 | 隔离属性 | 持久化内容 |
| --- | --- | --- | --- |
| `v3a_doc_parsed_facts` | `v3a_doc:{account_id}:{source_id}:{content_sha256}:{parser_version}` | 账号隔离 (`owner_id`) | Layer 0 结构化事实（原型、权威度、`call_default_basis`、会计期 `P`、`QaExchange` 原始偏移/时间戳、Slide 锚点、语言学先验）；修解析器 Bug 升 `parser_version` 自动失效旧缓存 |
| `v3a_call_l2_extractions` | `v3a_l2:{account_id}:{entity_key}:{mgmt_bundle_sha256}:{l2_prompt_version}:{context_budget_version}` | 账号隔离 (`owner_id`) | Layer 2 **管理层一手材料**的 6 类原子客观事实（不含红黄绿灯/白名单终裁） |
| `v3a_sellside_extractions` | `v3a_sellside:{account_id}:{entity_key}:{source_id}:{content_sha256}:{sellside_prompt_version}` | 账号隔离 (`owner_id`) | 单份卖方研报独立抽取结果（前瞻/点评数值与核心质疑点），实现追加研报零重跑主纪要 |
| `v3a_verbal_commitment_ledger` | `v3a_vcl:{account_id}:{entity_key}` | 账号隔离 (`owner_id`) | 跨季口头承诺与高管历史文本基线台账（含 `muted` opt-out、`linked_pillar` 伴生支柱、`operating_kpi`、`guidance_range_history`、`verified_against_accession`）；**绝不写入全局 V2.5 台账** |
| `v3a_account_company_index` | `v3a_idx:{account_id}:{entity_key}` | 账号隔离 (`owner_id`) | O(1) 最近 20 次核验索引（按会计期 `P` 归组同季文档、`awaiting_current_10q` 标志与上次轻量指纹检查时间戳） |
| `v3a_entity_aliases` | `v3a_alias:{account_id}` | 账号隔离 (`owner_id`) | 非 SEC 多语言公司别名到规范 `entity_key` 的映射表（支撑 D1 角落一键合并） |
| `v3a_account_manifest` | `v3a_man:{account_id}` | 账号隔离 (`owner_id`) | O(1) 账号键清单（加 `with_advisory_lock`），支撑删号 O(K) 确定性零残留彻底清理 |
| `sec_10q_redline_cache` | `redline:{cik}:{cur_accn}:{prior_accn}` | 共享只读 SEC 公共事实 | 10-Q/10-K Item 1A / MD&A 实质新增风险段摘要 |
| `runtime_maintenance_leases` | `lease:{task_name}` | 共享运行态维护租约 | `radar_bg_sweep` / `blob_store_prune` 短事务认领租约（含 `holder_id`、`lease_expires_at`、上次完成时间），复用 `sec_events_pacing` 短事务模式（`sec_events/pacing.py:235-252`） |

> 索引可选补强：如后续监测到 `items_for_owner` 集合规模增长，可追加幂等 `003_owner_id_index.sql` 为 `(collection, (document->>'owner_id'))` 建表达式索引；V3a 自身热路径不依赖该索引，全程 O(1) 主键点查。

### 6.2 外置规则文件：`src/apps/api/app/fin_data/adapters/v3a_call_verification_rules.json`

Pydantic `extra="forbid"` + 预编译正则 + 16 位 `rules_sha256` 随报告返回，**双版本显式分离**：
- **`context_budget_version`**（仅影响 Zone B/C 选窗与 65/35 配额，极少变，纳入 `v3a_l2` 主键）；
- **`evaluation_rules_version`**（`parsing_rules`、`evasion_scoring` 含双条件商业保密白名单、`ledger_collision_thresholds`、`negation_context_markers`、`item1a_risk_buckets`、`radar_alert_thresholds`）：**热改即时生效，`<10ms` 内存重算，0 重解析、0 重抓 SEC、0 LLM、0 迁库**。

---

## 7. 稳定性（`512M`）、异步 SSE、延迟 10-Q 自动触发与严格 CSP 前端

### 7.1 `512M / 768M` 内存预算闭环与 `DocumentBlobStore` 磁盘清理接线

1. **进程级内存账本（靶心 `512M` cgroup，实测峰值 RSS `<280–320MB`）**：
   - Python/FastAPI 常驻基线 ~120MB + 全进程 **`HEAVY_MEM_GATE = BoundedSemaphore(1)`** 串行化全部 4 类峰值操作：① `sec_get_to_tempfile()` + 45 科目全量对应 XBRL Tag（约 120+ 个）切片 `companyfacts`（~30MB）；② `sec_get_to_tempfile()` + 64KB 流式 10-Q/10-K HTML（`<5MB`）；③ 单路 150 DPI 灰度 OCR（~80MB，保留 `OMP_THREAD_LIMIT="1"`）；④ `>5MB` OOXML 流式解析（~40MB）。每次完成后显式 `del` + 大对象释放后 `gc.collect(0)`。
2. **`DocumentBlobStore` 与 `ocr-cache` 自动清理接线（解决 `/data/documents` 卷长期增长缺口，归属 `Step V3a-0`）**：
   - 在 `ResearchSources` / `PostgresDocumentRepository` & `InMemoryDocumentRepository` 新增轻量只读方法 **`live_blob_sha256s()`**：Postgres 模式执行单条轻投影 SQL（`SELECT DISTINCT COALESCE(document->'provenance'->'document_pages'->>'original_blob_sha256', document->'provenance'->>'original_blob_sha256') FROM research_state_documents WHERE collection = 'research_sources' AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP) AND COALESCE(document->'provenance'->'document_pages'->>'original_blob_sha256', document->'provenance'->>'original_blob_sha256') IS NOT NULL`），仅拉回 64 字节哈希字符串（内存 `<1MB`，绝不调用 `RESEARCH_SOURCES.items()` 导致反序列化正文 OOM）；内存测试模式直接遍历元数据；
   - **升级 `DocumentBlobStore.prune()`（`research_blob_store.py:132-184`）新增可选参数 `min_orphan_age_seconds: float | None = None`**（在 TTL 与 over-quota LRU 两段均自动保护 `(current_time - mtime) < min_orphan_age_seconds` 的新落盘文件，杜绝并发上传刚 `put_file` 尚未写完 `ResearchSource` 即被 over-quota LRU 误删的竞态）；
   - 在 `retention.py` 新增 `prune_document_blob_store(blob_store, *, live_sha256s_fn, max_age_seconds, max_total_bytes, min_orphan_age_seconds=3600, ocr_cache_max_age_seconds=..., ocr_cache_max_bytes=...)`：同时调用 `blob_store.prune(..., min_orphan_age_seconds=3600)` 与对 **`<blob_store.root>/ocr-cache/*.json`**（`research_ocr.py:465-514`，原 `DocumentBlobStore.prune()` 刻意跳过且当前无磁盘淘汰）执行有界 mtime-LRU / TTL 清理（纯可重建缓存，安全无损）；
   - 在 `main.py` `cleanup_expired_state()` 与 `lifespan` 低频维护循环（默认间隔 `FORESIGHTNINE_DOCUMENT_STORE_PRUNE_INTERVAL_SECONDS=21600`，配 `FORESIGHTNINE_DOCUMENT_STORE_MAX_BYTES` / `FORESIGHTNINE_DOCUMENT_STORE_MAX_AGE_DAYS`）中，通过**短事务租约认领**（`runtime_maintenance_leases` 集合 `lease:blob_store_prune` + `with_advisory_lock("blob-store:prune-claim")`，持有 DB 事务 `<5ms` 后即提交释放，再执行磁盘扫描与 unlink）。

### 7.2 完整异步运行态与三段式 SSE 协议

| 端点 | 方法 / 状态码 | 职责与运行态保障 |
| --- | --- | --- |
| `/v2/research/call-audit/preview` | `POST` `200` | **仅传 `source_ids`**（遵守 `security.py:301-310` `max_body_bytes`），`<150ms` 纯 0-LLM 返回文档原型、负向过滤结果、识别实体/CIK、会计期 `P`、待补 OCR 页数 |
| `/v2/research/call-audit/run` | `POST` `202` / `200` | `Idempotency-Key` + 账号隔离逻辑 Singleflight `(account_id, entity_key, fiscal_period_key, bundle_sha256)`；命中缓存 `<80ms` 直出，未命中返回 `202 Accepted + operation_id` |
| `/v2/research/call-audit/operations/{operation_id}/events` | `GET` SSE | 复用 `ResearchLiveBroker`（每账号上限 6 条，遇 `429`/断连降级 3s 退避轮询），推 **三段式渐进事件**：`layer0_ready`（~0.5s 实体/Q&A/语言学热区）→ `layer0b_ocr`（按需）→ `layer1_ready`（~1.5s 10-Q 账本与盈利质量基线）→ `layer2_extracting` → `completed` + **15s `: heartbeat\n\n`** + `Last-Event-ID` 断点续传 |
| `/v2/research/call-audit/operations/{operation_id}` | `GET` `200` | 轮询兜底与最终状态查询 |
| `/v2/research/call-audit/operations/{operation_id}/cancel` | `POST` `200` | 协作取消（每阶段检查 `cancel_event.is_set()` 并续期 `OperationManager` 300s 租约） |
| `/v2/research/call-audit/report` | `GET` `200` | `cache_only=true` **严格 `<80ms` 且 0 外部外呼**；纯 0-LLM `<10ms` 按最新 `evaluation_rules_version` 与已缓存 10-Q 实时重算 |
| `/v2/research/call-audit/check-filings` | `POST` `200` / `202` | **延迟 10-Q 前台静默指纹检查端点**：服务端在 `v3a_account_company_index` 强制 30 分钟冷却期、单次最多查 5 个 `awaiting_current_10q` CIK，执行纯 0-LLM `submissions` 指纹比对（`<300ms`）；仅在发现新 `10-Q/10-K/20-F` 或 `NT 10-Q/10-K` 时异步触发 Phase 2c 核验并返回 `triggered_operation_ids` |
| `/v2/research/call-audit/commitments/{commitment_id}` | `PATCH` `200` | 角落 opt-out `muted=true/false` |
| `/v2/research/call-audit/entities/merge` | `POST` `200` | 角落非 SEC 别名一键合并 `source_entity_key → target_entity_key`（加 `with_advisory_lock(f"v3a_man:{account_id}")` 原子合并别名、承诺台账与索引） |
| `/v2/research/sources/{source_id}/text` | `GET` `200` | **Owner-scoped 原文兜底端点**（支持可选 `?topic_id=` 查询参数：有 `topic_id` 时同步校验 `topic.request.as_of` 截止线；始终校验 `owner_id` 与 `permissions.display`），返回 `title`、`retrieval_method`、`permissions`、`obtained_at`、`original_available`、`text`、`content_sha256`、`document_pages`，完整支撑锁定 Topic 上传与跨 Topic/跨季 Silent Drop 原文穿透 |

- **防重启孤儿与删号防泄漏**：`lifespan` 注册 `_reconcile_orphaned_call_audits()` 收敛重启前 `running` 作业为可重试 `failed`；删号（`main.py:1764-1783`）在 `with_advisory_lock(f"v3a_man:{account_id}")` 下按 `v3a_account_manifest` O(K) 删除全部 V3a 文档，经 `items_for_account` 同步清理 `THESIS_LEDGERS` / `WATCH_SUBSCRIPTIONS` / `CHANGE_INBOX_ITEMS` 并取消 `_ACTIVE_CALL_AUDIT_RUNS` 活跃任务。

### 7.3 延迟 10-Q 双保险自动触发、雷达 Phase 2c 与 `/inbox` 两级合并

1. **延迟 10-Q 双保险自动触发机制（填补现网仅靠手动 `check-now` 的实时性缺口，归属 `Step V3a-2/3`）**：
   - **保险 1 · 首屏零外呼后的条件式静默 0-LLM 指纹刷新**：`CallVerificationBar` 挂载先走 `GET /report?cache_only=true`（**100% 零外呼 `<80ms` 完成首屏渲染**）；**仅当**响应标明存在 `awaiting_current_10q=True`（或打开 `InboxPage` 存在活跃 `call_commitment_verify` 订阅）且距上次检查超过 **30 分钟冷却期**时，前端在首屏渲染后异步静默调用 `POST /v2/research/call-audit/check-filings`（服务端同步强制 30m 冷却与单次 `<=5` CIK 上限，走短 TTL 缓存与 `SecPacingLimiter`）；发现新 `10-Q/10-K/20-F` 或 `NT 10-Q/10-K` 时立即后台启动 Phase 2c 核验与 `/inbox` 更新。
   - **保险 2 · API `lifespan` 可开关后台定时巡检协程**：受环境变量 `FORESIGHTNINE_RADAR_BG_ENABLED`（默认 `true` 于常驻环境、测试环境默认 `false`）与 `FORESIGHTNINE_RADAR_BG_INTERVAL_SECONDS`（默认 `3600`）控制。每次唤醒先通过**短事务 Postgres advisory lock**（`with_advisory_lock("radar:bg-sweep-claim")`）在 `runtime_maintenance_leases` 集合原子认领 `lease:radar_bg_sweep`（`lease_expires_at = now + 300s`）后**立即提交释放数据库连接**（绝不在 SEC/LLM I/O 期间持有 DB 事务，且天然兼容单进程 `compose.yaml` 与未来多副本水平扩展）；单轮巡检设批上限（每次最多处理 `<=20` 条到期 `next_check_at <= now` 订阅并在子批间短事务续约防超时双跑）；**按 `account_id` 分组并抽离无 `http_request` 依赖的账号级 runtime 解析器**（替代 `main.py:8103` 对 `http_request` 的耦合，确保账号 A 与账号 B 的模型凭据与槽位严格隔离），全程串行受 `HEAVY_MEM_GATE` + `SecPacingLimiter` + `_platform_llm_concurrency_gate()` 约束。
2. **雷达 Phase 2c 与 R2-D4 两级合并契约**：
   - `call_commitment_verify` 运行于 **Phase 2c（严格在 Phase 2b `earnings_delta` 写完并追加 `existing_inbox` 之后）**，独立维护 `seen_accessions[acc]["call_verify"]`，无 `ThesisLedger` 的普通 Topic 照常触发；
   - **两级匹配规则**：`_earnings_delta_for_sub` 与 Phase 2c 均在 `ChangeInboxItem`（`research_radar_scheduler.py:92-121`）写入 `report_date`、`fiscal_period_key`、`has_confirmed_ledger`；合并时① 精确匹配未读 `earnings_delta` 的 `(account_id, cik, accession)`；② 未命中时按同报告期 `(account_id, cik, report_date / fiscal_period_key)` 匹配未读 `earnings_delta`（兜底：同 `cik` 在 `[10q_filed - 45d, 10q_filed + 7d]` 内未读 `earnings_delta` 卡）；
   - **合并防埋没与防漏警**：合并至未读卡时，写入 `scorecard_diff.v3a_call_collision` 折叠区块，执行 **`impact_level = max(old, v3a)`**、合并 `affected_pillars` 并在卡片主摘要末尾加高可见徽章（如 `含电话会核验：2 项冲突 / 1 项承诺违背`）；若同报告期 `earnings_delta` 卡已处于 `accepted / dismissed_with_note` 或不存在，则独立发出最多 1 张 `call_commitment_verify` 聚合卡；
   - **`NT 10-Q / NT 10-K` 顺带告警**：零额外 SEC 请求检出延期归档通知时直接发高危提示卡。

### 7.4 前端三层有效内容密度与严格 CSP（`style-src 'self'`）

- **挂载点与零外呼契约**：`CallVerificationBar.tsx` 挂载于 `TopicPage.tsx` `{squadMode ? ...}` 与 `{squadDossier ? ...}` **外部**（普通 Topic 与财报小组均可用；存在 Squad 时紧邻 `GovernanceAuditBar`），挂载首请求严格为 `GET /v2/research/call-audit/report?cache_only=true`（`<80ms`，**0 外部外呼**）；仅当返回 `awaiting_current_10q=true` 且距上次检查 `>30m` 时，首屏渲染后异步触发 `POST /call-audit/check-filings`。
- **零摩擦上传与常规感知**：与 `AddSourcesPanel` 共享源列表；常规上传命中纪要/PPT 时自动亮条静默开跑并弹非阻塞轻提示 `「识别为 Q3 FY26 电话会材料，正在后台核验 · [取消]」`；角落提供实体/会计期静默纠偏 chip（含非 SEC 别名一键合并历史 `entity_key`）。
- **L1（30 秒条）**：红/黄/绿灯 + **1 句最致命结论 Headline**（如 `🔴 Q3 FY26 · 库存增速超营收 +26pp 与“渠道健康”表述矛盾 · 回避 2/11 轮`）+ 精简计数徽章 + `[展开面板]` + `[追加材料]`。
- **L2（3-Tab 高密度面板 + 顶部 1 键「复制 Markdown 审计简报」，100% 静态 CSS 零内联样式）**：
  1. **Tab 1 · Talk vs. Walk（口头 vs 账本 & 自相矛盾）**：紧凑双栏对撞卡（`管理层原话 [Slide/页码/时间戳]` ⇄ `10-Q 首发账本实数 + 公式` + `Skeptic 校验` + 口径/周期/重述徽章），按 `直接矛盾(红) → CEO/CFO 跨角色分歧与同场退缩(红/黄) → Non-GAAP 调节差(黄) → 10-Q 遗漏风险(黄) → 待10-Q 自动核验(灰) → 已印证(绿)` 排序；
  2. **Tab 2 · Q&A 回避雷达**：顶部单行响应分布微条（**纯静态 CSS class / 离散分档比例段或 SVG 属性，严禁任何内联 `style={{ width }}`**），默认突出 `fully_evasive` / `intermediate` 与**复合问题选择性漏答子问**，展示追问轮数及 `>=2` 季高管自身文本基线漂移；默认折叠 `direct` 与双条件 `legitimate_confidentiality`（一键可展开）；
  3. **Tab 3 · 跨季承诺台账 & 预期差 + Skeptic 校准**：分区展示 `待 10-Q 核验` / `已验真（违背 · 高危 Silent Drop · 全年指引漂移 · 兑现）`（每条角落留「忽略」opt-out）/ `分析师集群追问热区` / `卖方预期差（third_party_opinion · 前瞻/点评隔离区）`；常驻 `Skeptic 反方校正`、`引文验真徽章`、`DualEvasion 文本核验边界声明`、`rules_sha256 / model / tokens`；Markdown 导出严格执行 `permissions.export` 脱敏。
- **L3（跨 Topic / 跨季原文穿透）与 `InboxPage` 完整适配**：
  - 点击任意引文调用 `handleInspectSource(sourceId, \`chars:${rawStart}-${rawEnd}\`)`，`ReaderPanel` 在当前 Topic 未挂接或跨季引用旧 Topic 纪要时自动回退 owner-scoped `GET /v2/research/sources/{source_id}/text?topic_id={topicId}`（利用其返回的 `title` 与权限元数据避免标题退化为裸 `source_id`），顶部直观展示 **`第 N 页 / Slide N / 00:14:22`**；
  - 升级 `InboxPage.tsx:58-65, 160-169`：新增 `"call_commitment_verify"` → `"电话会承诺核验"` 标签、渲染 `scorecard_diff.v3a_call_collision` 折叠区与 `call_audit_url` 深链、依据 `item.has_confirmed_ledger`（而非仅看 `item.topic_id`）在无确认 `ThesisLedger` 时将主按钮显示为「确认知晓并查看核验面板」；配套 Vitest **CSP 零内联样式断言**与**挂载零外呼断言**（普通 Topic / 冷却期内严格 0 外呼；仅 `awaiting_current_10q=true` 且过 30m 冷却才触发 `/check-filings`）。

---

## 8. 分阶段实施路线、上线前探针与受控验证实验计划

### 8.1 渐进实施路线（每步独立测试回归、独立清晰提交）

1. **`Step V3a-0`（前置底座排雷与 `512M` 内存/磁盘防线 · 0 LLM）**：
   - **优先锁死 3 个核心回归用例**：① `main.py:8124` `seen_v1` 二次巡检崩溃；② `lib/sources.ts:273-275` `<=5MB` PDF 绕过流式管线与 OCR；③ `8-K` vs `10-Q` 不同 accession 导致 R2-D4 合并失配；
   - 完成 §3 全部 9 类前置修复：`RetrievalMethod` 扩容 + 全部 8 处下游分发（`main.py`、`research_earnings_squad.py`、`research_coverage_map.py`、`research_reconciliation.py`、`research_table_provenance.py`、`EvidenceViews.tsx`、`RecordPanel.tsx`、`copy.ts`）、`usesStreamingUpload` 全量流式化 + `AddSourcesPanel.tsx` / `RecordPanel.tsx` / `api/types.ts` / `lib/sources.test.ts:123,128`、**4 处 `original_blob_sha256` 回读**（含 `reextract_research_pdf` `main.py:2910-2927`）+ `research_upload_stream.py:101-112` Blob 优先零双写 + `main.py:2871,2966-2973` 分发与 `.pptx`/`.docx` MIME 放行 + 新增共享 `_PAGED_RETRIEVAL_METHODS`、`"text_partial"` 全链路 14 处页状态对齐（含 `research_fetch.py:1220,1226,1301-1306` 与 `research_documents.py:104,208,299,343`）+ `text_order` 贯通 + `_ocr_one(dpi=150)` + 同步 `<=25s/<=4页` 安全阀；
   - 落地进程级 `HEAVY_MEM_GATE`、`sec_events/pacing.py` `sec_get_to_tempfile()`、45 科目全量对应 XBRL Tag（约 120+ 个）切片增量 `raw_decode` `companyfacts`（`PEER_COMPANY_SNAPSHOTS v4` 含 55 项 `STANDARD_LINES` + 拆分 OpEx + 通用 ISO-4217 + `per_share`）、64KB 流式 10-Q/10-K `HTMLParser` + `fetch_filing_table(max_bytes)`、`DocumentBlobStore.prune(min_orphan_age_seconds=...)` + `ocr-cache` 有界清理接入 `retention.py`（配 `live_blob_sha256s()` 轻投影 SQL 与 `runtime_maintenance_leases` 短事务租约）；
   - 注册全部 4 个 V3a stage（`v3a_call_extract` `240s/16384`、`v3a_call_map_doc` / `v3a_sellside_extract` / `v3a_omission_verify` `180s/8192`）至 `_SQUAD_STAGE_TIMEOUT_FLOORS` & `_SQUAD_STAGE_MAX_OUTPUT_TOKENS`、升级 `invoke_and_parse_with_repair` 支持 `reset_conversation=True` / `turn_index=0`；实现并单测 `normalize_with_raw_map`；修 `check_negation_context` 2-tuple、`_earnings_delta_for_sub`、`ChangeInboxItem` 可选字段（`report_date`/`fiscal_period_key`/`has_confirmed_ledger`）、`upsert_inbox_item_with_folding` 3-tuple、全仓 `.items()` 清理（含 `items_for_account` 防 `account_id`/`owner_id` 失配）、删号关联集合与锁保护、`scan_sec_material_events` 输出 `periodic_filings`（含 `NT 10-Q/10-K`）、`GET /v2/research/sources/{source_id}/text`（含 `?topic_id=` 与展示元数据）；
   - **离线只读基准验证**：对 5 家代表美股（`NVDA / MSFT / META / JPM / TSM`）实测 45 科目全量 Tag 切片投影峰值 RSS 与 JSONB 体积、64KB 流式解析 25–45MB 10-K/10-Q 耗时与 RSS、以及新增 6 项 `STANDARD_LINES` 与拆分 OpEx 标签命中率。
2. **`Step V3a-1`（Layer 0 + Layer 1 + Layer 3 外置规则引擎与离线单测 · 0 LLM）**：
   - 实现 `research_pptx.py`（流式解压硬截断 + `.rels` + `numFmt` & `dispUnits` + 隐藏页 + 合并单元格）、`research_docx.py`（跳 `<w:del>`）、`parse_transcript_v2`（6 格式多语言 + VTT/SRT 滚动去重与时间戳 + `call_default_basis` + Slide 交叉锚定 + 负向原型过滤 + 非 SEC 规范化与别名映射）、`v3a_call_verification_rules.json`（双版本分离）、`Sec10QCollisionLedger`（双锚定会计期 `P` + 首发口径重建与 `restated_later` + `P+1` 四时间戳回测 + 无状态共享 XBRL 纯函数库）与 7 轨对撞引擎（含 6 项买方高精度增强）；
   - 将 `parse_transcript_v2` 接入 Direction A `transcript_index`（D6），回归 `EarningsDossierView.test.tsx` 与 `research_earnings_export.py:289-305`，验证用户自有纪要（`export=True`）不被误脱敏且 FMP BYOK（`export=False`）仍 100% 严遮；
   - 全量离线单测覆盖：Zip 炸弹/路径穿越/XXE 拒收、重排 Slide/百分比与 billions Chart/隐藏页 PPTX、含 `<w:del>` DOCX、滚动重复 VTT/SRT、6 格式中英电话会、混合型 `<=5MB` IR PDF `"text_partial"` 挑页与 `normalize_with_raw_map` 局部窗 0 误差、开场白隐式 Non-GAAP 继承、拆股/重述历史季首发口径对撞、双条件保密白名单、复合问题漏答、双因子 Item 1A 初筛、改 `evaluation_rules_version` `<10ms` 零重建、非 SEC 降级与别名合并、删号 O(K) 零残留。
3. **`Step V3a-2` 入口门控：配置化 `deep_reasoner` 最小运行态探针（固定 2 次调用，留档实测）**：
   - 在跑 Layer 2 全量实验前，对当前 staging 已配置的 `deep_reasoner` 部署（当前为 `glm-5.3-flash`，业务代码不硬编码品牌）执行 1 次最小探针，实证锁定：① 实际生效的 wire API 协议（`openai_chat_completions` vs `anthropic_messages`）与 capability profile；② `max_output_tokens=16384` 中 `reasoning_content` 与可见 JSON 的实际配额切分（确认能否稳定容纳 12 claims + 12 evasions 而无截断，否则微调输出上限或提示词精简档位）；③ 连续两次请求间纯静态 `system` 前缀是否触发 `cache_read_input_tokens` 命中。
4. **`Step V3a-2`（Layer 2 异步引擎 + 三段式 SSE + 延迟 10-Q 双保险与雷达 Phase 2c + R2-D5 + 受控实测）**：
   - 实现 `research_call_verification.py` Layer 2（纯静态 `system` + 动态 `user` 前缀、Zone B 65/35 双桶、分权威缓存 `v3a_l2` vs `v3a_sellside`、Layer 0b 不可变新版 `ResearchSource` OCR）、全部异步 REST/SSE 端点（含 `/check-filings` 与 `/entities/merge`）、租约续期、孤儿回收、抽离无 `http_request` 账号级 runtime 解析器 + `lifespan` 可开关后台定时巡检协程（`runtime_maintenance_leases` 短事务租约 + 批上限与续约）、雷达 **Phase 2c** + R2-D4 两级合并、R2-D5 Turn 1 + 分源撤销校验（防跨 Topic/OCR 衍生源误杀 `history`）+ `FinDataToolbox.call_qa_verification` `as_of` 放行；
   - 执行 §8.2 受控实验并留档真实延迟、RSS、Token、缓存命中与准确率数据。
5. **`Step V3a-3`（前端 `CallVerificationBar` / `Panel` + `ReaderPanel` / `InboxPage` 升级 + 容器构建部署）**：
   - 新增前端组件与纯静态 CSS（L1 Headline、Tab 1/2/3、Tab 2 零内联 CSP 响应微条、常规上传自动感知与取消轻提示、角落纠偏与非 SEC 别名一键合并 chip、Markdown 简报一键复制、opt-out 忽略），升级 `ReaderPanel` 跨 Topic/跨季 owner-scoped 回退（含标题元数据）与 `第 N 页 / Slide N / 00:14:22` 显示，`InboxPage` 适配 `call_commitment_verify` 通道、合并区块与 `has_confirmed_ledger` 按钮文案；完成 Vitest（含 CSP 零内联与挂载零外呼/条件 `/check-filings` 断言）、`vite build` 与现网容器端到端实测。

### 8.2 受控 LLM 实验方案（遵循 `AGENTS.md` 纪律）

- **固定测试集（16 份跨行业公开/半合成样本）**：
  - 美股科技电话会（`NVDA / META / MSFT`：高 Non-GAAP 调节差 + RPO/CapEx 承诺 + 分析师集群追问 + 复合问题选择性漏答）；
  - **对抗样本 1**：开场白声明“全场 Non-GAAP”但正文省略 Non-GAAP 前缀的样本；
  - **对抗样本 2**：含拆股/重述的非自然财年历史季纪要（如 `NVDA / MSFT`）+ 推进 `as_of` 回放延迟 10-Q 门控自动补全；
  - **对抗样本 3**：35 页“文本页眉 + 栅格化核心表格”且 **`<=5MB`** 的混合型 IR PPT PDF（验证路由不绕过 OCR + 同步上传 `<30s` 不 `524` + Layer 0b 异步补扫偏移 0 误差）；
  - **对抗样本 4**：20–45MB 巨头 10-K/10-Q（如 `MSFT / JPM`，验证 64KB 流式 HTML `<5MB` RAM 与轨道 5 正常产出）；
  - 其余必测样本：商业保密合理拒答 vs 滥用 `"confidential"` 转移话题、CEO vs CFO 跨角色分歧与同场自相矛盾、复杂 `.pptx`（重排页 + 百分比与 billions 图表 + 隐藏页 + CFO Notes）+ 含 `<w:del>` `.docx` + 滚动重复 `.vtt` 字幕 + 卖方前瞻/点评 PDF 增量融合（验证 0 重跑主纪要）、FPI (`TSM`)、金融股 (`JPM` SIC 豁免)、非 SEC 双语别名纪要降级与合并、证据不足模糊表态（`Conscious Incompetence` 专测）及 Prompt Injection 对抗 PDF。
- **单变量对照基线**：同资料、同模型部署配置、同 `as_of` 下对比「通用提示直接喂全文」vs「V3a 四层流水线」。
- **预锁定验收指标（含反退化双维度，实验结果如实留档，不事后调标准）**：
  - 展示引文服务端验真率 **`100%`** 且 `ReaderPanel` Raw-Offset 误差为 **`0`**；
  - Non-GAAP（含开场白隐式 Non-GAAP）错撞 GAAP 误报率 **`0%`**；历史季跨期/拆股/重述错账率 **`0%`**；
  - 预设矛盾与承诺违背的 **Precision `>= 85%`** **AND** **Recall `>= 85%`**（防“全不报→误报 0%”退化）；复合问题漏答检出率显著优于单变量基线；`Conscious Incompetence` 证据不足样本幻觉编造率 **`0%`**；
  - 合理商业保密误亮红灯率 **`<10%`**；同报告期 `/inbox` 重复卡数 **`0`**；
  - `512M / 1.0 vCPU` 压测下峰值 RSS **`<320MB`**（现网 `768M` 容器留足余量）、TTFT（`layer0_ready` / `layer1_ready`）**`<1.5s`**、0 次 OOM / Cloudflare `524` 断连；
  - 如实记录实测 P95 延迟、平均 Token 消耗、缓存命中率与人工核验状态（不以等待人工验收阻塞工程收尾）。
