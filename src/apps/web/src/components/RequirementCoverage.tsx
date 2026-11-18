interface Props { value: unknown; en: boolean }

export function RequirementCoverage({ value, en }: Props) {
  if (!value || typeof value !== "object" || !("requirements" in value) || !Array.isArray(value.requirements)) return null;
  return <details className="general-source"><summary>{en ? "Recorded task links" : "已记录的任务关联"}</summary>
    <p>{en ? "Planning dimensions and links are model suggestions. A link does not establish that evidence answers the task." : "规划维度和关联为模型建议；关联不证明证据已回答任务。"}</p>
    <ul>{value.requirements.map((row: unknown, index: number) => {
      if (!row || typeof row !== "object" || !("requirement_id" in row) || typeof row.requirement_id !== "string") return null;
      const record = row as Record<string, unknown>;
      const dimensions = record.dimensions && typeof record.dimensions === "object" ? record.dimensions as Record<string, unknown> : {};
      const list = (name: string) => Array.isArray(record[name]) ? (record[name] as unknown[]).join(", ") : "";
      const labels: Record<string, [string, string]> = { entities: ["Entities", "对象"], metrics: ["Metrics", "指标"], periods: ["Periods", "期间"], units: ["Units", "单位"], qualifiers: ["Context", "口径"] };
      const gapLabels: Record<string, [string, string]> = { no_recorded_finding: ["no linked finding", "未关联结论"], no_recorded_source: ["no linked source", "未关联来源"], no_recorded_counterpoint_relation: ["no linked counterargument", "未关联反方"], suggested_requirement_without_evidence_link: ["suggested requirement lacks linked evidence", "建议的要求尚无关联证据"], suggested_calculation_not_linked: ["suggested calculation is not linked", "建议的计算尚未关联"], calculation_input_unit_mismatch: ["calculation input unit differs", "计算输入单位不一致"], calculation_input_period_mismatch: ["calculation input period differs", "计算输入期间不一致"], calculation_input_currency_mismatch: ["calculation input currency differs", "计算输入币种不一致"], fact_unit_conflict: ["fact units differ", "事实单位存在分歧"], fact_currency_conflict: ["fact currencies differ", "事实币种存在分歧"], fact_value_conflict: ["fact values differ; check revisions and context", "事实数值有分歧，请核对修订与口径"], request_decomposition_not_verified: ["request coverage not established", "原始任务覆盖尚未确认"] };
      const gaps = Array.isArray(record.gaps) ? record.gaps.filter((item): item is string => typeof item === "string") : [];
      return <li key={`${row.requirement_id}-${index}`}><strong>{row.requirement_id}</strong>
        {Object.entries(dimensions).map(([name, values]) => Array.isArray(values) && values.length ? <span key={name}> · {labels[name]?.[en ? 0 : 1] ?? name}: {values.join(", ")}</span> : null)}
        {list("retrieval_hints") ? <span> · {en ? "suggested retrieval hints (not evidence)" : "建议检索线索（不是证据）"}: {list("retrieval_hints")}</span> : null}
        {list("source_ids") ? <span> · {en ? "sources" : "来源"}: {list("source_ids")}</span> : null}
        {list("candidate_fact_ids") ? <span> · {en ? "fact links" : "事实关联"}: {list("candidate_fact_ids")}</span> : null}
        {list("candidate_calculation_indexes") ? <span> · {en ? "calculation indexes" : "计算索引"}: {list("candidate_calculation_indexes")}</span> : null}
        {gaps.length ? <span> · {en ? "recorded gaps" : "已记录缺口"}: {gaps.map((gap) => gapLabels[gap]?.[en ? 0 : 1] ?? (en ? "needs review" : "需复核")).join(", ")}</span> : null}
        <span> · {en ? "semantic support not independently verified" : "语义支持尚未独立核验"}</span></li>;
    })}</ul>
  </details>;
}
