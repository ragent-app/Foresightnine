# A2：代码与数据盘点

记录时间：2026-11-11。基于只读代码和文件清单检查；尚未运行 A2 测试、未验证部署或迁移。以下“候选”均不是对质量、安全性或生产就绪的认证。

## 仓库基线与数据保全

| 对象 | 当前检查结果 |
| --- | --- |
| ForesightNine | `/home/lxx/Foresightnine`；`master`；提交 `3a4c75fc3b8a6f64a78f509e360f2c662eb5324f`，相对 origin/master ahead 1。检查时 `AGENTS.md` 和 `src/docs/product/` 为未跟踪文件，包含用户方案及本阶段成果；不可覆盖或清理。 |
| OpenClaw-stock | `/home/lxx/Openclaw-stock`；`main`；提交 `5c6f215bd656633676278747c9d1f4af680048ac`；检查时工作树无已显示修改。 |
| ForesightNine 资料 | `src/docs/data/` 约 48 KiB；`src/packages/test-fixtures/` 约 56 KiB；`src/packages/contracts/fixtures/` 约 40 KiB；`src/repair-evidence/` 约 82 MiB；根目录 `repair-evidence/` 约 32 KiB。大小是本次文件系统观察，不代表完整数据清单。 |
| OpenClaw 资料 | `reports/` 约 96 KiB，包含 daily report 与 social media JSON/TXT；`openclaw/` 约 1.2 MiB。未检查或复制报告内容，不假定其隐私、真实性和授权。 |
| 本机工具 | Node v22.23.2、Python 3.14.4；`pnpm` 和 `uv` 可执行文件存在。另发现忽略的 Web 和移动端本地环境文件；仅记录存在，未读取内容，不纳入成果或迁移。 |

上述目录只记录已发现的工作树资料，不证明不存在外部数据库、忽略文件、挂载存储或其他用户资料。迁移前需由实际部署环境补充数据库、对象存储、权限与备份清单；不复制密钥，不读取、修改或删除原资料。

## ForesightNine 候选资产

| 能力 | 直接代码证据与结论 | 差异与接入条件 |
| --- | --- | --- |
| 契约 | `src/packages/contracts/schemas/v1/` 存在研究、实体和契约 schema；候选复用 | 当前围绕旧 thesis、challenge、worldline；需为普通研究、来源、事实和用户确认设计版本化契约，不直接声称已经覆盖。 |
| 保存与并发 | `src/apps/api/app/storage.py` 有内存和 PostgreSQL JSON 文档仓库及版本冲突检查；`src/infra/migrations/001_research_state.sql` 有版本字段；候选改造 | 版本计数不等于保存可追溯历史版本；研究对象、历史快照、所有权与迁移兼容性须单独验证。 |
| 任务执行 | `src/apps/api/app/operations.py` 和 `operation_store.py` 包含取消、幂等、租约和持久操作状态；候选改造 | 按当前操作语义核验，不能假设复杂多步骤研究中断后可从任意步骤恢复；参考日志与成本粒度也需补齐。 |
| 证据与提供方 | `src/apps/api/app/phase3.py` 存在证据 URL 防护、客户端证据标识、引用 ID 验证、调用预算及取消；候选改造 | API 文档明确客户端 URL 证据不会由服务端抓取。引用 ID 校验不证明原文支持主张；需区分原文、抽取事实、计算与解释。 |
| 身份 | `src/packages/asset-registry/` 存在资产注册表；候选改造 | 核查公司、证券、交易所、币种、时间和跨市场映射，不沿用固定资产列表作为系统上限。 |
| 计算 | `src/packages/fm-engine/` 与 `test/parity.test.mjs`、`phase5.test.mjs` 存在；优先保留并验证 | 保持客户端计算、FM Raw 与约束结果分离。公式一致不证明投资有效性；普通研究计算工具仍需独立接口。 |
| Web 和导出 | `src/apps/web/src/research-workspace.ts`、`exports.ts` 存在；候选改造 | 当前以旧流程和模拟成果为中心；需核验新研究引用、用户确认、敏感数据排除及不同格式导出。 |
| 权限 | `src/apps/api/app/local_auth.py`、`oauth_*`、`security.py` 存在；待验证 | 存在认证代码不证明研究对象账户隔离；需要跨账户访问、导出和凭据泄露测试。 |
| 账户、计费、部署 | `apps/api/app/billing.py`、`infra/deployment/` 存在；待验证 | 不继承旧价格、配额、上线状态或运营承诺；盘点配置和实际运行证据。 |

## OpenClaw-stock 候选资产

