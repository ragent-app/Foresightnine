# 354 新研究输出契约双任务真实诊断预登记

用户授权独立批次最多两次模型调用，不搜索、获取、重试、购买或修改历史。第一题沿用 352 原问题和 BEA 本地快照（sha256 f36fc389b6b0420ccd8416938f1028fd8c644b1c14c2e0624e5ba7ebb15aa4ef）；第二题固定问题："Using the supplied Federal Reserve statement, identify the policy decision and the strongest explicitly stated uncertainty or qualification; cite the relevant passages and explain whether the qualification challenges the decision or instead limits what can be concluded." 使用 2020 年 6 月 Federal Reserve 声明本地文本（sha256 fec9898ca7650e7ed9a8271704c57ea4c7157d34174546273a1169c150f415ed）。二者均为既有许可的一手政府资料，不调用 FRED。

每题执行一次隔离生产 execute_generation、provider、ResearchExecutionContext 和 RecoveryJournal，不 mock 模型或校验。固定 glm-5.3-flash、输入 6000、输出 4000、总时限 120 秒、每地址连接 15 秒、响应无单独时限、thinking enabled、low、do_sample false；每题最多一次、10,000 tokens 保守预留、研究期限 360 秒。固定 JSON object 能力模式，strict/tool 在 6000 下不适用，不自动降级或增加限额。发送前 observer 显式接收并透传新contract，核查版本、提示示例、实际 wire 模式、输入预算和非空资料范围，并保存哈希。

独占批次标记在任何模型调用前创建；每题独立标记、调用编号及 journal。第一题如传输结果或结算未知、拒绝/截断、结构根本失败或无有效事实及主张，停止第二题；若结构可解析且有有效事实或主张，即使结果 partial 仍按原顺序运行第二题，不改变阈值或重复。每题保留提供方报告用量、保守本地结算、费用未知、阶段诊断、分析与选中资料；不将 HTTP 200 或结构成功当作事实正确。检验引用及期间、单位、计算和反方，并如实保留缺口；非独立质量基准。
