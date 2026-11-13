interface Props { value: unknown; en: boolean }

type Cue = { code: string; indexes?: number[]; index_kind?: string; count?: number; truncated?: boolean };

const labels: Record<string, readonly [string, string]> = {
  no_analyzed_sources: ["No source was analyzed in the last run.", "上次运行没有已分析的来源。"],
  no_findings: ["No finding was retained.", "没有保留研究结论。"],
  citations_missing_or_incomplete: ["Findings have missing or incomplete citations.", "部分研究条目缺少完整引文。"],
  evidence_warnings: ["Evidence warnings require review.", "部分研究条目存在证据警示，需要核查。"],
  no_counterpoint: ["A counterargument was requested but none was retained.", "本次要求反方，但没有保留反方条目。"],
  counterpoint_target_unlinked: ["Counterarguments have no recorded or suggested finding to review against.", "部分反方未关联或建议关联具体结论。"],
  calculation_inputs_unverified: ["Calculation inputs are missing, changed or not verified.", "部分计算的输入缺失、变更或尚未核查。"],
  calculation_or_amount_differences: ["Potential differences between findings, amounts or calculations require review.", "结论、金额或计算存在待核查差异。"],
  model_review_incomplete: ["The requested model review did not finish for all findings.", "请求的模型复查未覆盖全部条目。"],
  counterpoint_followup_incomplete: ["The requested counterargument follow-up did not complete.", "请求的反方补充未完成。"],
  last_run_partial: ["The last run returned partial results; review its limitations.", "上次运行部分完成，请查看其限制和缺口。"],
};

export function ResearchChecklist({ value, en }: Props) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const data = value as { schema_version?: string; items?: unknown };
  if (data.schema_version !== "1" || !Array.isArray(data.items)) return null;
  const items = (data.items as Cue[]).filter((item) => item && typeof item.code === "string" && labels[item.code]);
  return <section className="general-source">
    <h3>{en ? "Review checklist" : "研究待核查项"}</h3>
    <p>{en ? "Generated from the saved record. An empty checklist does not verify the findings, prove evidence support or rule out other gaps." : "根据已保存记录生成。待办为空不代表结论已核实、证据充分或不存在其他缺口。"}</p>
    {items.length ? <ul>{items.map((item) => <li key={item.code}>{labels[item.code][en ? 0 : 1]}
      {Array.isArray(item.indexes) && item.indexes.length ? <span> {item.index_kind === "calculation" ? (en ? "Calculations" : "计算") : (en ? "Items" : "研究条目")} {item.indexes.filter(Number.isInteger).map((i) => i + 1).join("、")}{item.truncated ? (en ? " (additional items omitted)" : "（还有未列出的条目）") : ""}.</span> : null}
    </li>)}</ul> : <p>{en ? "No recorded checklist items." : "没有已记录的待核查项。"}</p>}
  </section>;
}
