# ForesightNine 产品与实施计划

## 0. 文档状态

- 文档版本：v1.0
- 建立日期：2026-08-10
- 项目目录：D:\Foresightnine
- 唯一实施目录：D:\Foresightnine\src
- 状态：已批准的执行基线
- 变更规则：除非某一步经过验证后客观无法执行，否则不得修改本计划。若必须修改，只允许做解除阻塞所需的最小修改，并记录原计划、证据、替代方案、影响和复核结果。

本计划的目标不是制作静态演示，而是完成一个公开可访问、可审计、可扩展的观点到世界线到组合配置产品，同时提供 Web 与 Android 两个客户端。

## 1. 已确认的产品决策

1. 产品面向公众开放。
2. 长期目标覆盖所有资产类别，不把业务模型硬编码为 9WL 的 15 个可交易标的。
3. 用户除观点外，只强制输入资金金额；资金金额用于把组合权重换算为配置金额和可交易份额。
4. 概率拆分为：
   - 前提概率 P(premise)
   - 前提成立后的条件路径概率 P(worldline | premise)
   - 联合概率 P(worldline) = P(premise) × P(worldline | premise)
5. Challenge 使用结构化审查 rubric；用户可以在 AI 保留异议的情况下确认观点。
6. 优先使用 OpenCode DeepSeek 自带检索；若不可用，接收用户客户端提交的搜索结果、链接和证据摘录。
7. 初期通过现有 OpenCode 兼容网关调用付费 DeepSeek Flash，只进行小规模测试。
8. 因子与组合计算采用实验 FM 的方法，并在用户端执行。
9. 技术栈采用 React、TypeScript、React Native Expo、FastAPI、PostgreSQL。
10. 视觉采用 Claude.ai 式克制、编辑感、对话优先的产品语言，但不得复制 Claude 商标、logo、专有插画或造成官方关联误认。
11. MVP 完成完整纵向闭环，不接券商真实下单。

## 2. 产品定位与边界

### 2.1 产品定位

ForesightNine 是情景研究与组合模拟工具。它把用户观点转化为：

观点草案 → 反方审查 → 证据补全 → 世界线 → 概率树 → 资产影响 → FM 因子组合 → 持仓配置 → 压力测试与解释。

### 2.2 面向公众时的表达边界

- 产品输出必须标注为模型生成的研究情景和组合模拟，不承诺收益。
- 不使用“保证”“必然”“稳赚”“确定性预测”等表达。
- 概率必须显示来源、区间、置信度和是否经过历史校准。
- 组合结果必须显示数据时点、市场数据来源、方法版本和风险提示。
- 在完成目标司法辖区的法律审查前，不连接券商、不代客下单、不托管资金、不收取按收益计算的费用。
- 正式公开部署是单独的 launch gate：隐私政策、服务条款、风险披露、数据许可和投资建议合规必须审核通过。

### 2.3 “所有资产”的工程定义

“所有资产”通过可扩展 Asset Registry 实现，而不是在代码中枚举固定资产：

- 股票、ETF、基金、指数
- 国债、信用债、收益率曲线和债券 ETF
- 外汇与货币篮子
- 商品现货、商品 ETF、期货
- 加密资产
- 房地产与 REIT
- 期权及其他衍生品
- 现金及现金等价物

MVP 必须以统一 schema 支持这些类别，并至少实际打通：

- 现有 benchmark 15 个可交易暴露
- 主要全球股票/债券/商品 ETF
- BTC、ETH
- 现金等价物

期权、期货等具有杠杆、保证金和非线性风险的资产可以进入注册表和影响分析，但只有在数据、合约乘数、期限、Delta、保证金和风险模型齐全时才允许进入默认配置。无法直接交易的指数或收益率必须映射为可交易代理工具，并同时保留原始经济暴露标识。

## 3. MVP 用户闭环

### 3.1 观点输入

首屏只要求两项核心输入：

1. 用户观点：自然语言。
2. 可配置资金：金额，货币默认按客户端区域设置推断，用户可在金额控件中切换币种，但不增加额外问卷。

AI 从观点中提取并让用户确认：

