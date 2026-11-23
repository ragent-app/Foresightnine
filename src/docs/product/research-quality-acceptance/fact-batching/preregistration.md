# 事实分批指令：冻结 A/B 预登记

2026-11-23，外呼前登记。目标是观察小批次记录指令对完整交付、事实及证据的影响；这是探索性实验，不可估计总体质量或投资效果。全部 16 个运行均从 E1 冻结代码的 B 策略（完整证据缺口反馈、原生运行器）开始，使用相同 D1–D4 自编合成资料、问题、系统提示、权限、工具 schema、提交判定与运行环境。A 原策略；B 只在相同系统提示末尾追加：

> When recording facts, use small batches of at most two facts per record_facts call; after each call, inspect the result before recording more facts.

检查每次已解析 `record_facts` 的批量数量、同轮并发多次调用、完成/被动停止和已接受的提交。未接受的提交不当作交付；`reason=submitted` 且最终状态为 completed 或 partial 表示接受的主动提交，预算或截断产生的 partial 单列。四调用上限使分批可能占用提交机会，不能把完成率变化单独解释成事实质量。D3/D4 相对日合成文本仍不能充当结构化价格或自动交易日证据。依据冻结 cases 的分项与引用标准评分；语义无法核验记未知，不用模型自评替代；样本外泛化未验证。

顺序 D1-A-1、D1-B-1、D1-B-2、D1-A-2，D2、D3、D4 同序，严格串行、不自动重试、不搜索、不自动取数。每次最多 4 次模型调用、160000 核算 token、180 秒运行墙钟、每请求 90 秒和 2048 输出 token，总最多 64 次模型调用；不设采样参数，设 `FORESIGHTNINE_RESEARCH_PRESEND_RETRIES=0`。模型 `glm-5.3-flash`，Messages 原生工具；服务端版本不可锁定，实际价格未知。缓存命中与回执用量按实际记录，缓存状态可能漂移，不因此调整预算。

每个已启动运行创建排他 `.started` 标记；不得删除或重放。认证、服务配额、端点故障、未知发送/结算、进程异常或 180 秒硬超时立即停止未启动组；已返回且已结算的截断记失败但可继续，恰好 4 次调用用尽预算的正常结束可继续。若硬超时终止进程，当次在途外呼状态及费用未知，进程内此前轮次与收据也可能不可得；记录缺失，禁止重发。部分结果不得替代未知结果。不得自动接续已存在标记的批次。

记录每轮提示与 schema 合并哈希、缺口反馈、来源哈希和权限、事实、计算、claims、question_parts、报告、事件、各工具输入输出、实际调用、已报告用量、预算核算量、耗时以及接受提交的状态。两臂统一无侵入诊断：在 provider 的已解码 block 被校验/截断前记录允许的工具名、schema 中的参数字段结构、数组数量与字符串字符数、block 类型、stop reason；不存思维正文、原始 wire 数据、端点或凭据。流式路径额外保存可得分块结构及未完成 block 位置；不改变 `stream` 或注入 stream 回调。若非流式返回不完整 JSON 或在 provider 解析前失败，原始输出及其字段不可得；记录现有安全诊断，不推测内容。可得工具参数内容仅来自正常进入原生运行器的合成工具调用，不采集被屏蔽的值。

## 冻结与无外呼验收

- E1 快照 `e1-code-snapshot.tar.gz` SHA-256 `3dc6ba29005a9fe6ec4c8fe6e738f38e7a4f8fa236dd2b7f282f3801d0037a72`；逐文件清单 `e1-code-freeze.json` SHA-256 `0235d83d8f8c200c3cab5b7cb9e68c4693fab4acad68a3dd2d1816f5d49e5664`。
- `cases.json` SHA-256 `7e62e0cd385e9923ea1030b1fd9825b2fba41ade816d6601e8e57e288cdcd316`；复用构造器文件 `research-delivery-acceptance/paired.py` 当前完整文件 SHA-256 `92e3b9dc9e8e6a7a4f056b9fce3b02a257de839a4be1459aada333f686fa1d14`。与 E1 文档先前记录的构造器哈希口径不同，现固定完整文件；运行时 provider 构造及预算与 E1 一致。
- `batch.py` `19e07e903296e312190b32ad6613e1007485e93d890455567decd771652c3916`；`worker.py` `2a8217825cc5d8fc57be9c0f26b30ada0594bc06f800e162c06de898b06e60b0`；`preflight.py` `9a1aab41704cc657a1f02a068cf5b4c84d2c7f1cfdc99146787f1b4308bbcaf7`；`verify.py` `b869e84feb090ec169bf0b45b48b1e9ee34256bf3703e204d4aa1359b02eb610`。
- 使用 `foresightnine-api-1` 的 Python 虚拟环境及已有环境配置执行。非密钥配置摘要与 E1 一致，provider 配置检查通过，凭据存在但未显示；凭据仅由已授权运行环境引用，脚本不写入凭据。真实端点的可用性、服务端额度及不可变模型版本在不外呼的预检中无法确定。
- 离线假 provider 确认首轮 A/B 仅相差上述句子、工具 schema 和问题相同、搜索为零；合成截断响应验证工具参数结构可见且思维正文不入文件。当前未进行本批模型调用。

## 从仓库重建运行环境

以下准备步骤不发起模型请求；运行主体须另行获得明确启动指令。使用独立新目录，`/tmp` 仅为运行副本，不依赖历史内容；不要把凭据拷贝到文件。容器及虚拟环境名称是本机开发配置，可替换为提供相同依赖及已授权环境配置的隔离容器；变更配置时必须重新核验冻结清单，不输出敏感值。

```bash
cd /home/ubuntu/Foresightnine
stage=$(mktemp -d /tmp/fn-fact-batch.XXXXXX)
tar -xzf src/docs/product/research-quality-acceptance/e1-code-snapshot.tar.gz -C "$stage"
cp src/docs/product/research-quality-acceptance/cases.json src/docs/product/research-quality-acceptance/e1-code-freeze.json "$stage/"
cp src/docs/product/research-delivery-acceptance/paired.py "$stage/prior.py"
cp src/docs/product/research-quality-acceptance/fact-batching/{batch,worker,preflight,verify}.py "$stage/"
sudo -n docker exec foresightnine-api-1 mkdir /tmp/fn-fact-batch
sudo -n docker cp "$stage/." foresightnine-api-1:/tmp/fn-fact-batch/
sudo -n docker exec -e PYTHONPATH=/tmp/fn-fact-batch/apps/api foresightnine-api-1 /app/apps/api/.venv/bin/python /tmp/fn-fact-batch/verify.py
sudo -n docker exec -e PYTHONPATH=/tmp/fn-fact-batch/apps/api foresightnine-api-1 /app/apps/api/.venv/bin/python /tmp/fn-fact-batch/preflight.py
```

上述副本须先按本登记所列 SHA-256 对脚本和归档核验；`verify.py` 对快照逐文件、资料、构造器、模型、协议、授权能力及非密钥配置摘要做检查。`preflight.py` 使用离线假 provider；通过后仍须确认无其他模型批次且环境稳定。经明确启动授权后，在容器中执行下列命令并在结束或停批后复制完整 `results`（含标记）；保持新目录原样以支持审计。

```bash
sudo -n docker exec -e PYTHONPATH=/tmp/fn-fact-batch/apps/api foresightnine-api-1 /app/apps/api/.venv/bin/python /tmp/fn-fact-batch/batch.py
sudo -n docker cp foresightnine-api-1:/tmp/fn-fact-batch/results src/docs/product/research-quality-acceptance/fact-batching/
```
