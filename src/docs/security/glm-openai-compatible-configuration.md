# GLM 与 OpenAI-compatible Provider 配置

## 安全边界

所有模型凭据只由 API 服务进程的 Secret Manager 或未提交的运行时环境变量提供。不得把凭据写入 `.env`、源码、测试、客户端 bundle、Android build、日志、截图或数据库。Web 与 Android 都只调用 ForesightNine API；它们从不直连 GLM，也不接受用户 API key。

没有有效环境密钥时，服务使用 deterministic mock。它不会伪造真实模型结果。

## GLM Coding 预设

将 `FORESIGHTNINE_LLM_PROVIDER=glm_coding`（`zhipu_glm_coding` 也兼容）设给 API 服务，即启用固定的 OpenAI Chat Completions 预设：

- 固定 base URL：`https://open.bigmodel.cn/api/coding/paas/v4`
- 固定 host allowlist：`open.bigmodel.cn:443`
- server-only secret：`FORESIGHTNINE_GLM_CODING_API_KEY`
- model：`FORESIGHTNINE_GLM_CODING_MODEL`，未设置时请求 `glm-5.3`

预设不会读取通用 `FORESIGHTNINE_LLM_BASE_URL`，因此部署环境不能把 GLM 路径重定向到任意地址。`glm-5.3` 是当前产品配置目标；上线前必须用拥有该模型访问权的安全注入环境进行一次小规模 challenge 验收。若供应商租户尚未开放该模型，设置 `FORESIGHTNINE_GLM_CODING_MODEL` 为该租户已批准的模型后再验证，不能在客户端静默降级。

## 用户自带 / 订阅的 OpenAI-compatible 端点

通用路径使用 `FORESIGHTNINE_LLM_PROVIDER=openai_compatible`，并且只允许部署者配置：

- `FORESIGHTNINE_LLM_BASE_URL`（HTTPS URL）
- `FORESIGHTNINE_LLM_MODEL`
- `FORESIGHTNINE_LLM_API_KEY`
- `FORESIGHTNINE_LLM_ALLOWED_HOSTS`（逗号分隔的精确 host[:port] allowlist）

这不是把个人 key 传到浏览器的 BYOK 功能。若未来提供用户订阅或用户自带供应商，必须先增加经审计的服务端密钥托管与租户隔离设计；在此之前，客户端配置界面不得收集或保存 key。

## 实际 Provider 验收

在独立、可撤销的服务端环境中完成，且只发起一条无私人数据的结构化 challenge：

1. 注入上述运行时变量；不写入任何仓库文件。
2. 启动 API，确认 `/v1/challenges` 只接收已通过 schema 的 JSON。
3. 验证返回含反例、逻辑跳跃、已定价审查、缺失证据与 10 项 rubric。
4. 记录 HTTP 状态、模型名、请求 ID（如有）和脱敏后的结构化结果；不得记录 Authorization header 或响应中的用户私密材料。
5. 验证 429、5xx、timeout、invalid JSON、citation mismatch 均 fail closed。

该仓库的单元测试覆盖预设 URL/host 固定、模型覆盖、缺失密钥不发请求和密钥不进入 prompt/`repr`；它们不是供应商线上成功调用的替代证据。
