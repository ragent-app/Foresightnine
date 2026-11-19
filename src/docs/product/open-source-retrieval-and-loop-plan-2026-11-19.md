# 开放来源获取与研究闭环改进规划

日期：2026 年 11 月 19 日。状态：**规划中（尚未实施）**。本文记录基于真实线上运行排查得出的服务端与研究运行时改进方案，作为 [研发计划](../../../plan.md) 的补充执行依据，不代表代码已修改或阶段 B 已通过。

## 1. 背景与实测诊断

在公网环境对仅输入问题的主题 `topic_8e91c0299a7b4b25823bc12ab9c7e53a`（“比较英伟达最近两个财年的总收入和营业利润率”，未上传本地资料，开启 `discover_sources` 与 `auto_fetch_official`）排查可见：

- 迁移前 `operation:b6f0cc51d464a20832bd7827`（2026-11-19 09:28 UTC）因连续 5 次发送前 TLS 握手失败（`provider_transport_error`）而终止。
- 迁移至当前主机后通过 `/continue` 触发的 `operation:d7ff50a51b5265d7746f5ae0`（10:30–10:35 UTC）共成功执行 18 次模型调用（`glm-5.3-flash`，`anthropic_messages`，累计记账 127,392 tokens）、8 次 `discover_sources`、10 次 `acquire_sources`，但最终仍以 `partial` 结束且无任何引用、事实或计算。

暴露出的五个环节问题：

1. **抓取白名单硬编码过窄且 SEC 未启用**：[research_fetch.py](../../apps/api/app/research_fetch.py) 的 `ALLOWED_HOSTS` 仅允许 `www.bea.gov`、`www.federalreserve.gov`、`www.sec.gov`；`fetch_official_pdf` 进一步限制为前两个域名。线上容器未配置 `FORESIGHTNINE_SEC_USER_AGENT`（或 `SEC_USER_AGENT`），导致 `www.sec.gov` 返回 `sec_contact_unconfigured`；英伟达官网、IR、新闻稿、PDF 托管及财经数据站均因不在白名单内被拒（`unsupported_source`）。同时，已实现的 SEC XBRL 结构化连接器 [research_structured_connector.py](../../apps/api/app/research_structured_connector.py) 尚未暴露为 Agent 工具。
2. **模型不知晓来源能力与边界**：`run_research_agent` 初始上下文只传入 `available_historical_price_instruments`，未传入搜索/抓包/SEC 可用性与限制；`acquire_sources` 对“未在搜索结果中发现的 URL”与“策略/格式/反爬拒绝”反馈不够明确，模型误以为普通 HTML 网页可用并尝试拼接 URL，空转 18 轮。
3. **开放网页适配存在技术阻断隐患**：`fetch_official_text` 使用 `_default_transport` 默认响应体上限 `_MAX_RESPONSE_BYTES`（256 KB）且固定按 UTF-8 解码，禁止一切 3xx 重定向，且 HTML 提取使用 `_TextParser`（未过滤隐藏 DOM）；同时 `execute_official_retrieval` 仅靠路径 `.endswith('.pdf')` 区分 PDF，且一律赋予 `kind='official'` 与 `export=True`。
4. **搜索连接器跳转链接损耗严重**：8 次 `discover_sources` 每次均有 5–6 条候选因 `redirect_link` 被丢弃（[research_discovery.py](../../apps/api/app/research_discovery.py)），每次仅剩 3 条线索且 `published_at` 均为 `null`。
5. **部分交付清空解释说明**：模型第 17 轮提交了包含期间推断依据、失败渠道清单与下一步建议的 `report`，但 [research_runtime.py](../../apps/api/app/research_runtime.py) 对 `status != 'completed'` 强制置空 `report=""`，前端只能展示通用兜底提示。另观察到容器无 IPv6 出网路由时每次请求都会先尝试两次 IPv6 `tcp`（`errno: 101`）再回退 IPv4。

---

## 2. 核心决策：移除 3 域名硬编码白名单，分离安全与许可边界

不再以写死的 3 个政府/监管域名作为抓正文的准入门槛，而是将原本耦合在一起的三层职责拆开：

### 2.1 传输安全边界（严格保留并强化）