- 前提
- 时间范围
- 触发事件
- 中间因果链
- 受益/受损资产
- 影响方向与强度
- 用户声称的专业背景或私有证据
- 可证伪条件

### 3.2 Challenge

DeepSeek 扮演结构化反方审查者，不能只做顺从式扩写。每轮输出：

- 当前主张
- 最强反例
- 缺失证据
- 逻辑跳跃
- 市场可能已经定价的部分
- 推荐补充的公开证据
- 对用户的一个到三个高价值问题
- rubric 分数与理由

### 3.3 确认与保留异议

当 rubric 达标时，AI 可以建议确认。用户也可以提前选择“带保留意见确认”。系统必须保留：

- 未解决问题
- AI 反方观点
- 用户的最终确认
- 用户是否声明拥有不可公开验证的业内信息
- 每项证据的可验证等级

### 3.4 世界线

生成一条主世界线和至少两条相邻分支：

- 主路径
- 前提成立但传导失败的弱路径
- 前提成立且产生二阶效应的强路径

每条路径包含时间轴、事件节点、状态转移、资产锚点、事件揭示时间、失效条件和概率。

### 3.5 因子和持仓

客户端获取签名的世界线、因子库快照和必要市场面板后，在本地完成：

- 因子表达式求值
- IC、ICIR 和有效性检查
- 因子选择
- 因子合成
- 目标权重计算
- 成本死区判断
- 金额和份额换算
- 组合压力测试

### 3.6 输出

结果页必须同时显示：

- 世界线摘要
- 三层概率
- 主要支持与反对证据
- 资产影响矩阵
- FM 原始权重
- 公开产品安全约束后的建议权重
- 各资产金额、单位数、币种和可交易代理
- 预期风险、最大回撤情景、换手和费用假设
- 方法与数据版本
- 风险披露

## 4. Challenge 与证据系统

### 4.1 Rubric

每项采用 0 到 4 分：

1. 前提清晰度
2. 时间边界
3. 触发事件可观察性
4. 因果链完整性
5. 反事实与替代解释
6. 证据质量
7. 资产传导合理性
8. 市场已定价程度
9. 可证伪性
10. 用户确认程度

默认通过条件：

- 总分至少 30/40
- 前提、时间、因果链、资产传导、可证伪性均不得低于 3
- 仍有重大缺口时必须显示保留意见

### 4.2 证据等级

- A：原始数据、监管文件、公司公告、同行评议论文
- B：可靠媒体、机构研究、可追溯数据库
- C：二手评论或专家访谈
- D：用户内部经验或不可外部验证信息
- E：模型推断

模型不得把 D 或 E 描述成公开事实。

### 4.3 搜索优先级

1. OpenCode DeepSeek 原生搜索或 tool call。
2. 后端 SearchEvidenceProvider。
3. 用户客户端提交的 SearchEvidenceBundle。

SearchEvidenceBundle 至少包含 URL、标题、发布日期、检索时间、摘录、用户说明和内容哈希。只有客户端提交摘要而没有 URL 时，必须标为不可独立复核。

### 4.4 Provider 抽象

定义：

- LlmProvider
- SearchEvidenceProvider
- MarketDataProvider
- AssetRegistryProvider
- WorldlineGenerator
- ProbabilityAssessor

OpenCode DeepSeek Flash 是首个实现，但业务层不得依赖提供商私有响应格式。

## 5. 世界线数据模型

所有核心对象都必须有 schema_version、created_at、source_refs 和 content_hash。

### 5.1 ThesisDocument

- thesis_id
- raw_text
- normalized_premise
- horizon
- trigger_events
- claimed_asset_impacts
- capital
- base_currency
- user_private_evidence_claims
- status

### 5.2 EvidenceItem

- evidence_id
- source_type
- url
- title
- publisher
- published_at
- retrieved_at
- excerpt
- grade
- supports_or_challenges
- content_hash

### 5.3 ChallengeRecord

- turn_id
- claims
- objections
- missing_evidence
- questions
- rubric_scores
- unresolved_issues
- user_confirmation

### 5.4 Worldline

- worldline_id
- title
- premise
- baseline_date
- horizon_end
- nodes
- edges
- asset_anchors
- event_reveal_schedule
- invalidation_conditions
- provenance

