# 旧失败行为对照

基线原始记录见 [verification.json](../../context-evidence-acceptance/verification.json)。本次以本地 mock 身份与离线网络重跑，旧协调器请求已不被当前 API 接受，旧 `analyze` 草稿不符合当前输出契约；不将这些旧用例直接删除。运行日志保留于本目录。

| 旧用例 | 用户行为 | 当前入口或证据 | 结果 |
| --- | --- | --- | --- |
| `test_optional_planning_hints_guide_existing_retrieval_without_changing_question` | 规划提示不改变问题并辅助检索 | `runtime search_source / read_sources` | 旧协调规划路径退出；检索及原问题测试存在，提示等价行为未单独验收 |
| `test_acquisition_can_choose_delivery_with_unread_ranges` | 未读资料时可以交付 | `runtime partial_submission_keeps_report_empty_and_preserves_partial_notes_across_recovery` | 通过；不再有强制 acquisition_decision |
| `test_api_delegated_two_reads_use_the_selected_source` | 选中来源的两次阅读 | `runtime parallel_read_sources_turn_budget_and_resumed_tool_batch_enforcement` | 通过；原 delegated 接口退役 |
| `test_assertion_links_recovery_edit_and_export_permissions` | 引用链接、编辑、导出权限 | `runtime checkpointed_question_parts_survive_interruption_and_reopen_without_leaking_revoked_evidence` | 通过当前撤权重开、导出隐私及版本引用；旧 assertion 索引结构已退役 |
| `test_bad_envelope_repair_is_separate_checkpointed_call[False]` | 坏响应修复调用可恢复（不打断） | `runtime failed_attempts_and_partial_notes_recorded_across_truncation_context_incomplete_and_no_submission` | 部分：失败与部分结果通过；旧自动修复步骤退役 |
| `test_bad_envelope_repair_is_separate_checkpointed_call[True]` | 坏响应修复调用可恢复（打断） | `runtime tool_batch_cursor_resumes_without_model_replay` | 部分：工具游标通过；旧修复阶段退役 |
| `test_checkpoint_is_account_scoped_and_user_revision_blocks_continuation` | 账户隔离、修改后阻止恢复 | `runtime_api partial_continue_respects_cumulative_budget_and_unknown_blocks` | 通过未知调用、预算、跨账户隔离及修改后拒绝恢复 |
| `test_direction_proof_rejected_when_forged_and_invalidated_by_source_or_location_edits` | 方向证明不可伪造、修改后失效 | `versions API 严格事实字段和重新校验` | 未等价验收；旧方向证明已退役，不声称语义方向得到证明 |
| `test_private_validation_diagnostics_are_bounded_and_not_exported[analysis_schema]` | 诊断有界且不导出：分析 schema | `runtime failed_attempts_and_partial_notes_recorded_across_truncation_context_incomplete_and_no_submission` | 通过失败记录及私有错误输入不进入导出 |
| `test_private_validation_diagnostics_are_bounded_and_not_exported[provider_parse]` | 诊断有界且不导出：provider parse | `同上` | 当前失败诊断导出隐私通过；旧审查阶段已退役 |
| `test_private_validation_diagnostics_are_bounded_and_not_exported[review_index]` | 诊断有界且不导出：审查索引 | `旧独立审查阶段已退役` | 旧独立审查阶段已退役；当前错误诊断导出隐私通过 |
| `test_private_validation_diagnostics_are_bounded_and_not_exported[review_schema]` | 诊断有界且不导出：审查 schema | `旧独立审查阶段已退役` | 旧独立审查阶段已退役；当前错误诊断导出隐私通过 |
| `test_repeated_continuation_preserves_committed_stages_and_original_budget` | 重复续跑保留成果与原预算 | `runtime_api partial_continue_respects_cumulative_budget_and_unknown_blocks；completed_submission_recovers_without_another_model_call` | 通过；旧分阶段执行顺序不再适用 |
| `test_review_settlement_is_separate_from_result_validation[invalid_schema]` | 审查结果无效也结算 | `recovery_acceptance model_call_receipts_separate_response_settlement_and_stage_validation` | 通过回执单元用例；旧审查专用阶段退役 |
| `test_review_settlement_is_separate_from_result_validation[timeout]` | 审查超时也记录结算状态 | `同上；runtime provider_timeout_produces_dedicated_gap_code_and_unknown_settlement` | 通过；未知结算与已结算区分 |
| `test_settlement_is_account_scoped_and_separates_ledger_from_provider_report` | 结算归原账户，区分服务商报告 | `runtime_api returned_response_settles_original_account_after_continuation` | 通过原账户结算；账本与报告粒度未单独重测 |
| `test_structured_all_invalid_discloses_no_valid_research_result` | 全部无效时不声称有成果 | `runtime failed_attempts_and_partial_notes_recorded_across_truncation_context_incomplete_and_no_submission` | 部分：未提交无完整报告；旧结构化分析契约退役 |
| `test_structured_partial_survives_recovery_and_export_without_rejected_content` | 有效部分恢复导出且排除无效内容 | `runtime mixed_facts_checkpoint_replay_preserves_valid_entries_and_exact_source_spans` | 通过恢复与有效事实；当前导出隐私及事实版本重开通过 |
| `test_truncated_analysis_splits_into_distinct_checkpointed_calls` | 截断调用与检查点 | `recovery_acceptance repeated_truncation_circuit_breaker_blocks_recovery_status_and_continue` | 部分：截断熔断通过；旧分拆阶段退役 |
| `test_unresolved_external_call_blocks_replay_but_preserves_partial_delivery` | 未知外呼防重复并保留部分结果 | `runtime unknown_external_result_is_not_replayed；runtime_api partial_continue_respects_cumulative_budget_and_unknown_blocks` | 通过防重及预算；部分交付由 runtime partial_submission 测试覆盖 |
| `test_analysis_keeps_structural_calculation_provisional_and_fails_closed` | 标签一致性不等于真实性 | `runtime_xbrl_debt current_record_facts_checks_xbrl_and_keeps_source_truth_unverified` | 通过新工具路径；仍需真实发行人回放 |
| `test_inline_consistency_never_uses_generic_match_to_bypass_known_metric_mapping` | 单元格与原始 XBRL 对照 | `runtime_xbrl_debt current_record_facts_checks_xbrl_and_keeps_source_truth_unverified` | 通过整行及单元格引用；表头需已读 |
| `test_inline_consistency_supports_ifrs_currency_and_exact_other_row` | 命名空间、上下文、单位、主体冲突关闭 | `runtime_xbrl_debt current_record_facts_fails_closed_on_xbrl_conflicts` | 通过合成变体；覆盖主体及期间重叠 |
| `test_single_cell_citations_can_be_cross_checked_against_original_xbrl` | IFRS 币种及精确指标 | `runtime_xbrl_debt current_record_facts_supports_ifrs_and_rejects_wrong_taxonomy` | 通过合成 EUR 样本 |
| `test_xbrl_metadata_fails_closed_for_namespace_context_units_and_entity_conflicts` | 已知指标不能退化为泛匹配 | `runtime_xbrl_debt current_record_facts_fails_closed_on_xbrl_conflicts` | 通过错误指标变体 |
| `test_xbrl_tags_can_confirm_source_consistency_but_not_source_truth` | 只有表格结构时计算暂定 | `runtime_xbrl_debt current_record_facts_requires_read_headers` | 通过未读表头及无 XBRL 表格关闭匹配 |

