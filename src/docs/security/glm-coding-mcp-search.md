# GLM Coding Plan MCP 搜索配置

## 目的与边界

`GlmCodingMcpSearchEvidenceProvider` 是 ForesightNine 内置的、服务端专用的检索候选源。它只连接 GLM Coding Plan 的固定 HTTPS MCP 地址：

`https://open.bigmodel.cn/api/mcp/web_search_prime/mcp`

API 进程从未提交的 `FORESIGHTNINE_GLM_CODING_API_KEY` 环境变量读取访问令牌。Web、Android、日志、浏览器 source map、测试夹具和归档文件都不得接触该值。模型 Chat Completions 的 GLM 配置与此搜索配置共用同一服务端密钥，但两个网络路径和调用预算必须分别记录。

启用前设置：

```text
FORESIGHTNINE_SEARCH_PROVIDER=glm_coding_mcp
FORESIGHTNINE_GLM_CODING_API_KEY=<server-secret-manager-value>
```

未显式选择或缺少密钥时，系统仅接受客户端 `SearchEvidenceBundle`；不会尝试任意网络地址，也不会伪造搜索结果。

## 协议防护

- 固定主机、HTTPS、禁用重定向，并复用公开 DNS 解析检查。
- 每次检索使用 MCP `initialize`、`notifications/initialized`、`tools/list`、`tools/call` 顺序；仅调用供应商声明的 `webSearchPrime`，并在返回 session ID 后为每次后续请求带上该 ID。
- 查询最长 70 个字符、结果最大 10 条、响应最大 256 KiB；429/5xx/网络超时最多重试一次。
- 不支持的工具 schema、JSON-RPC 错误、非公开/私有 URL、无有效结果或无密钥均 fail closed。
- 只解析 MCP `structuredContent` 或受控 JSON 文本中的标题、URL、摘要、来源和发布日期；不取网页正文，不跟随搜索结果 URL，也不把结果文本放入 prompt。

## 证据工作流

搜索返回的是 `SearchCandidate`，不是 `EvidenceItem` 或公开事实。用户必须逐条确认、补充检索时间和说明，并经现有 `SearchEvidenceBundle` 哈希/URL 验证后才能进入 Challenge。未提供发布日期的候选显示为“可查看但不可独立复核”；即使含日期，也只是可独立复核的 C 级候选，不能自动升级为已验证事实。

Android 继续优先支持 Share Intent 导入 URL/摘要；它不直连 MCP，因此不会将服务端密钥编入 Android 包。项目内 MCP 搜索可由 API 在用户明确请求时使用，客户端离线或搜索不可用时仍保留 bundle fallback。

## 线上验收

必须在部署者控制的 Secret Manager 环境中执行一次低风险、非个人化查询并保留脱敏证据：HTTP 状态、模型/工具名、请求 ID（若有）、候选数量和 URL 域名。不得记录 Authorization 值、完整用户查询、原始 MCP body 或任何私密证据。单元测试只证明协议与 fail-closed 行为，不构成供应商线上调用成功的证明。
