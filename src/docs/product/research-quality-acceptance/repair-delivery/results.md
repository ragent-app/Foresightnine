# 交付验收记录

原 14 轨的散列、终态、事实和计算 ID 见 `frozen-baseline.json`；D4-A-2 和 D4-B-2 未启动。`replay.py` 读取原轨 final_state_projection，按轨迹外层来源散列与冻结材料核对后执行评估、组装和检查点合并；历史 read_ranges 不含版本字段，保留原区间并从轨迹外层来源元数据重建版本绑定。没有调用模型或搜索。

`api_fixture.py` 使用隔离内存 repository，经 FastAPI 实际 topic 和 export 路由读取，账户认证由 fixture 替换，不是认证或生产持久化验收。turn 手工重建保留原 report、partial_notes、原因、事实/计算/主张 ID、调用与账本 usage，D4-B-1 未知用量保留 partial_unknown 和预留。其他未保存运行字段、恢复状态和真实持久化不在覆盖内。`api-results.json` 逐轨保存路由响应；上轮 14/14 两端 200，分项一致。D1-A1 50% 保留且原单位差额缺失、状态 partial；D3 与 D4-B1 未补算。最终 D1-A2 同时显示 50% 和 +1 美元/股。4/4 已存有据项及 5/5 已读明确否定项均在 API 与导出中可见，引用均存在；未观察到缺证项被填入数值。

`permission-results.json` 为隔离 fixture 下的真实路由反例：display 禁止和来源哈希变化时 topic 分项撤回，export 均 403；export 禁止时 topic 仍可展示、export 403；另一个账户读 topic 为 404。此记录已按最终代码重跑。指标、期间、主体、输入篡改、过期时点与跨来源反例不由此脚本覆盖，需并入实现者测试结果。

`browser-preliminary.json` 是早期候选 14 轨的实际浏览器 DOM：本地 Vite 代理到真实 FastAPI 路由，没有拦截请求；每轨可见分项文字与当时 API 一致。但该轮 fixture 缺 list 接口导致 500、另有恢复状态 404，且随后生产代码修改，不能作为最终页面通过证据。最终以静态构建、同源真实 API 和独立 Chromium 重跑，`browser-results.json` 保存 14 轨 DOM，所有非空分项 answer/reason 与 API 一致。D2-A2 点击查看原文后高亮加载，`browser-citation.json` 保留定位、材料哈希与原文。14 个 console 错误为隔离 fixture 中缺少 op_1 对应恢复状态的 404；未验证恢复或浏览器导出下载。没有真实模型新实验，离线结果不支持总体研究质量或正式提交率结论。

`REPAIR_RAW_RESULTS_DIR=/absolute/path` 可使 `api_fixture.py` 读取该目录各轨文件并直接使用原始 question_parts，不重新生成；该模式已用原 14 轨成功试跑；尚未用新实验数据验证。
