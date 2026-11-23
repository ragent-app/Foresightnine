# 计算契约集中验收

2026-11-23，按 preregistration.md 冻结口径离线执行，无模型和外部服务调用。运行 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src/apps/api src/apps/api/.venv/bin/python src/docs/product/research-quality-acceptance/calculation-contract/replay.py`；原始请求、文件哈希、逐项实际结果、反例错误及 Messages 线协议定义见 results.json。每次生成的计算 ID 随机，不影响比较。

四条真实第三轮调用 4/4 接受，共 11 项；收入增速均为 20%，EPS 相对增速均为 50%，后三批差额均为 1 USD per share。逐项核对有序事实 ID、原文定位、公式和数量，未运行第四轮。A1 已有的第四轮成功回执与新回放的公式、值、单位、币种、有序输入一致；四批分别以单独二元写法和数组写法再次运行，结果一致。模型可见定义实际经 Messages provider 的 `_wire` 序列化，只有必填非空数组 `fact_ids`；旧二元写法仍由应用输入校验接受。原模型输入保留于调用参数，归一化不修改输入审计；计算记录沿用已保存的输入事实 ID。按工具 handler 的本地批次入口检查无其他应用内直接调用点；工具注册、调度通过 registry/TOOL_HANDLERS，此外仅有测试直接调用。未对重试、防重和外部服务作新的端到端验证。

冲突、逆序、半对、缺失事实、来源版本变更、来源缺失、原文改变、混合无效项和无效公式参数均返回错误且批次落库数为零。原来源的权限、时点、原文和版本检查及既有公式和计算精度继续执行。

相关集中回归：`tests/test_research_runtime_s0b.py tests/test_research_calculations.py tests/test_calculation_provenance.py tests/test_research_returns.py tests/test_anthropic_messages_provider.py`：114 通过、3 失败。相同三项在修改前 HEAD 独立归档中复现：两项 market_data 重复获取测试的工具消息索引越界，以及 intraday 测试夹具缺少 `as_of`。这些失败没有计为通过，也未作为本次变更造成；无关夹具不在此修改。首次测试命令引用不存在的 `test_anthropic_messages.py`，未执行测试；随后已改用实际文件。
