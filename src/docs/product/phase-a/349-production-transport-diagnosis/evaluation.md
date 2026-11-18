# 349 正式传输诊断结果

按预登记执行一次独立最小模型调用，没有搜索、获取或重试。`ResearchExecutionContext.invoke_model` 调用真实提供方，正式传输回调经隔离的 `RecoveryJournal` 写入 `private-journal.json`；没有修改生产数据。HTTP 200，JSON 格式验证通过，提供方报告 40 tokens，费用未知；10000 tokens 为保守预留。`attempt.json` 中 local receipt 的 `settled` 仅表示隔离实验本地记账回调成功，不是供应商账单或实际费用确认；journal 的 pending 是独立实验标记，不能用来推断任何历史调用。

总耗时 9521 ms，正式私有诊断依次为 DNS 2 ms、TCP 0 ms、TLS 5878 ms、发送 0 ms、响应头 3235 ms、正文读取 385 ms，发送状态为 `response_received`。各阶段是各自采样，不必与总耗时精确相加。此次固定连接时限为 15 秒，TLS 阶段已超过旧代码每地址 5 秒连接上限；这直接证明当前网络条件下存在旧上限不足的情况，但不能唯一确定 344 当时的故障阶段或结算。344 的响应与结算继续 unknown。本次是最小连通性诊断，不是研究质量验收。

此次执行完成后停止外呼；后续定位需要新的明确指挥。

源码及探针在调用前记入 `sha256.txt`，调用后复核一致；`fixed-parameters.json` 是调用后从预登记与探针汇总的明文参数和提示快照，其哈希随后追加，不应误称其独立哈希在调用前冻结。隔离日志 `private-journal.json` 保留了正式诊断记录。
