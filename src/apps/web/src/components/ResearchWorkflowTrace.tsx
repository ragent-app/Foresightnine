/** Saved execution receipts are operational history, not evidence validation. */
interface Props { value: unknown; en: boolean }

type Receipt = { stage: string; status: string; reason?: string | null };
const stages: Record<string, readonly [string, string]> = {
  planning: ["Planning", "规划"], source_discovery: ["Source discovery", "来源发现"],
  source_retrieval: ["Source retrieval", "资料获取"], analysis: ["Analysis", "研究分析"],
  counterpoint: ["Counterpoint", "反方补充"], gap_followup: ["Evidence follow-up", "证据补充"],
  gap_search: ["Gap search", "缺口搜索"], support_review: ["Evidence review", "证据复查"],
  delivery_assessment: ["Delivery assessment", "交付评估"], coordination: ["Research coordination", "研究协调"],
  plan_suggestions: ["Plan suggestions", "规划建议"],
};
const statuses: Record<string, readonly [string, string]> = {
  selected: ["Selected", "已选择"], observed: ["Recorded", "已记录"], in_progress: ["In progress when saved", "保存时进行中"],
  completed: ["Completed", "已完成"], complete: ["Completed", "已完成"], partial: ["Partial", "部分完成"],
  failed: ["Failed", "失败"], denied: ["Not authorized", "未获授权"], unavailable: ["Unavailable", "不可用"],
  not_requested: ["Not requested", "未请求"], not_needed: ["Not needed", "无需执行"],
  not_started: ["Not started", "未开始"], budget_exhausted: ["Budget exhausted", "预算已耗尽"],
  cost_unknown: ["Cost unknown", "费用未知"], input_budget: ["Input limit reached", "输入超限"],
  no_evidence: ["No evidence retained", "未保留证据"], needs_review: ["Needs review", "需要复核"],
  no_eligible_source: ["No eligible source", "无符合条件的来源"], no_gaps: ["No recorded gaps", "无已记录缺口"],
  stopped: ["Stopped", "已停止"],
};
const reasons: Record<string, readonly [string, string]> = {
  goal_or_available_steps_complete: ["Goal or available steps complete", "目标或可执行步骤已完成"],
  remaining_gaps: ["Unresolved gaps remain", "仍有未解决的缺口"], analysis_unavailable: ["Analysis unavailable", "研究分析不可用"],
  action_limit: ["Action limit reached", "已达到行动上限"], required_capability_unavailable: ["Required capability unavailable", "所需能力不可用"],
  "Existing sources or task permissions do not require discovery.": ["Discovery was not needed or authorized", "已有资料或任务授权不需要发现来源"],
  "No authorized eligible candidate is available for retrieval.": ["No authorized candidate is available", "没有可获取的授权候选资料"],
  "The task did not authorize this optional action.": ["This optional action was not requested", "未请求此可选步骤"],
  "The preceding analysis did not produce usable claims.": ["No usable findings from the preceding analysis", "此前分析没有可用结论"],
  "The current artifacts do not require this action.": ["Current research does not require this action", "当前研究不需要此步骤"],
  "The run time budget is exhausted.": ["Time budget exhausted", "时间预算已耗尽"],
  "The model call budget is exhausted.": ["Model call budget exhausted", "模型调用预算已耗尽"],
  "The action cost cannot be bounded by the requested limit.": ["Cost cannot be bounded by the requested limit", "无法确认费用在所设上限内"],
  "The capability is not registered or configured.": ["Capability is not available", "所需能力不可用"],
};

export function ResearchWorkflowTrace({ value, en }: Props) {
  if (!Array.isArray(value)) return null;
  const rows: Receipt[] = value.filter((item): item is Receipt =>
    !!item && typeof item === "object" && typeof item.stage === "string" && typeof item.status === "string");
  if (!rows.length) return null;
  const lang = en ? 0 : 1;
  return <details className="general-source"><summary>{en ? "Execution details" : "执行详情"}</summary>
    <p>{en ? "Recorded when this version was saved; execution does not verify findings." : "以下为保存本版本时的执行记录，不代表研究结论已核验。"}</p>
    <ol>{rows.map((row, index) => <li key={`${row.stage}:${index}`}>
      {stages[row.stage]?.[lang] ?? row.stage}: {statuses[row.status]?.[lang] ?? row.status}
      {row.reason && reasons[row.reason] ? <> · {reasons[row.reason]?.[lang]}</> : null}
    </li>)}</ol>
  </details>;
}
