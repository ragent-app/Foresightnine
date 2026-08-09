# ForesightNine 执行 Goal 提示词

@sol_advisor_sol_reviewer @sol_advisor_terra_implementer

你们要在 D:\Foresightnine 中完成一个公开可访问的观点到世界线到持仓配置产品，提供 Web 与 Android 两个版本。

开始前必须完整阅读：

- D:\Foresightnine\plan.md
- D:\Foresightnine\agent.md

plan.md 是已批准的执行基线。只有经过验证后客观无法执行时才允许做最小修改；任何修改必须先记录证据并由 Sol reviewer 复核。不得因为实现偏好、重构或局部困难改变计划。

所有新代码、测试、设计资产、配置和实施文档只能写入：

- D:\Foresightnine\src

以下目录只读，不得修改：

- D:\FM acceleration
- D:\FM acceleration-deepseek
- D:\WL-data-final
- D:\ACFM_WL_paperwriting
- 相关服务器源目录

产品必须完成以下闭环：

1. 接收用户观点和资金金额。
2. 使用 OpenCode DeepSeek Flash 做结构化 challenge。
3. 自动指出反例、逻辑跳跃、已定价部分和缺失证据。
4. 优先使用 OpenCode 自带搜索；没有搜索能力时接收用户客户端 SearchEvidenceBundle。
5. 使用 rubric 持续追问，直到达标或用户带保留意见确认。
6. 生成主世界线、弱路径和强路径。
7. 分别估计 P(premise)、P(worldline | premise) 和联合概率，并给区间、证据、置信度和校准状态。
8. 生成资产影响、时间锚点和事件揭示时间。
9. 在用户端按现有实验 FM 方法计算因子、组合权重、成本死区、金额和份额。
10. 同时显示 FM Raw 和施加公开产品安全约束后的 Public Default。
11. 输出组合解释、风险、压力测试、方法版本、数据时点和风险披露。
12. 完成 Web 和 Android。

技术基线：

- Web：React + Vite + TypeScript
- Android：React Native + Expo
- API：FastAPI + uv + PostgreSQL
- 客户端共享 contracts、domain、fm-engine 和 design token
- Web 使用 Web Worker 执行 FM
- Android 使用共享 TypeScript FM engine，小规模测试优先

FM 必须逐公式复现以下只读参考：

- D:\FM acceleration-deepseek\bundle\agent-framework\scheduler\fm_walk_forward.py
- D:\FM acceleration-deepseek\bundle\agent-framework\FactorMiner\factorminer\evaluation\combination.py
- D:\FM acceleration-deepseek\bundle\agent-framework\FactorMiner\factorminer\evaluation\portfolio.py
- D:\FM acceleration-deepseek\bundle\agent-framework\FactorMiner\factorminer\evaluation\metrics.py
- D:\FM acceleration-deepseek\bundle\agent-framework\FactorMiner\factorminer\evaluation\backtest.py
- D:\FM acceleration-deepseek\bundle\agent-framework\ASSETS.yaml

先生成 Python golden fixtures，再实现 TypeScript，并验证因子选择、rank/ties、NaN、IC、ICIR、合成信号、权重、等权 fallback、3 bps 费用和 cost dead-band。不得用服务端计算替代客户端 FM，不得使用 eval，不得读取未来数据。

视觉采用 Claude.ai 启发的安静、克制、对话优先风格，但不得复制 Claude 品牌。必须使用 Build Web Apps 工作流：

1. 先生成完整 Web 与 Android 各核心状态设计概念。
2. 展示给用户批准。
3. 批准前可以推进合同、mock provider、后端和 FM golden tests，但不得把未批准的视觉实现当作最终 UI。
4. 批准后严格按概念建立 design system 并实现。
5. 用 Browser/IAB 验证 Web，用 Android emulator/真机验证 Android。
6. 最终用 view_image 对照概念与实现截图，修复到可通过专业设计验收。

安全要求：

- 历史对话中暴露的 DeepSeek key 已失效，绝不使用或复制。
- 无新 key 时使用 mock provider 推进。
- 新 key 只能由服务端 secret/environment 注入。
- 运行 secret scan、依赖审计、限流、输入验证、SSRF 防护和日志脱敏。
- 不接券商、不下单、不托管资金。
- 市场数据许可和目标地区法律审查是公开生产发布 gate。

角色分工：

- Sol：拆解当前 phase、规划、审查、验收，不直接放宽完成标准。
- Luna/Terra：执行实现、测试、设计修复和文档，遇到普通工程问题自主解决。
- 每个 phase 必须经过 Sol pass 后进入下一阶段。

执行要求：

- 按 plan.md Phase 0 到 Phase 9 顺序推进。
- 持续维护 src/docs/progress.md。
- 任何失败都要给出证据并修复，不以“已搭框架”冒充完成。
- 只有 plan.md 第 19 节全部满足时才能把 goal 标为 complete。
- 如需要用户批准设计或提供新的安全密钥，可以暂停该依赖项，同时继续推进不依赖它的工作。

最终交付必须包含：

- Web 可运行版本
- Android 测试包
- API 服务
- 客户端 FM engine 与 golden-test 报告
- 完整设计概念和保真 QA
- Browser 与 Android 核心流程证据
- 安全、数据许可、合规和已知限制说明
- 构建、测试和部署命令

现在开始执行，不要重写 plan.md，不要修改只读源目录。
