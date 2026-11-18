# 348 研究模型传输诊断与超时实现

本次适用 `generate_structured` 研究模型调用；旧 `generate_challenge` 路径仍使用原传输行为，不宣称所有 LLM 路径均已切换。每次调用以调用编号将发送状态、DNS、TCP、TLS、请求发送、响应头、响应正文及超时或取消阶段的受控诊断写入私有运行日志。正在阻塞的阶段在截止或取消时保留截至当时的耗时；状态标记不能证明供应商实际结算。既有解析和契约验证诊断继续保留，公开运行输入和成果导出不包含私有字段。仅接受固定阶段、结果、耗时、数值错误码、HTTP 状态和发送状态，不保存地址、请求头、凭据、正文或异常消息。日志写入失败不会改变调用结果和保守结算；进程中断或存储失败可能造成诊断缺失。

单次请求总时限仍由 `FORESIGHTNINE_LLM_TIMEOUT_SECONDS` 控制，默认 20 秒，范围大于 0 且不超过 180 秒。研究模型默认传输新增 `FORESIGHTNINE_LLM_CONNECT_TIMEOUT_SECONDS`，默认每地址 15 秒，TCP 和 TLS 共用，受剩余总时限约束；`FORESIGHTNINE_LLM_RESPONSE_TIMEOUT_SECONDS` 默认不设置，设置时限制发送完成后的响应头和正文总时间，同样受总时限约束；两个参数设置值均须大于 0 且不超过 180 秒。地址回退仅在发送前发生，不重发 POST。自定义注入传输保留原接口，其内部阶段标为不可用。DNS 校验、固定地址和原主机名 TLS 校验保留。

离线验证：provider、调用记账与私有日志初组 73 项通过、4 项因未启用测试所需的本地 mock 登录而在准备阶段失败；仅对该 4 项启用 mock 后重跑，4 项通过。随后对中断冻结、活跃阶段留位、DNS 截止和并发容量做 4 项定向复测，4 项通过；这些组存在重叠，不合计为全量测试。未进行真实网络或模型重放；[344](344-provider-connectivity-probe/evaluation.md) 原有未知响应及结算状态不变。此次验证不证明真实研究质量或阶段 B 已验收。

测试在 `src/apps/api` 目录执行：

- 初组：`.venv/bin/pytest -q tests/test_openai_compatible_provider.py tests/test_research_model_receipts.py tests/test_research_recovery_acceptance.py::test_private_validation_diagnostics_are_bounded_and_not_exported`。
- 环境纠正后：`FORESIGHTNINE_LOCAL_AUTH_MODE=mock .venv/bin/pytest -q tests/test_research_recovery_acceptance.py::test_private_validation_diagnostics_are_bounded_and_not_exported`。
- 后续定向：`.venv/bin/pytest -q tests/test_openai_compatible_provider.py::test_transport_interruption_keeps_active_stage_with_full_history tests/test_openai_compatible_provider.py::test_dns_deadline_captures_blocked_stage_and_freezes tests/test_openai_compatible_provider.py::test_transport_deadline_includes_slow_dns_and_does_not_submit_after_timeout tests/test_openai_compatible_provider.py::test_transport_capacity_remains_bounded_when_dns_cannot_be_cancelled`。