每个节点包含：

- node_id
- time_window
- event
- observable_indicators
- causal_parents
- state_changes
- confidence

### 5.5 ProbabilityAssessment

- premise_probability
- premise_probability_range
- conditional_probability
- conditional_probability_range
- joint_probability
- confidence
- base_rate_refs
- adjustment_reasons
- calibration_status

MVP 中若没有足够历史样本，calibration_status 必须是 model_estimate_unvalidated，禁止显示伪精确小数。

### 5.6 AssetImpact

- economic_exposure_id
- instrument_id
- direction
- severity
- start_window
- peak_window
- decay
- first_order_reason
- second_order_reason
- already_priced_score
- evidence_refs

## 6. 世界线路径生成

### 6.1 复用现有 9WL 方法

新的任意观点世界线应继承已有 benchmark 的可审计方法：

- 基准日使用当前可见市场数据
- 事件和资产锚点分离
- 价格类资产在锚点间采用对数路径
- 收益率和波动率类采用水平路径
- 可选波动桥噪声在锚点归零
- 新闻可见日与事件发生日分离
- 每个事件必须有 leak/reveal 规则
- 任何决策只使用当时可见数据

现有 9WL 是模板、测试夹具和回归集，不把九条场景当作未来事实。

### 6.2 概率流程

1. 从公开证据提取基础发生率。
2. 估计前提概率区间。
3. 在前提成立条件下估计路径概率。
4. 计算联合概率。
5. 对相邻分支做概率归一和一致性检查。
6. 输出支持证据、反方证据和敏感性因素。

概率展示以区间为主，例如 15%–25%，不默认展示 21.37% 一类伪精确值。

## 7. 市场数据与资产注册表

### 7.1 Asset Registry

每项资产至少包含：

- asset_id
- display_name
- asset_class
- subtype
- exchange
- currency
- timezone
- price_type
- tradable
- proxy_instruments
- contract_multiplier
- lot_size
- fractional_supported
- liquidity_tier
- data_license
- provider_symbol_map

### 7.2 数据字段

- datetime
- asset_id
- open
- high
- low
- close
- volume
- amount
- currency
- adjusted
- provider
- as_of

衍生品扩展字段：

- expiry
- strike
- option_type
- delta
- implied_volatility
- margin
- contract_multiplier

### 7.3 数据许可

公开产品不得直接再分发没有授权的 Suntime、交易所或商业数据库内容。开发期可以使用：

- 用户已授权的本地数据
- 现有 9WL 测试快照
- 明确允许开发/展示的公共数据
- mock provider

正式上线前必须替换为有许可的 MarketDataProvider，并记录每个字段的来源和刷新时间。

## 8. 客户端 FM 计算规范

### 8.1 权威参考实现

只读参考：

- D:\FM acceleration-deepseek\bundle\agent-framework\scheduler\fm_walk_forward.py
- D:\FM acceleration-deepseek\bundle\agent-framework\FactorMiner\factorminer\evaluation\combination.py
- D:\FM acceleration-deepseek\bundle\agent-framework\FactorMiner\factorminer\evaluation\portfolio.py
- D:\FM acceleration-deepseek\bundle\agent-framework\FactorMiner\factorminer\evaluation\metrics.py
- D:\FM acceleration-deepseek\bundle\agent-framework\FactorMiner\factorminer\evaluation\backtest.py
- D:\FM acceleration-deepseek\bundle\agent-framework\ASSETS.yaml

不得修改这些源文件。客户端实现必须通过 golden fixtures 与 Python 输出逐项对齐。

### 8.2 因子选择

1. 因子按 abs(IC mean) × abs(ICIR) 降序。
2. 默认最多选择 top 10。
3. 因子录取门槛从因子库元数据读取，不硬编码。
4. 缺失或求值失败的因子采用 reject 策略。

### 8.3 因子合成

对每个因子最后可见日的横截面信号：

1. 百分位排名。
2. contribution = sign(IC mean) × (percentile_rank - 0.5)。
3. quality_weight = max(abs(IC mean), 1e-6) × max(abs(ICIR), 1e-6)。
4. 每个资产只在产生有效信号的因子之间加权平均。
5. 缺失信号不得当作零信号。