- **协议与地址校验**：仅允许公网 HTTPS（如从搜索线索收到 `http://` 链接，仅允许在安全校验下升级或跳转至公网 HTTPS）；禁止 URL 内嵌用户名/密码、非标准端口（仅 443/None）；继续通过 [openai_compatible.py](../../apps/api/app/openai_compatible.py) 的 `_default_binary_transport` 与 `_validated_addresses` 在建连前校验全部 DNS 解析 IP 为公网全局地址并固定连接 IP，防止 SSRF 与 DNS Rebinding。
- **FRED 硬拦截**：在请求入口、搜索规范化、重定向每一跳均保持 `is_fred_source` 拦截（[research_source_policy.py](../../apps/api/app/research_source_policy.py)），在条款核清前禁止 FRED 进入实验或 Agent 输入。
- **反隐藏注入 HTML 解析**：开放网页文本提取统一改用 [research_uploads.py](../../apps/api/app/research_uploads.py) 的 `_UploadedHtmlParser`，过滤 `<ix:header>`、`hidden`、`display:none`、`visibility:hidden` 等隐藏节点。
- **运维可配置开关**：代码中移除 `ALLOWED_HOSTS` 三域名写死限制；保留可选的运维配置项（如禁用域名黑名单或可选的部署级允许域名列表），默认允许开放公网 HTTPS 抓取（仍拦截 FRED 与内网地址）。

### 2.2 开放网页抓取适配（去掉白名单后必须同步修改）

| 改造点 | 当前现状 | 目标方案 |
| --- | --- | --- |
| 响应体与文本上限 | `fetch_official_text` 走默认 `_MAX_RESPONSE_BYTES`（256 KB），提取文本上限 200,000 字符 | 原始下载上限提升至 5 MB（`5_000_000`，与 `research_sec.py` 及 `MAX_PDF_BYTES` 对齐）；提取后纯文本上限与 `SourceInput.text` 对齐（最高 2,000,000 字符），靠 `read_sources` 分片阅读 |
| 字符集解码 | `_default_transport` 固定 `.decode("utf-8")` | 改用 `_default_binary_transport`，依据响应头 `Content-Type` `charset` / HTML meta 声明解码并提供安全回退 |
| PDF/HTML 分流 | `execute_official_retrieval` 靠 URL `.endswith('.pdf')` 分流；`fetch_official_pdf` 硬编码仅限 `www.bea.gov` / `www.federalreserve.gov` | 移除 `fetch_official_pdf` 的域名写死；统一发起 GET 请求并按响应 `Content-Type`（`text/html`、`text/plain`、`application/pdf`）分流，PDF 校验 `%PDF-` 魔数与 5 MB 上限 |
| 受控重定向 | 任何 3xx 一律失败 | 支持有界跳转（最多 3–5 跳）；每跳重走 URL 合法性、公网 IP 校验与 `is_fred_source` 拦截（私有/回环跳转目标继续 fail-closed 失败），并记录最终规范 URL |
| 通用网页权限与标识 | 抓回内容一律标为 `kind='official'` 且 `export=True` | 区分官方披露通道（SEC/BEA/Fed 等明确公共领域或授权来源）与通用网页获取（如独立 `retrieval_method` 或明确来源标记）：通用网页赋予 `store=True, display=True, model_input=True`，**默认 `export=False`**（完整导出自动剥离原文，仅保留引用定位、URL 与哈希），严守再分发合规边界 |

### 2.3 SEC 专项与结构化渠道

- **保留 SEC 公平访问机制**：`www.sec.gov` 与 `data.sec.gov` 继续要求合规的 `FORESIGHTNINE_SEC_USER_AGENT`（含真实联系信息）与全站 1 秒限流（`reserve_sec_request`）；未配置时，仅 `www.sec.gov` / SEC 工具不可用，**不再阻断其他开放域名抓取**。
- **开放 SEC 结构化工具**：将已实现的 [research_structured_connector.py](../../apps/api/app/research_structured_connector.py)（`search_sec_identities` + `fetch_sec_concept`）封装并暴露为 Agent 工具（仅在 `sec_available=True` 时开放），使美股公司财务指标可直接读取一手 XBRL 精准 `Decimal` 数据并配合 10-K 原文核验，降低第三方站点 WAF/403 拦截带来的不确定性。

