# 350 真实研究分析诊断预登记

经授权，在 349 成功后独立执行一次分析阶段，不重放或修改 343/344。使用 343 MACRO_CALC 原问题和原 BEA 文本快照（SHA-256 f36fc389b6b0420ccd8416938f1028fd8c644b1c14c2e0624e5ba7ebb15aa4ef），资料权限为已核验的 BEA 政府原文。直接调用当前生产 execute_generation、研究上下文及私有 RecoveryJournal，隔离文件保存实验 journal 与本地结算；不启动或修改生产服务、用户数据或账本。最多一次模型调用，10,000 tokens 保守预留，不搜索、获取、规划、补证、复查或重试。

固定 GLM Coding glm-5.3-flash，输入 6000、输出 4000、总请求 120 秒、连接 15 秒、响应限时未单独设置、thinking enabled、effort low、do_sample false；研究截止 360 秒。只包装调用参数以记录提示及系统提示哈希和字节数，传参完全透传；模型响应使用生产校验、确定性计算及证据处理，保存可检查结果。provider 可能实际收费，使用量以回报为准，未报告为未知；中断或失败不再发起。

343 首题分析阶段 provider_transport_error，约 16.422 秒，原因及是否送达未知；原提示 5255 字节、SHA-256 81ff606ef2d9f2faaa1bbd79a696abedc3ffdbb960b46e9f7edb2ca4353842d0，系统提示 SHA-256 a5eb6e45ac3565451f73e4e2d920b276b99828bd5e895ac27cede1945939f9f7。当前 research_analysis.py 与 343 冻结副本哈希相同（6027d7c2316c2e2d9d0a383f619fe376ebb883383d768146f89bd485472bd4ad）；来源身份和时间等运行元数据、调用上下文及传输实现可能不同，不能宣称字节级重放或单因素比较。此次只诊断研究输入路径，不构成阶段 B 全面验收。
