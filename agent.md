# ForesightNine Agent 执行约束

## 1. 必读顺序

任何执行者开始工作前必须完整阅读：

1. D:\Foresightnine\plan.md
2. D:\Foresightnine\agent.md
3. 与当前阶段直接相关的源代码和测试

不得只根据摘要实施。

## 2. 计划是执行基线

- plan.md 是已批准的产品与工程基线。
- 只有原计划客观无法执行时才允许修改。
- “更喜欢另一种框架”“重构更优雅”“实现较慢”“依赖需要学习”不构成无法执行。
- 修改前必须完成至少三种安全尝试，并在 src/docs/plan-deviations.md 记录证据。
- 只允许最小解除阻塞修改。
- 修改必须经过 Sol reviewer 复核。
- 用户最新明确指令优先级最高。

## 3. 目录边界

- 所有新实现、生成资产、测试、配置和文档都放在 D:\Foresightnine\src。
- plan.md、agent.md 和 GOAL_PROMPT.md 是根目录允许存在的治理文件。
- D:\FM acceleration、D:\FM acceleration-deepseek、D:\WL-data-final、D:\ACFM_WL_paperwriting 和服务器目录一律只读。
- 可以读取、复制和生成 golden fixtures，不得修改原始 FM、AC、9WL 或论文文件。
- 不进行破坏性 Git 或文件操作。

## 4. 角色

### Sol reviewer

- 将 plan.md 转化为当前阶段的最小工作清单。
- 审查架构、概率、FM 一致性、安全、数据泄漏、设计保真和验收证据。
- 不以评论代替可执行验收。
- 每个 phase 结束后给出 pass、fail 或 blocker。

### Luna/Terra implementer

- 按 plan.md 顺序实施。
- 在 src 内完成代码、测试和文档。
- 先复现参考行为，再优化。
- 对可恢复问题自主继续，不把普通工程选择抛回用户。
- 不得绕过测试或用静态假 UI 冒充完成。

## 5. Build Web Apps 工作流

- 可视实现前先生成完整 Web 和 Android 概念，不只做 hero。
- 使用 Image Gen 制作所需状态。
- 用户批准后，概念成为生产规格。
- 建立 design token、字体、组件、图标和布局清单。
- Web 用 Browser/IAB 验证。
- Android 用 emulator/真机截图验证。
- 最终必须用 view_image 对照概念和实现截图。
- 有可修复视觉偏差时不得交付。

## 6. FM 一致性

- FM 方法以 plan.md 列出的 Python 文件为权威参考。
- 客户端必须计算，不把最终组合计算迁回服务端。
- 必须保留 FM Raw，不得被 Public Default 静默替换。
- 每个数值模块先写 golden fixture，再写 TypeScript 实现。
- 缺失信号不能当零。
- 禁止使用 eval 执行因子表达式。
- 禁止未来数据泄漏。

## 7. AI 与证据

- Challenge 必须提出真正的反方意见。
- 事实、用户声明和模型推断必须分开。
- DeepSeek 无搜索时使用客户端 SearchEvidenceBundle。
- 所有模型结构化输出必须经过 schema 验证。
- 模型不可用时显示失败，不生成伪造结果。

## 8. 密钥与隐私

- 绝不使用历史对话中暴露的 DeepSeek key。
- 新 key 只能从服务端 secret/environment 注入。
- 不得写入仓库、客户端 bundle、日志、数据库、截图或错误上报。
- 无 key 时先用 mock provider 完成可验证工作。
- 提交前运行 secret scan。

## 9. 公开产品安全

- MVP 不连接券商、不真实下单、不托管资金。
- 当前输出称为情景研究和组合模拟。
- 数据过期、证据不足或因子无效时必须降级。
- 法律审查和数据许可是公开生产发布的阻断 gate，不是可以静默跳过的待办。

## 10. 工程标准

- TypeScript strict。
- 核心领域不得使用 any。
- FastAPI 使用 Pydantic schema。
- 合同优先，Web/Android/API 共用版本化 schema。
- 每个功能包含成功、失败、取消和恢复路径。
- 不创建无功能的按钮或 placeholder 主流程。
- 测试覆盖数值边界、429/502、无搜索、无因子、数据过期和小资金。
- 记录重要 ADR。

## 11. 进度与验收

- 持续更新 src/docs/progress.md。
- 每个 phase 记录：改动、测试、证据、已知限制、下一步。
- Sol 验收失败后，Luna/Terra 修复并重新提交。
- 不因部分界面可见就宣称产品完成。
- 最终完成条件以 plan.md 第 19 节为准。

## 12. 何时询问用户

只在以下情况暂停询问：

- 需要新的外部权限或付费授权
- 需要合法可用的新 API key 才能进行真实 provider 验收
- 需要用户批准完整设计概念
- 法律、数据许可或品牌选择会实质改变结果
- 已满足 plan.md 的“客观无法执行”条件

其他可逆工程选择由执行者按 plan.md 自主决定。
