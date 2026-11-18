# 344 独立提供方连通性探针（预登记）

与 343 完全独立；343 的未知调用及账本不重放、不修改。本探针只检查一次结构化输出与用量是否可对账，不评价研究质量。

- 固定模型：GLM Coding `glm-5.3-flash`；thinking enabled，reasoning effort low，do_sample false，JSON 输出；单次 120 秒，不搜索、不获取，不重试。
- 唯一输入与系统提示固定在 probe.py；不含用户资料。输入上限 6000、输出上限 4000，本次最多预留 10000 tokens，最多一次模型调用；这些仅为实验参数，不改变产品预算。费用未知，不新增购买。
- 执行前独占创建 pending 状态并持久化；仅有 provider 正常返回且明确报告 usage 才记录 reported_usage。没有 usage、传输异常、进程中断或服务端响应不明均保留结算未知，禁止重投。JSON 内容仅检查 `ok: true`，不保存原始响应或凭据。此文件账本是独立实验记录，不等同供应商计费账单。
- 成功要求响应返回、JSON 满足约定且有报告用量；其他情况按状态如实记录，不能推断实际费用。实验结束后不自行开启其他批次。
da0bac330eced21885ce531ab0d7242e5dcb2ff0086d4a4bd42b5a4a6b1a9408  src/docs/product/phase-a/344-provider-connectivity-probe/probe.py