## 运行与界限

- `legacy-rerun.log`：原失败组整体重跑，26 项仍失败；API 旧字段被严格拒绝，旧表格用例在已退役草稿契约报错。
- `current-safety.log`：当前运行/恢复替代路径 28 项通过。结合 current-api-acceptance.log 覆盖跨账户、修改后续跑和导出隐私。
- `broad-rerun.log`：更宽组合含 6 项旧表格失败和额外 6 项 runtime API 失败（行情、SEC、部分交付）；17 项额外失败均在同配置干净 82900b4 复现，见 extra-baseline-82900b4.log，整组仍未通过。
- 新 XBRL 仅在已读表格行及表头可定位时签发与事实绑定的来源一致性结果；它不验证来源真实性、会计语义或 PDF 表格。限制为每批最多追踪两个表格行，超限保持待核。
- 测试为合成及本地 provider，未调用真实模型，也未进行人工或真实发行人文件验收。

- `xbrl-current.log`：当前 XBRL 及用户注入证明防伪共 14 项通过；`current-regression.log` 为运行时、财报提取与 XBRL 的整组通过结果。
- `topics-rerun.log`：全组另有 11 项失败，涉及旧差值警告及 merge_turn 公式轮转，同配置干净 82900b4 上的对应 11 项均失败；不能把它计为整组通过。

命令均在 `src/apps/api` 下运行，使用 `.venv/bin/pytest -q -p no:cacheprovider`，环境变量为 `FORESIGHTNINE_LOCAL_AUTH_MODE=mock FORESIGHTNINE_OFFLINE_TESTS=1`。原失败组文件为 `test_research_context.py test_research_context_capacity.py test_research_recovery_acceptance.py test_research_table_provenance.py test_research_statement_extraction.py`；宽组文件为 `test_research_runtime.py test_research_runtime_api.py test_research_runtime_xbrl_debt.py test_research_statement_extraction.py test_research_table_provenance.py`；当前运行时回归为 `test_research_runtime_xbrl_debt.py test_research_runtime.py test_research_statement_extraction.py`。各日志记录失败明细。

- `current-api-acceptance.log`：15 项通过，包括原表与单元格核对、伪造证明、跨账户隔离、用户修改阻止恢复、私有诊断不导出，以及 XBRL 事实版本重开、导出和编辑证明失效。该验收是本地合成资料，并未验证所有语义冲突。