---

## 3. 配套研究闭环与交付改造

### 3.1 Agent 能力感知与精细错误反馈（消除无效空转）

- 在 `run_research_agent` 初始 `user` 载荷中显式提供当前运行环境的能力摘要（`search_available`、`web_fetch_enabled`、`sec_available`、`historical_prices_available` 及格式/大小边界），明确提示“只可获取 `discover_sources` 返回的 URL，禁止自行猜测或拼接未返回 URL”。
- 细分 `acquire_sources` 返回原因，例如区分：`lead_not_discovered`（不在已发现线索中）、`sec_contact_unconfigured`、`http_forbidden_or_challenge`（上游 403/401/防爬验证）、`response_too_large`、`unsupported_content_type`、`domain_blocked_policy`、`timeout_or_unavailable`。模型据此可转向其他来源（如从被 WAF 拦截的 IR 站转向 SEC 或 PDF）或在所有路径不可达时迅速止损。

### 3.2 部分交付保留模型的过程说明（不混入未核实结论）

- 维持“没有经过原文位置核验的内容不得作为正式结论交付”的原则，`turn.report` 在 `partial` 状态下继续保持受控（仅交付有据成果或为空）。
- 在 `SubmitRequest` 与 `ResearchTurn` 中把**正式报告**与**未核实的过程说明 / 补证建议**拆开（扩展 `stopping_note` 或新增独立字段如 `partial_notes` / `next_steps`），前端部分交付卡片在显著标注“未核实线索/说明，非正式结论”的前提下将其展示给用户，解决目前空白只剩一行兜底文案的问题。

### 3.3 搜索连接器与传输层优化

- **搜索跳转链接治理**：核查 `glm_coding_mcp` 原始返回结构是否包含可还原的直链或发布日期字段；如上游长期仅返回不透明跳转链接导致高比例丢弃（`redirect_link`），则评估补充或切换返回直链的搜索连接器，并保留现有的链接合法性检查。
- **IPv6 无路由连接优化**：针对容器无 IPv6 出口导致每次连接先试两次 `errno: 101` 的情况，在 `_validated_addresses` 或传输连接顺序中支持按配置优先 IPv4 或跳过本机无路由地址族（不削弱公网 IP 校验）。

---

## 4. 实施顺序与验证要求

| 阶段 | 范围 | 验收与测试重点 |
| --- | --- | --- |
| **第一阶段（抓取解耦与 SEC 启用）** | 1. 去除 `research_fetch.py`、`research_source_catalog.py`、`research_official_retrieval.py` 的 3 域名与 PDF 2 域名写死限制；<br>2. 响应上限调至 5 MB、按 `Content-Type` 与字符集解码、支持安全有界跳转、改用 `_UploadedHtmlParser`、通用网页默认 `export=False`；<br>3. 部署配置合规的 `FORESIGHTNINE_SEC_USER_AGENT`（单独在部署环境中注入，不写入仓库）。 | 1. 单元测试覆盖：开放 HTTPS 抓取（HTML/纯文本/无 `.pdf` 后缀 PDF）、非 UTF-8 编码、5 MB 边界、安全重定向、跳转至 `127.0.0.1` / 私网 / `FRED` 必须 fail-closed、`export=False` 导出剥离原文；<br>2. 在公网/隔离环境重测无上传资料的真实公司研究（如 `topic_8e91c0299a7b4b25823bc12ab9c7e53a` 同类问题），记录四维状态与实际耗时/Token。 |
| **第二阶段（能力感知、SEC 工具与部分交付展示）** | 1. 将 `research_structured_connector` 接入 Agent 工具；<br>2. 初始上下文注入能力矩阵，细化 `acquire_sources` 失败原因；<br>3. 增加部分交付说明字段及前端展示；<br>4. 排查 `glm_coding_mcp` 跳转链接与容器 IPv6 回退。 | 1. 契约生成与校验、API 与 Web 测试通过；<br>2. 真实对比验证：对比改造前后同一问题的模型调用次数、Token 消耗与实际完成率，同时验证全路径受阻时能快速止损并给出清晰缺口说明。 |