### 8.4 预测收益缩放

1. 计算 forecast_horizon 日收益。
2. 每日计算横截面波动率。
3. 取最近 252 个有效时点的中位数作为 return_scale。
4. forecast = signal_z × mean_abs_ic × return_scale。
5. 该 forecast 只用于成本死区和解释，不描述为精确收益预测。

### 8.5 原始目标权重

1. 对合成信号横截面中心化并标准化。
2. kappa 默认 1。
3. raw_weight_i = (1 + kappa × z_i) / N。
4. 负权重截断为 0。
5. 重新归一化到总权重 1。
6. 无有效信号时回退为全资产等权。
7. FM parity 输出为 long-only、完全投资、支持小数份额、现金权重 0。

### 8.6 成本与调仓

- 初始配置成本默认 0。
- 后续资产迁移成本默认 3 bps。
- 默认每 10 个交易日计算一次。
- 如果预期增益不超过迁移成本，触发 cost dead-band 并保持原组合。
- 计算中不得使用决策日之后的数据。

### 8.7 公开产品安全层

结果页同时保留：

1. FM Raw：完全忠实于参考实现。
2. Public Default：在 FM Raw 基础上施加可解释的公开产品约束。

Public Default 初始约束：

- long-only
- 总权重 100%
- 不使用杠杆
- 单一证券权重上限 25%
- 单一高波动资产类别上限 35%
- 流动性不足或数据过期资产不得配置
- 不能直接交易的经济指标映射到可交易代理
- 衍生品默认以风险等价暴露而非名义金额归一

任何约束造成的权重变化必须在 UI 中逐项解释。Sol 验收时必须确认 FM Raw 没有被安全层悄悄覆盖。

### 8.8 客户端实现方式

建立共享 TypeScript 包 packages/fm-engine：

- 使用 Float64Array
- 自己实现确定性的 rank、tie handling、mean、std、Spearman、IC、ICIR、turnover
- 因子 DSL 使用白名单 AST，不允许 eval 或动态执行任意代码
- Web 在 Web Worker 中运行
- Android MVP 在 Hermes 中运行小规模计算；数据规模超过阈值时采用分块计算
- 后续若性能不足，再用 Rust 核心分别编译为 WASM 和 React Native JSI；这属于经 benchmark 证明后的优化，不是首选重写

## 9. 组合金额和份额

用户只输入资金，不输入持仓或风险问卷。系统因此使用公开、固定、可见的默认约束。

计算：

- target_amount_i = capital × target_weight_i
- fractional_supported 时：units_i = target_amount_i / price_i
- 不支持小数时：按 lot_size 向下取整
- 舍入余量进入现金等价物
- 多币种资产先按 as_of 汇率换算
- 指数和收益率暴露先映射可交易代理

资金只缩放金额和份额，不改变原始 FM 权重。若小资金无法满足最小交易单位，必须明确显示无法实施的资产和替代 ETF/基金。

## 10. 技术架构

### 10.1 Monorepo

所有实现位于 D:\Foresightnine\src：

~~~
src/
  apps/
    web/                 React + Vite + TypeScript
    mobile/              React Native + Expo
    api/                 FastAPI + Python，使用 uv
  packages/
    contracts/           OpenAPI/JSON Schema 与生成的 TS 类型
    domain/              纯 TypeScript 领域对象和状态机
    fm-engine/           客户端 FM 计算
    design-system/       Web/Android 共用 token 与组件语义
    asset-registry/      资产、代理、币种和交易规则
    test-fixtures/       Python 对齐夹具和 9WL 回归夹具
  infra/
    docker/
    migrations/
    deployment/
  docs/
    architecture/
    api/
    compliance/
    qa/
~~~

### 10.2 Web

- React
- Vite
- TypeScript strict
- TanStack Query
- Zustand 或同等级轻量状态管理
- React Router
- Web Worker 承载 FM 计算
- 图表使用可访问、可导出的 SVG/Canvas 方案

### 10.3 Android