| 能力 | 直接代码证据 | 处理 |
| --- | --- | --- |
| 宏观与其他数据 | `openclaw/skills/data_collection/free_data_sources.py` 中有 FRED、yfinance 等入口，若干失败返回 `None` | 逐接口核验授权、时间口径、单位和可观测失败；空值不可等同没有事件。 |
| 美港股行情 | `us_hk_stock_fetcher.py` 使用 Finnhub、yfinance 等路径，并有 `None` 故障结果 | 核验真实覆盖、延迟、价格调整、身份和许可；代码注释不作为服务承诺。 |
| 韩国公告 | `announcement_monitor.py` 在缺少密钥和部分异常情况下调用 `_generate_mock_announcements`，示例含 `mock: True` | 不直接迁移为真实数据源；必须隔离模拟输出并显式呈现故障。 |
| 财务指标 | `fundamental_data_fetcher.py` 使用 Finnhub；缺密钥、响应缺少指标或异常时 `_get_default_metrics()` 返回多项零值及 50 分，部分缺失字段也使用 0 | 不能把这些默认值或分数当真实基本面；逐字段保留缺失及失败状态，验证期间、币种、来源和授权。 |
| 宏观与行业默认值 | 同一文件的 `get_macro_indicators()` 以硬编码假设生成 GDP、失业、通胀、利率及 VIX，并附当前时间；`get_sector_strength()` 返回默认 Technology 和零值 | 这些是代码内假设，不是实际采集的当前宏观或行业数据。禁止迁移成真实事实；若用于测试须显式标为模拟。 |
| 新闻 | `openclaw/skills/data_collection/news_aggregator.py` 的 Naver 和 CryptoPanic 路径在缺凭据或异常时调用 `_generate_mock_news()`，结果带 `mock: True` 和示例链接 | 模拟新闻必须留在测试域；不可进入真实来源、事件或提醒。迁移前还需核验实际来源授权和失败状态。 |
| 监控/仓位/数据库 | `openclaw/core/scheduler.py`、`portfolio_manager.py`、`database.py` 存在；`DatabaseManager` 在 Redis 不可连接时退回进程内字典；`simple_portfolio_manager.py` 另直接使用 Redis 哈希 | 场景经验可参考；进程内回退不能作为持久研究、持仓及故障恢复保证，迁移前需盘点实际 Redis 实例、键空间、归属、备份及恢复。任务状态、账户隔离和统一资产身份待重新设计；本次未连接或读取用户 Redis。 |
| 回测 | `openclaw/skills/backtesting/` 存在多个引擎；已对一个价格查找函数和均线信号路径做初步检查，详见下文 | 已发现时间穿越风险及同柱成交假设，不能承接未经验证的收益结论；其他引擎与真实数据仍需分别核验。 |

### 旧回测的时间、成交与费用检查

- `enhanced_backtest.py` 的 `_get_price_at_timestamp` 返回首个时间戳大于或等于查询时间的 K 线收盘价；`_check_position_risk` 在处理信号时使用它。以合成数据调用原方法：请求 10:00:30、已有 10:00 价格 100、下一条 10:01 价格 200，函数返回 200。[独立函数探针](probe_old_backtest_time.py)和[结果](old-backtest-time-probe-2026-11-11.json)记录源码哈希与输入。该检查证明此输入会选到较晚时间戳，不等于已证明某一真实回测的收益偏差；K 线时间戳代表开盘还是收盘仍需按数据源确认。首次探针误写了类名，未生成结果；检查实际类名并修正探针后运行成功，未修改旧项目。
- 在此基础上，[风控路径探针](probe_old_backtest_risk.py)以原类的 `_process_signal` 做两次合成对照：两次均在 10:00:30 以 100 触发买入，手续费和滑点设为 0；唯一变化是历史数据中是否包含 10:01 的价格 80。不包含该记录时持仓保持；包含时返回了该较晚价格，并在 10:00:30 记录 80 的止损平仓。[结果](old-backtest-risk-probe-2026-11-11.json)保存输入、参数及源码哈希。探针只调用单次信号处理，不执行完整回测或真实成交，不能据此估算真实绩效偏差。
- `real_data_backtest_engine.py` 的均线计算包含当前 K 线收盘价，并将该 K 线的时间戳与收盘价写入信号；`enhanced_backtest.py` 在开仓时以信号价格加滑点成交。若该收盘价仅在柱结束后可知，却假设同时以该价格成交，可能引入不可实现的时点假设；具体影响取决于 K 线语义和可执行价格，需要以明确信号生成、订单提交和下一可成交价格的规则重测。
- `enhanced_backtest.py` 的风险检查在处理信号后执行，而不是逐根扫描所有 K 线；结束时以最后一条数据价格平仓。[运行循环探针](probe_old_backtest_risk_cadence.py)使用相同的合成价格 100→80→100、10:00 买入和固定参数：没有后续事件时，10:30 跌破止损但没有触发，最终在 11:00 按 100 结束平仓；增加一条 10:30 的合成 `HOLD` 事件后，代码虽未将其视为买卖，却执行风险检查并按 80 止损。[结果](old-backtest-cadence-probe-2026-11-11.json)记录了输入和来源哈希。此事件只是测试刺激，不表示真实系统一定产生它；探针保留原交易和风控循环，但因环境缺少依赖而替换日志和汇总指标函数，不能用于测算真实回测绩效。未覆盖价格缺口、盘中高低点和真实行情时间语义。
- 开平仓使用固定比例滑点与手续费，并按其代码记入盈亏；是否覆盖税费、点差、流动性、不同市场规则及真实可执行性未验证。用户要求的样本外、数据修订和信号时间验证尚未完成。

