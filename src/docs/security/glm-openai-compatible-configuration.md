# GLM 与 OpenAI-compatible Provider 配置

## 安全边界

所有模型凭据只由 API 服务进程的 Secret Manager 或未提交的运行时环境变量提供。不得把凭据写入 `.env`、源码、测试、客户端 bundle、Android build、日志、截图或数据库。Web 与 Android 都只调用 ForesightNine API；它们从不直连 GLM，也不接受用户 API key。

没有有效环境密钥时，服务使用 deterministic mock。它不会伪造真实模型结果。

## GLM Coding 预设

将 `FORESIGHTNINE_LLM_PROVIDER=glm_coding`（`zhipu_glm_coding` 也兼容）设给 API 服务，即启用固定的 OpenAI Chat Completions 预设：

- 固定 base URL：`https://open.bigmodel.cn/api/coding/paas/v4`
- 固定 host allowlist：`open.bigmodel.cn:443`
- server-only secret：`FORESIGHTNINE_GLM_CODING_API_KEY`（已由 Linux 部署侧注入 gitignored 的 infra/docker/staging.env，勿写入任何被跟踪文件）
- 固定 model：`glm-5.3`

预设不会读取通用 `FORESIGHTNINE_LLM_BASE_URL`，因此部署环境不能把 GLM 路径重定向到任意地址。该产品切片只允许请求 `glm-5.3`：`FORESIGHTNINE_GLM_CODING_MODEL` 已废弃，任何非空值都会在发起请求前 fail closed；未设置或显式为空时使用代码内固定值。上线前必须用拥有该模型访问权的安全注入环境进行一次小规模 challenge 验收，不能静默降级到其他模型。

### 服务端确定性参数

以下变量只能由 API 服务进程读取；请求体中没有对应的客户端配置入口，也不会从 challenge JSON 复制这些值：

- `FORESIGHTNINE_GLM_CODING_THINKING`：仅接受 `enabled`/`true`，默认 `enabled`，发送为 `thinking.type`；`glm-5.3` 不支持 `disabled`，因此 `disabled`/`false` 会 fail closed。
- `FORESIGHTNINE_GLM_CODING_REASONING_EFFORT`：Coding Plan 映射允许 `low`、`medium`、`high`，默认 `high`。
- `FORESIGHTNINE_GLM_CODING_DO_SAMPLE`：严格的 `true`/`false`，默认 `false`。

服务端对布尔枚举和 reasoning 档位执行 fail closed 校验；任何空值或未知枚举都不会发出供应商请求。GLM 请求明确发送 `thinking`、`reasoning_effort`、`do_sample`、现有 `max_tokens` 和 `response_format: {"type":"json_object"}`。`temperature`、`top_p` 是可选供应商字段，产品不提供用户选择、不读取对应环境变量、不校验也不发送它们；其默认值由供应商处理，不由产品覆盖。

参数和模型依据智谱官方文档：[对话补全 API](https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/%E5%AF%B9%E8%AF%9D%E8%A1%A5%E5%85%A8)（GLM-5.3、参数范围、JSON response format）和[深度思考](https://docs.bigmodel.cn/cn/guide/capabilities/thinking)（`thinking.type`、reasoning 档位及 Coding Plan 映射）。本产品使用的 Coding Plan endpoint 仍固定为 `https://open.bigmodel.cn/api/coding/paas/v4`，其 `/chat/completions` 路径由适配器拼接；官方通用 Chat Completions 文档展示的同一服务 host 和 v4 协议路径用于参数语义参考。

`reasoning_effort` 的语义取决于 provider profile：官方通用 GLM-5.3 API 文档支持 `minimal`、`low`、`medium`、`high`、`xhigh`、`max`；这是通用 API 的允许值集合，不等于 Coding Plan endpoint 的原样透传集合。本适配器只承诺并允许 Coding Plan 映射后的 `low`、`medium`、`high` 三个 wire 值，不把通用 API 的六档集合或 OpenAI 风格的 `none` 发给 GLM；产品默认 `high`。该字段是供应商的思考强度档位，不是本地 `max_tokens`。通用 `openai_compatible` profile 不读取这些 GLM 变量，不注入 GLM 专用生成控制，且只发送 `model`、`messages`、`max_tokens` 和 `response_format`；包括 `temperature`、`top_p` 在内的可选采样字段均由供应商默认值处理。官方参数和 `reasoning_effort` 语义链接见本节上一段的智谱[对话补全 API](https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/%E5%AF%B9%E8%AF%9D%E8%A1%A5%E5%85%A8)与[深度思考](https://docs.bigmodel.cn/cn/guide/capabilities/thinking)文档。

启用 thinking 时，适配器只解析 `message.content` 中的严格 JSON；供应商返回的 `reasoning_content` 不会写入 `ChallengeDraft`、citation 或事实字段，也不会向客户端输出。thinking 内容不是证据，不能成为事实。

## 用户自带 / 订阅的 OpenAI-compatible 端点

通用路径使用 `FORESIGHTNINE_LLM_PROVIDER=openai_compatible`，并且只允许部署者配置：

- `FORESIGHTNINE_LLM_BASE_URL`（HTTPS URL）
- `FORESIGHTNINE_LLM_MODEL`
- `FORESIGHTNINE_LLM_API_KEY`
- `FORESIGHTNINE_LLM_ALLOWED_HOSTS`（逗号分隔的精确 host[:port] allowlist）

这不是把个人 key 传到浏览器的 BYOK 功能。若未来提供用户订阅或用户自带供应商，必须先增加经审计的服务端密钥托管与租户隔离设计；在此之前，客户端配置界面不得收集或保存 key。

## 实际 Provider 验收

在独立、可撤销的服务端环境中完成，且只发起一条无私人数据的结构化 challenge。live smoke 只允许在服务端显式注入 `FORESIGHTNINE_GLM_CODING_API_KEY` 后执行；不得把 key 写进仓库、客户端或测试：

1. 注入上述运行时变量；不写入任何仓库文件。
2. 启动 API，确认 `/v1/challenges` 只接收已通过 schema 的 JSON。
3. 验证返回含反例、逻辑跳跃、已定价审查、缺失证据与 10 项 rubric。
4. 记录 HTTP 状态、模型名、请求 ID（如有）和脱敏后的结构化结果；不得记录 Authorization header 或响应中的用户私密材料。
5. 验证 429、5xx、timeout、invalid JSON、citation mismatch 均 fail closed。

该仓库的单元测试覆盖预设 URL/host 固定、服务端参数默认值、非法配置 fail closed、固定模型、缺失密钥不发请求、密钥不进入 prompt/`repr`、5xx 重试失败、citation mismatch 和 `reasoning_content` 不外泄；它们不是供应商线上成功调用的替代证据。