- React Native
- Expo
- Expo Router
- 与 Web 共用 contracts、domain、fm-engine 和 design token
- 使用 Android Share Intent 接收用户从浏览器分享的搜索结果或链接
- 支持离线查看已完成的世界线和组合快照

### 10.4 API

- FastAPI
- uv 管理 Python 环境
- Pydantic v2
- SQLAlchemy 或 SQLModel
- PostgreSQL
- Redis 用于限流、短期缓存和异步任务状态
- SSE 用于流式 Challenge 和世界线生成

### 10.5 存储

MVP 默认匿名 session：

- 服务端保存最少必要状态
- 对话和证据设置过期时间
- 不保存 API key
- 本地保存可导出的加密研究档案

公开测试必须有：

- IP/session 限流
- CAPTCHA 或同等级滥用控制
- 每日模型调用配额
- 请求大小限制
- 审计事件但不记录秘密

## 11. API 合同

建议端点：

- POST /v1/sessions
- POST /v1/challenges
- POST /v1/challenges/{id}/messages
- POST /v1/challenges/{id}/evidence
- POST /v1/challenges/{id}/confirm
- POST /v1/worldlines
- GET /v1/worldlines/{id}
- POST /v1/probabilities/assess
- GET /v1/assets/search
- GET /v1/assets/{id}
- POST /v1/market-data/manifests
- POST /v1/market-data/shards
- GET /v1/factor-libraries/{version}/manifest
- GET /v1/factor-libraries/{version}/shards
- POST /v1/client-computations/{id}/attest
- GET /v1/methodology
- GET /health

服务端不直接返回未经 schema 验证的 LLM 文本。所有结构化结果先做：

- JSON schema 验证
- 概率范围验证
- 资产 ID 验证
- 引用完整性验证
- 内容安全检查

## 12. 状态机

~~~
DRAFT
  -> CHALLENGING
  -> EVIDENCE_REQUIRED
  -> CHALLENGING
  -> CONFIRMED
  -> WORLDLINE_GENERATING
  -> WORLDLINE_READY
  -> DATA_PREPARING
  -> CLIENT_COMPUTING
  -> PORTFOLIO_READY
  -> EXPORTED
~~~

任何失败必须可恢复，不允许因为刷新页面而重复扣费或丢失已确认的证据。

## 13. DeepSeek/OpenCode 集成

### 13.1 安全

- 先前在对话中暴露的 key 视为失效，绝不使用。
- 新 key 只能由部署者放入服务端 Secret Manager 或未提交的环境变量。
- Web bundle、Android bundle、source map、日志、错误上报和数据库均不得出现 key。
- 增加 secret scanning 测试。

### 13.2 小规模测试模式

- 默认模型调用并发 1
- 会话轮次上限
- token 和金额预算
- 429/5xx 指数退避
- provider timeout
- 可取消请求
- mock provider 保证无 key 时仍可完成 UI、合同和 FM 验证

### 13.3 Prompt 合同

Challenge、世界线和概率分别使用独立 prompt 模板。每个模板：

- 有版本号
- 有输入 schema
- 有输出 schema
- 有反顺从要求
- 有证据引用要求
- 有未知项处理要求
- 有 prompt injection 防护
- 有 snapshot tests

## 14. Claude.ai 启发的设计方向

### 14.1 设计原则

- 安静、对话优先、编辑感
- 温和但不是装饰性的背景
- 清晰的长文本阅读体验
- 克制的边框、阴影和动效
- 少卡片、少仪表盘噪音
- 重点使用开放布局、对话流、证据抽屉和时间线
- 组合页可以提高信息密度，但必须保持排版纪律

不得复制：

- Claude 名称和 logo
- Anthropic 专有插画
- 完全相同的导航、文案或品牌标识
- 会造成官方产品误认的视觉组合

### 14.2 设计稿范围

编码前必须生成并审查完整概念：

Web：

1. 观点输入空状态
2. Challenge 对话状态
3. 证据补全抽屉
4. 世界线时间轴和概率树
5. 因子计算进度与解释
6. 组合结果与压力测试
7. 方法、数据和风险披露

Android：

1. 输入
2. 对话
3. 证据
4. 时间线
5. 组合
6. 分享/导出