以上仅覆盖一个辅助函数及选定调用路径；`short_term_backtest.py`、其他策略和实际市场数据未完成独立审计。旧回测只能作为待修正实验材料，不能作为已验证投资效果或新产品默认策略。

## 旧基线差异和迁移约束

当前 Web 的 `ThesisInput.tsx` 包含资金字段；`apps/api/app/main.py` 的 `NORMAL_CONFIRM_MINIMUM_TOTAL = 30` 并在普通确认路径检查门槛。新方向允许问题或资料开始研究，金额仅在金额换算时需要，缺口和反方不阻断一般研究；这是阶段 B 需设计的行为变化，不表示现在已修改。旧世界线、概率和 FM 保留兼容读取与既有回归边界，是否采用可选模块或双入口仍待决策。

迁移前制定显式版本转换与兼容读取：保留源记录和历史版本，双向核对关键字段及所有权，建立备份与恢复演练；旧模型推断不得转换成已验证事实。旧计数器或乐观锁版本不自动构成历史版本。未实施任何迁移或清理。

## 验收状态

本次运行 `uv run --project apps/api pytest apps/api/tests/test_storage.py apps/api/tests/test_operations.py apps/api/tests/test_operation_store.py -q`，退出码 0；运行 `pnpm --filter @foresightnine/fm-engine test`，20 项通过、退出码 0。这些检查支持现有存储、操作和 FM 的测试基线，不覆盖新版研究流程、生产 PostgreSQL、跨账户隔离、外部服务或投资有效性。

### 账户与匿名会话边界补充核验

2026-11-11，在本机内存存储、模拟提供方条件下运行三项现有测试：`test_operation_receipts_and_idempotency_are_account_scoped`、`test_authenticated_routes_isolate_keys_clear_and_route_personal_without_billing` 和 `test_telemetry_allowlist_does_not_retain_private_content_or_identifiers`，均通过。第一项覆盖两个已登录账户之间的操作回执、部分 Challenge 读取、取消及幂等键隔离；其他两项覆盖个人模型凭据及遥测脱敏。它们不能证明全部研究对象或生产环境均隔离。

同日另用两个独立的本地 `TestClient`、不带认证，在进程内合成会话和 Challenge：客户端 A 创建会话返回 201、创建 Challenge 返回 201；客户端 B 在已知该 Challenge ID 时调用 `GET /v1/challenges/{id}` 返回 200。[复现脚本](probe_anonymous_isolation.py)固定内存存储及模拟提供方，[运行结果](anonymous-isolation-probe-2026-11-11.json)不保存真实用户数据。`apps/api/app/main.py` 中 `_operation_owner_subject` 对无认证请求返回同一个 `anonymous` 主体，`_require_session_owner` 据此检查。该实验只证明**知道标识时两个匿名客户端的访问没有按客户端隔离**，不证明标识可被猜测，也不证明已登录账户隔离失败；它不适合作为私人研究资料的账户隔离保证。匿名记录迁移和公开用途必须独立审查；阶段 B 应采用可验证的账户所有权或安全的匿名访问凭证，并测试跨用户读取、写入、导出及兼容路径。本次未修改原功能或部署。

静态盘点记录了可见代码基线、已发现资料、候选能力、旧新差异和迁移约束；部署外部数据库与存储清单、跨账户隔离和迁移恢复仍待具体环境验收。A2 可以作为 B 规格的输入，外部数据资产未知项继续保留，不表述为已完成生产迁移准备。
