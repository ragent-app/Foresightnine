# 离线选段回放

三套数据共用本目录的七个回放脚本。用 `--data-dir` 指定本目录、`holdout2` 或 `holdout3`。入口将所选数据和共享脚本放入临时目录，按该数据集的 `freeze.json` 校验摘要，再执行原脚本。命令行中的输入、输出路径仍相对于调用者的工作目录。

例如，从本目录对第三套数据的基线轨迹重新评分，结果写到临时文件：

```bash
python run_replay.py --data-dir holdout3 grade.py holdout3/baseline-trace.json /tmp/holdout3-grade.json
```

检索与模型输入回放使用 API 项目的 Python 环境。基线代码和契约取自 `82900b4`，将其 `src/apps/api` 设为 `PYTHONPATH`。候选回放在独立的基线副本上覆盖相应的 `candidate-v1/` 或 `candidate-final/` 中的模块。脚本参数（含检索对照的 `--repeat-query`）直接跟在脚本名后。实验条件见[冻结协议](protocol.md)与[基线记录](baseline.md)。

原始目录布局、两套留存数据旁的脚本副本以及全部实验记录可从归档提交 `3008858` 取回。