必须先用 Image Gen 生成完整可读概念，获得用户批准后才实现可视层。批准后的概念是生产规格。

### 14.3 视觉验收

- Browser/IAB 优先验证 Web
- Android 使用 Expo/Android emulator 截图
- Desktop、窄屏和 Android 三套视口
- 用 view_image 同时检查概念和实现截图
- 至少记录五项逐点对照
- 不允许裁切、溢出、默认浏览器字体、假按钮或无响应控件

## 15. 测试策略

### 15.1 FM Golden Tests

从只读 Python 参考实现导出固定输入和输出：

- tie ranks
- NaN
- 正负 IC
- 全部无效因子
- 单因子
- top 10 因子
- 权重截断
- 等权回退
- 3 bps 成本
- cost dead-band
- 多币种
- lot rounding

TypeScript 与 Python 对齐标准：

- 中间 signal 和 weights 绝对误差不超过 1e-10
- 金额误差不超过最小货币单位
- 相同输入产生稳定哈希

### 15.2 Worldline Tests

- schema 验证
- 节点无环
- 时间单调
- 概率范围合法
- 分支条件一致
- 联合概率计算正确
- 事件时间与 reveal 时间分离
- 不引用未来数据

### 15.3 Provider Tests

- DeepSeek 成功
- 无搜索能力
- 用户客户端证据 fallback
- 429
- 502
- timeout
- invalid JSON
- prompt injection
- citation mismatch

### 15.4 Web/Android

- 核心闭环 E2E
- 无障碍键盘/读屏
- Android 返回键和分享入口
- 响应式布局
- 断网恢复
- 流式响应取消
- FM 计算进度与取消
- 导出结果可重复打开

### 15.5 安全

- secret scan
- dependency audit
- rate limit
- CORS
- CSP
- 输入长度
- 上传内容类型
- SSRF 防护
- URL 抽取和搜索结果清洗
- 日志脱敏

## 16. 实施阶段

### Phase 0：项目基线和证据盘点

交付：

- src monorepo 骨架
- README
- ADR 模板
- 源代码/数据只读清单
- FM 参考实现哈希
- 不可使用的旧 key 记录为 revoked，不记录其值

验收：

- 所有新代码只在 src
- 没有修改 FM、9WL 或论文源目录
- 无秘密进入 Git

### Phase 1：合同优先

交付：

- JSON Schema
- OpenAPI
- TS/Python 生成类型
- 状态机
- mock 数据
- contract tests

验收：

- Web、Android、API 使用同一 schema
- 无 any 逃逸核心领域类型

### Phase 2：完整视觉概念

交付：

- Web 各核心状态概念图
- Android 各核心状态概念图
- 设计 token
- 组件清单
- copy 清单
- 用户批准记录

验收：

- 完整产品面而不是单一 hero
- Claude.ai 气质但无品牌复制
- 可实现、可读、移动端完整

### Phase 3：Challenge 和 Evidence

交付：

- OpenCode DeepSeek adapter
- mock adapter
- 搜索能力检测
- 客户端证据 fallback
- rubric
- 流式对话
- 确认和保留异议

验收：

- 无 key 也可用 mock 完成闭环
- DeepSeek 小规模测试通过
- 每项事实有证据等级

### Phase 4：世界线和概率

交付：

- 世界线生成器
- 概率分解
- 分支一致性检查
- 9WL 回归夹具
- 时间线可视化数据

验收：

- P(premise)、P(path|premise)、联合概率可复算
- 未校准状态明确
- 无伪精确概率

### Phase 5：客户端 FM Engine

交付：

- AST DSL
- operators
- metrics
- factor selection
- factor combination
- target weights
- forecast scaling
- cost dead-band
- rounding
- golden fixtures

验收：

- 与 Python 参考实现在容差内一致
- Web Worker 路径通过
- Android 小规模数据通过
- 服务端不计算用户最终组合

### Phase 6：Web

交付：

- 完整主流程
- 响应式
- 本地计算
- 图表
- 导出
- 方法和风险披露

验收：

- Browser 实际操作闭环通过
- 概念与截图 10/10 对照
- 桌面和移动 Web 无严重偏差

### Phase 7：Android

交付：

