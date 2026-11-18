# 346 非模型传输诊断

本次只读取固定 GLM Coding 地址及传输实现，未读取或发送凭据，未调用模型。一次本地标准解析与实际 `_validated_addresses` 都返回 `47.251.66.145`、`47.88.85.161`；本次未触发 fake DNS 与 DoH 分支。代理环境变量仅记录是否存在，均不存在。`diagnose.py` 采用实际 `_PinnedHTTPSConnection`，结果见 `result.json`：两个地址 TCP 分别约 0、1 ms，TCP 加 TLS 分别 2640、2922 ms，均建立 TLS 1.3；在此阶段没有发送 HTTP。

随后一次普通主机名 TLS 对照使用 `47.253.242.212`，3040 ms 成功；不同于先前地址，反映独立解析结果变化，不能作为同一 IP 对照。另一次使用生产 `_default_binary_transport` 对 `https://open.bigmodel.cn/` 发无认证 HEAD，返回 HTTP 200，总计 5492 ms，其中实际固定地址 `47.251.66.145` 的 TCP 和 TLS 为 4570 ms。仅访问根路径，没有调用 chat completions；记录见 `http_result.json`。

本次未复现 344 的失败。固定连接的握手耗时曾接近代码每地址 5 秒上限，344 的 10.011 秒与两个地址各超时约 5 秒的模式相容，但历史记录缺少逐地址、逐阶段日志，也未证明当时地址、网络状况或发送状态；不能将相容性当作该次根因，344 的 provider response 和结算仍为 unknown。当前证据也不能判断延迟来自本机、网络中间环节还是供应方。

代码定位：`src/apps/api/app/openai_compatible.py` 的 `_PinnedHTTPSConnection.connect` 将 TCP 和 TLS 组合在同一限时连接中；`_execute_binary_transport` 每地址上限 5 秒且仅在连接成功后发送请求。建议在不记录凭据、请求体或响应内容的前提下增加 DNS、每地址 TCP/TLS、HTTP 发送开始与完成的结构化诊断；异常记录类别和耗时，并保留发送后状态未知的语义。超时调整需根据受控测量决定，不能仅凭本轮推断。`_validated_addresses` 的 fake DNS 分支使用 DoH 检查公网性后返回原始 DNS 地址，本次未走该分支，不对其实际网络行为作结论。

说明：首次启动因系统没有 `python` 命令失败，未运行脚本及发起网络；随后使用项目虚拟环境执行。诊断脚本在成功结果后仅修正了异常分支，使后续运行保留原始异常；未重新执行，已有成功路径不受影响。

补充一次更贴近业务路径的无认证 `HEAD /api/coding/paas/v4/chat/completions`，仍使用生产 `_default_binary_transport`，未发送 POST、凭据或模型调用。`api_head_result.json` 记录：固定 `47.251.66.145`，实际连接上限 5 秒，TCP 0 ms，TCP 加 TLS 1088 ms，发送 HEAD 0 ms，等待响应头 1571 ms，读取 0 ms，总计 2666 ms；HTTP 401 表明该无凭据请求得到 HTTP 响应，不能证明认证或模型生成可用。346 诊断此后未再进行网络探测。此前普通主机名对照解析到了不同地址，不能把它视为相同目的 IP 的对照。

2026-11-16 后续 [347 独立最小真实模型探针](../347-instrumented-provider-probe/evaluation.md)一次返回 HTTP 200、提供方报告 40 tokens，总耗时 4.404 秒；该结果不能确认历史 344 的具体根因或结算，也不构成研究质量验收。344 缺少分阶段日志是定位缺口；本次 4.570 秒 TCP 加 TLS 与每地址 5 秒上限说明存在实测超时风险，并非对 344 的根因证明。
