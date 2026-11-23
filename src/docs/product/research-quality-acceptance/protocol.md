# 研究质量受控验收协议

冻结日期 2026-11-23。代码起点 HEAD `879de1d`，并以 `start-manifest.json` 所列当时未提交文件覆盖；这些文件的 SHA-256 是对照指纹，未列文件取该提交。数据均为 `cases.json` 中的合成材料，不代表实际公司或市场，允许本项目测试使用，不依赖外部资料；FRED 禁用。开发题 D1–D4 与保留题 H1–H4 使用不同主体、文本和问题；保留题为工程检查，不冒充正式质量盲测或人工基线。该文件、判据和快照在本次模型外呼前冻结。可接受证据是每题引用的合成材料及由它们确定性计算的结果；不能使用题外模型知识补足。

E1 对照工具计数反馈与实体、期间、角色绑定反馈；固定任务、快照、工具、模型、生成参数、提交策略和预算。E2 在同一证据与同一提交上比较旧/新状态判定，再检验同提醒策略的正常结束；检查漏答误判完成与齐全误判部分完成。E3 同一证据包比较旧正文与绑定主张提交，核查定位、支持、计算与推断限定；无法独立判断的语义记未评估。E4 同轨迹注入已保存事实、已保存计算、提交写入、未知外呼等故障，比较重开后的保留、权限与重复外呼；PostgreSQL 重开另行记录，内存测试不能替代。所有版本记录提交及工作树指纹，逐题保留请求、工具回执、结果与失败原因；不得记录密钥。

真实模型运行前必须补充并冻结实际 wire 协议、模型及不可锁定版本、支持的参数、工具 schema 和提示的 SHA-256、授权快照哈希、每组调用/搜索/token/时间/费用上限与运行顺序。每组同一预算，先在独立基线确定用量，不得据结果追调。起步 E1 四题，每题 A/B 各两次，交替 AB/BA，共 16 次；运行需环境稳定，错误和未知费用如实计入。未完成预登记前不调用模型。实验预算不改变产品默认预算。

逐项记录缺证误报完成数/确有缺口数、齐全误报部分完成数/齐全数、有据回答核心分项/核心分项总数、引文定位/可评估引文、支持/可评估主张、可复算/可评估计算，以及恢复保留/故障前可核验且仍获授权分项；另列未知外呼、重复计费、权限违规、调用量、耗时、已知费用。分母为零和语义无法评估写“无法判定”，不做零值替代。所有运行入统计；未取得的人工核验与独立测试质量阈值保持未完成。

## 基线实测

在 `/tmp/fn-quality-baseline` 从 `git archive 879de1d` 解出并覆盖起点快照，使用当前 Python 环境执行 `python -m pytest tests/test_research_runtime_attention.py::test_presubmit_gate_nudges_once_for_unmet_method_pack_then_accepts -q`：1 passed。测试 A 读来源、提交只含收入的报告、收到一次缺证提醒、原样重交，旧结果为 completed；B 补入两项收入、执行增长计算与普通风险搜索，仍未回答 EPS，旧结果也为 completed。它证明该冻结轨迹的旧状态行为，不证明总体误报率，也不是模型质量实验。

启动前环境未配置 `FORESIGHTNINE_DISPOSABLE_POSTGRES_DSN`，且宿主机无 `pg_isready` 命令；其后已在临时容器验证 PostgreSQL，见下。历史恢复测试和早期交付整改实验保持原协议，不在此修改其限额或结果。

## PostgreSQL 初查（非最终集成验收）

临时 PostgreSQL 16 Alpine、128 MiB、随机密码与本地随机端口；独立新库应用 `001_research_state.sql`、`002_operation_state.sql`。初次未加载迁移导致表不存在，已中止并重建；不视为产品失败。加载迁移后，旧 `test_interrupted_research_retains_inputs_and_cannot_replay_external_call[before_provider]` 在旧模拟 provider 故障点等待超时；另外两个旧版本测试分别因事实必填字段与当前 `ResearchContent` 契约不符而无法到达重开断言。旧恢复验收中的部分项目因测试进程未设 mock 本地认证而返回 503。包含 PG 双进程准入在内共 20 项通过，19 项失败；这些失败不证明实际恢复失败，也不能宣称矩阵通过。容器已删除。最终需使用当前 native runtime 的故障注入重新验收持久重开。

## 当前 native API 与 PostgreSQL

以新建隔离 PostgreSQL 16、128 MiB，等待目标库可执行 SQL、应用两项迁移，设置 mock 认证，顺序运行当前 `test_completed_submission_recovers_without_another_model_call`、`test_partial_continue_respects_cumulative_budget_and_unknown_blocks`、`test_postgres_two_processes_cannot_admit_same_topic_concurrently`：3 passed。覆盖已提交结果版本冲突后 recover 不新增模型调用、部分续跑累计预算与未知外呼阻止再续跑、跨进程同主题并发准入。前两项在 PostgreSQL 存储上执行真实 API 恢复，但不是进程重启测试；未覆盖分项在 PostgreSQL 进程重启后导出。容器和临时数据已删除。另一次初次启动连接中断、一次目标数据库未创建，属于初始化就绪竞争；等待目标库 SQL 后解决。

为避免依赖临时目录，起点未提交文件及逐文件 manifest 已打包为 `start-snapshot.tar.gz`，SHA-256 `3ad124ceb265794c9b6c5368ad8dbae38601b7ba48b818af2f6e2c908698bfee`；`python3 src/docs/product/research-quality-acceptance/replay.py` 使用该包叠加 git commit 并校验所有文件哈希。本机以打包副本再次重放旧轨迹通过。可选 `--current-test` 重复传入当前 pytest 节点，以相同命令同时运行当前断言。
