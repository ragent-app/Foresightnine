# 359：原生工具研究运行验证预登记

本次只准备一次独立实验，不因预登记而执行。固定问题：仅使用授权上传的 2022 年 3 月及 5 月联储声明，比较联邦基金目标区间，计算每个端点变化（百分点），说明声明的限定并引用依据。参考区间分别为 0.25–0.50% 和 0.75–1.00%，两个端点各上升 0.50 个百分点。引用位置、语义支持、计算与任务完成分别核验；只有实际模型原生工具调用、反馈和可核查成果才可作为闭环证据，部分结果保留为 partial。

输入来自 337 的授权快照，原文件不改：

- `fed-2022-march.txt` SHA-256 `02b0db2d39a15f8dc2759e219e8914f273506f4f5e72d3ded3b8b40fa8784466`，https://www.federalreserve.gov/newsevents/pressreleases/monetary20220316a.htm
- `fed-2022-may.txt` SHA-256 `e1005a8ecab1b13c8d47271be03b1350c5c84adeb0f0aa8e0bd7d76e6d215c45`，https://www.federalreserve.gov/newsevents/pressreleases/monetary20220504a.htm

使用隔离进程与内存存储，禁止搜索、发现、自动获取及 FRED，只授权上传资料。无模型调用、token、总时长或费用上限；账户权限仍生效；单次请求技术超时 120 秒。参数、数据和范围固定，本次变更为原生工具运行路径；不是旧批次的质量对照，任务也不相同。

357 的实际记录是 `glm-5.3-flash`，thinking enabled、reasoning_effort low、do_sample false，原 GLM Coding 路径 `https://open.bigmodel.cn/api/coding/paas/v4/chat/completions`。本实验在隔离进程将现有服务端 `FORESIGHTNINE_GLM_CODING_API_KEY` 映射为通用协议凭据，不保存或打印凭据；这些参数仅是本实验记录，不是产品默认。`native_tools=true` 仅为本实验待验证声明，并不表明该 endpoint/model 真实支持；拒绝、协议不支持或失败按实际保存，禁止更换模型或自动重试。若旧凭据或其映射无法确认，不执行。

冻结脚本、预登记、API 代码和两份资料哈希后，独占持久 marker 必须先于研究提交；禁止重放。保留提交、模型工具请求及关联反馈、检查点、结算、用量、耗时、费用状态及失败；未知费用或用量标记 unknown。提交或外呼状态未知时停止，不自动继续、重放或销毁隔离实例；保留只读核对入口和受限控制文件。敏感凭据不进入结果；授权资料和模型输出仅存于受保护实验结果。尚未有任何真实结果。

依据[服务商思考模式说明](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode)，工具多轮需要原样续传 `reasoning_content`。因此仅本实验明确允许该字段作为不透明续接数据，保存在受限私有检查点并用于后续请求；不将其纳入公开导出或研究结论。其他生成参数不变。
