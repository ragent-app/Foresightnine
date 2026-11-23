# 选择器合成压力检查

基于本地第二版选择器，对 1,000 条合成候选测量；不是质量验收、真实研究或留存集。运行命令：`PYTHONPATH=src/apps/api src/apps/api/.venv/bin/python src/docs/product/context-evidence-acceptance/selection/stress-2026-11-23/run.py`。原始单次结果见 `results.txt`；测时受并发及环境影响，不是性能保证。Python `tracemalloc` 的峰值仅覆盖受跟踪分配，不是进程总内存。

本次 K=2、K=4 都保留 30/1000 个候选；无跟踪分别 0.489、0.296 秒，跟踪时分别 1.198、1.157 秒，峰值约 368、486 KB。人工指定一项必要材料位于候选 999 的对抗样本中，该项被裁掉（1/1）；因此剪枝不保证保留必要证据，也不能以角色覆盖冒充已核验。9 条小池 K=4、宽度 32 的结果与宽度 1000（足以包含所有 4 段组合）的结果一致，仅表明这个小样本无差异，不证明全局最优。

基线核对：独立 `git archive 82900b4` 中运行 `test_reconciliation_appended_to_record_facts_and_gap_and_view_at_submission`，同样返回 partial 而非预期 completed。当前选择相关单测见 `test_research_library_selection.py`；组合模式尚未通过正式留存门槛，产品保持基线默认。
