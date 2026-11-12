import type { ResearchV2 } from "@foresightnine/contracts";

// A text report is a view of the server-authorized export, not a new export
// permission boundary. All untrusted text stays plain text and is confined to
// one labeled line so embedded line breaks cannot impersonate report fields.
function scalar(value: unknown, missing: string): string {
  if (value === undefined || value === null || value === "") return missing;
  if (typeof value === "string") return value.replace(/[\u0000-\u001f\u007f]/g, (c) => c === "\n" ? "\\n" : c === "\r" ? "\\r" : c === "\t" ? "\\t" : " ");
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

export function formatResearchTextReport(data: ResearchV2.ResearchFullExport, en: boolean): string {
  const missing = en ? "not recorded" : "未记录";
  const label = (english: string, chinese: string) => en ? english : chinese;
  const lines: string[] = [];
  const add = (name: string, value: unknown) => lines.push(`${name}: ${scalar(value, missing)}`);
  const section = (name: string) => lines.push("", `== ${name} ==`);
  const items = (value: unknown): readonly Readonly<Record<string, unknown>>[] => Array.isArray(value)
    ? value.filter((item): item is Readonly<Record<string, unknown>> => Boolean(item) && typeof item === "object" && !Array.isArray(item)) : [];
  const empty = (count: number) => { if (!count) lines.push(label("None recorded.", "未记录。")); };
  const v = data.version, c = v.content;
  lines.push(label("ForesightNine research report", "ForesightNine 研究报告"));
  add(label("Topic question", "主题原问题"), data.request.question);
  if (typeof c.run?.question_override === "string") add(label("Question for this version", "本版本的研究问题"), c.run.question_override);
  add(label("Scope", "范围"), data.request.scope);
  add(label("Research cutoff", "研究截止时间"), data.request.as_of);
  add(label("Topic ID", "主题标识"), data.topic_id);
  add(label("Version ID", "版本标识"), v.version_id);
  add(label("Parent version", "上一版本"), v.parent_version_id);
  add(label("Version created", "版本创建时间"), v.created_at);
  add(label("Exported", "导出时间"), data.generated_at);
  add(label("Review state", "复核状态"), v.review);
  add(label("Version author", "版本创建方"), v.created_by);
  add(label("Version note", "版本备注"), v.note);
  add(label("Last run state (not a new review)", "上次运行状态（不代表新的复核）"), c.run?.status);
  lines.push(label("Model interpretations and declared source permissions are not independently verified.", "模型解释及声明的资料权限尚未独立核验。"));
  lines.push(label("Calculations use recorded inputs; source precision and prior rounding are not established.", "计算使用已记录的输入；来源精度及此前舍入情况尚未确认。"));
  if (c.summary) { section(label("Summary", "摘要")); add(label("Summary", "摘要"), c.summary); }
  section(label("Findings and counterarguments", "结论与反方"));
  empty(items(c.claims).length);
  items(c.claims).forEach((claim, i) => {
    lines.push(`\n${label("Finding", "条目")} ${i + 1}`);
    for (const [key, a, b] of [["role", "Role", "类型"], ["text", "Text", "内容"], ["authorship", "Authorship", "作者类型"], ["kind", "Kind", "内容类型"], ["verification", "Verification", "核验状态"], ["support_status", "Support status", "支持状态"], ["support_review", "Model review (not independent)", "模型复查（非独立核验）"]] as const) add(label(a,b), claim[key]);
    for (const citation of items(claim.citations)) {
      add(label("Citation source", "引文来源"), citation.source_id);
      add(label("Quotation", "引文"), citation.quotation);
      add(label("Location", "位置"), citation.location);
      add(label("Quotation status", "引文状态"), {match: citation.quotation_match ?? null, partial_token: citation.partial_token ?? false, support: citation.support_status ?? null});
    }
    for (const key of ["causal_attribution_unverified", "uncited_exact_dates", "publication_date_warnings", "rate_unit_warnings", "uncited_amounts", "uncited_claim_amounts", "uncited_dollar_amounts", "uncited_quarters", "balance_direction_warnings", "uncited_percentages", "uncited_percentage_bounds", "reused_percentages", "uncited_basis_points", "uncited_percentage_points", "uncited_fact_values", "citation_context_warnings", "suggested_fact_evidence", "suggested_related_quotations", "suggested_calculations"]) if (claim[key] !== undefined) add(key, claim[key]);
  });
  section(label("Extracted figures", "提取的数值"));
  empty(items(c.facts).length);
  items(c.facts).forEach((fact, i) => {
    lines.push(`\n${label("Figure", "数值")} ${i + 1}`);
    for (const [key, a, b] of [["fact_id","ID","标识"],["label","Metric","指标"],["value","Value","数值"],["unit","Unit","单位"],["currency","Currency","币种"],["period","Period","期间"],["verification","Verification","核验状态"],["authorship","Authorship","作者类型"],["source_id","Source ID","来源标识"],["quotation","Quotation","引文"],["quotation_match","Quotation match","引文定位方式"],["location","Location","位置"]] as const) add(label(a,b), fact[key]);
    for (const key of ["currency_context_unverified", "currency_quote_missing", "table_alignment_unverified", "table_period_conflict", "table_structure_alignment", "table_confirmation", "currency_conflict_unverified", "direction_context_unverified", "direction_confirmation", "source_value_warnings", "numeric_normalization", "range_parent_id"]) if (fact[key] !== undefined) add(key, fact[key]);
  });
  section(label("Calculations", "计算"));
  empty(items(c.calculations).length);
  items(c.calculations).forEach((calculation, i) => {
    lines.push(`\n${label("Calculation", "计算")} ${i + 1}`);
    // Preserve every stored input, formula, precision and status, including
    // fields introduced by later compatible schema versions.
    for (const [key, value] of Object.entries(calculation)) add(key, value);
  });
  section(label("Sources and provenance", "来源与追溯信息"));
  empty(data.source_provenance.length);
  data.source_provenance.forEach((source, i) => {
    lines.push(`\n${label("Source", "来源")} ${i + 1}`);
    for (const [key, value] of Object.entries(source)) add(key, value);
  });
  if (c.run && Object.keys(c.run).length) {
    section(label("Recorded run", "已记录的运行"));
    for (const [key, value] of Object.entries(c.run)) add(key, value);
  }
  section(label("Limitations and outstanding checks", "局限与待核查项"));
  if (!c.limitations?.length) lines.push(label("None recorded.", "未记录。"));
  c.limitations?.forEach((value, i) => add(String(i + 1), value));
  for (const [key, values] of [["evidence_differences", c.evidence_differences], ["calculation_differences", c.calculation_differences], ["amount_differences", c.amount_differences], ["source_candidates", c.source_candidates]] as const) {
    if (values?.length) { section(key); values.forEach((value, i) => add(String(i + 1), value)); }
  }
  lines.push("", label("For the complete structured record, export the research JSON separately.", "完整结构化记录可另行导出研究 JSON。"));
  return lines.join("\n") + "\n";
}