- Expo app
- 与 Web 共用合同和 FM engine
- Share Intent 证据导入
- 离线快照
- Android build

验收：

- emulator/真机核心闭环
- 计算结果与 Web 一致
- Android 生命周期和返回键行为正确

### Phase 8：公开测试加固

交付：

- 配额
- 限流
- CAPTCHA
- 数据清理
- 隐私和风险文档草案
- 监控
- 故障降级

验收：

- 不泄漏密钥和隐私
- 模型不可用时不产生伪结果
- 数据过期时禁止给出“当前配置”

### Phase 9：发布候选

交付：

- Web release candidate
- Android APK/AAB 测试包
- 部署说明
- 数据提供商切换说明
- 安全审计
- QA 报告
- 已知限制

公开发布前仍需：

- 目标地区法律审查
- 市场数据许可
- 隐私政策和服务条款批准
- 新 DeepSeek key 的安全注入

## 17. 关键验收场景

### 场景 A：完整观点

用户输入结构完整观点和资金，Challenge 两轮完成，生成世界线、概率和组合。

### 场景 B：业内人士声明

用户声称拥有行业信息但不给公开证据。系统可以继续，但必须把该依据标为不可外部验证并保留反方意见。

### 场景 C：搜索不可用

OpenCode 没有 search tool，客户端提交 URL 和摘录，系统完成证据分级。

### 场景 D：无有效因子

客户端 FM engine 返回等权 fallback，UI 解释原因，不能伪造高置信策略。

### 场景 E：小资金

资金无法购买最小交易单位，系统用代理或现金等价物处理舍入余量。

### 场景 F：数据过期

数据超过 freshness threshold，系统只显示历史模拟，不显示当前可执行金额。

### 场景 G：模型故障

429、502 或超时后可重试、取消或恢复；不得重复生成收费请求。

## 18. 风险登记

1. 公开组合建议触发监管要求。
   - 缓解：研究模拟定位、固定默认约束、无下单、launch legal gate。
2. “所有资产”导致数据和计算范围失控。
   - 缓解：Asset Registry、按相关性选择候选集、分片下载、适配器。
3. DeepSeek 搜索能力不稳定。
   - 缓解：能力检测和客户端证据 fallback。
4. LLM 概率不校准。
   - 缓解：区间、基础率、calibration_status、回测后再升级。
5. FM Python 到 TypeScript 漂移。
   - 缓解：逐公式 golden tests 和中间值对齐。
6. Android 本地计算性能不足。
   - 缓解：小规模 MVP、分块、benchmark 后再 Rust/JSI。
7. 数据许可不允许公开再分发。
   - 缓解：provider adapter、mock/授权数据、上线许可 gate。
8. API 成本和滥用。
   - 缓解：匿名配额、限流、CAPTCHA、缓存、调用预算。
9. 用户把输出当成确定性投资建议。
   - 缓解：方法、异议、概率区间、风险披露和不确定性同屏。

## 19. 完成定义

项目只有同时满足以下条件才可声明完成：

- Web 与 Android 都能跑通 MVP 闭环
- Challenge 不是顺从式聊天，rubric 可见
- 搜索和客户端证据 fallback 都有测试
- 世界线 schema、时间线和概率可审计
- FM 在客户端计算并与 Python golden fixtures 一致
- 只输入观点和资金即可得到配置
- 输出 FM Raw 和 Public Default 两套权重且差异可解释
- 没有真实下单
- 没有任何 API key 泄漏
- 设计概念经批准，最终截图通过 view_image 对照
- Browser 和 Android 验收通过
- 所有核心测试通过
- 已知限制、合规 gate 和数据许可状态清楚

## 20. 计划修改纪律

执行者不得因为实现偏好、重构冲动、库选择或局部困难修改 plan.md。

只有满足全部条件时可以修改：

1. 原步骤客观无法执行，而非仅仅困难。
2. 已完成至少三种安全的验证或替代尝试。
3. 有可复核证据。
4. 先在 docs/plan-deviations.md 记录。
5. 修改范围是解除阻塞的最小范围。
6. Sol 复核通过。

用户的新明确指令可以覆盖本计划，但必须记录覆盖项。
