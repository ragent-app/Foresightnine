# 338 隔离环境预检

2026-11-14：新建仅回环可访问的临时 PostgreSQL 库，依次应用 `001_research_state.sql` 与 `002_operation_state.sql`。使用本地 mock provider 的隔离 API，临时账户注册返回 201；空主题运行列表返回 200 且为空，不存在 operation 的 settlement 返回 404；`operation_state`、`operation_idempotency`、`operation_events`、`operation_session_associations` 均为零行。没有提交 operation，也没有发生模型调用。API 账户、口令和数据库连接不写入此记录。

五题请求对象的离线 Pydantic 校验见 [请求契约记录](request-contract-preflight.json)。此预检只覆盖模拟 provider 和 API／数据库初始化，真实 provider 参数、最终代码散列及启动时再次检查仍需在代码冻结后执行。模型调用预算和未知状态停批规则见 [预登记](preregistration.json)。
