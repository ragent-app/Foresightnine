import type { ResearchFact } from "../general-research-client";

type CitationLink = { citation_index: number; same_passage_fact_ids: string[] };
type ClaimLinks = { claim_index: number; citations: CitationLink[]; citation_location_status?: string;
  semantic_support_status?: string; model_review_opinion?: string | null;
  counterpoint_relevance_opinion?: string | null; counterpoint_link_status?: string;
  candidate_calculation_indexes?: number[]; assertion_coverage?: string; assertions?: { text: string; citation_indexes: number[]; calculation_indexes: number[]; link_status: string }[] };
type CalculationLink = { calculation_index: number; inputs: { fact_id: string | null; fact_reference: string }[] };
type EvidenceLinks = { schema_version: string; truncated: boolean; claims: ClaimLinks[]; calculations: CalculationLink[]; facts?: {fact_id: string | null; reference_kind?: string; record_locator?: Record<string, unknown>}[] };

interface Props {
  value: unknown;
  claimIndex: number;
  facts: ResearchFact[];
  calculationCount: number;
  en: boolean;
}

// The map is server-generated for a saved version. It only links recorded
// locations and explicit inputs; it is not a semantic-support judgment.
export function ResearchEvidenceLinks({ value, claimIndex, facts, calculationCount, en }: Props) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const map = value as Partial<EvidenceLinks>;
  if (map.schema_version !== "1" || !Array.isArray(map.claims) || !Array.isArray(map.calculations)) return null;
  const entry = map.claims.find((item) => item && item.claim_index === claimIndex);
  if (!entry || !Array.isArray(entry.citations)) return null;
  const ids = new Set(entry.citations.flatMap((link) => Array.isArray(link?.same_passage_fact_ids)
    ? link.same_passage_fact_ids.filter((id): id is string => typeof id === "string") : []));
  const matches = facts.filter((fact) => ids.has(fact.fact_id));
  if (!matches.length && !map.truncated && !entry.semantic_support_status && !entry.citation_location_status) return null;
  const related = map.calculations.filter((calc) => calc && Number.isInteger(calc.calculation_index) &&
    calc.calculation_index >= 0 && calc.calculation_index < calculationCount && Array.isArray(calc.inputs) &&
    calc.inputs.some((input) => input?.fact_reference === "unique" && typeof input.fact_id === "string" && ids.has(input.fact_id)));
  return <details className="general-source">
    <summary>{en ? "Recorded evidence links" : "已记录的证据关联"}</summary>
    {entry.citation_location_status ? <p>{en ? "Quotation location" : "引文定位"}: {({
      all_recorded: en ? "all recorded" : "均有记录", partially_recorded: en ? "partially recorded" : "部分有记录",
      unknown_or_unlocated: en ? "unknown or unlocated" : "未知或未定位", not_provided: en ? "not provided" : "未提供",
    } as Record<string, string>)[entry.citation_location_status] ?? (en ? "unknown" : "未知")} · {en ? "Semantic support has not been independently verified." : "语义支持尚未独立核验。"}</p> : null}
    {Array.isArray(entry.assertions) && entry.assertions.length ? <><p>{en ? "Suggested parts of this finding; neither decomposition nor support has been independently verified." : "以下为模型建议的结论分项；分解是否完整及各项支持均未独立核验。"}</p><ul>{entry.assertions.map((part, index) => <li key={index}>{part.text} · {part.link_status === "linked_suggestion" ? (en ? "suggested references" : "建议关联") : part.link_status === "invalid_or_unlocated" ? (en ? "invalid or unlocated references" : "关联无效或未定位") : (en ? "no recorded reference" : "未记录关联")}{part.citation_indexes.length ? ` · ${en ? "citations" : "引文"}: ${part.citation_indexes.map((n) => n + 1).join(", ")}` : ""}{part.calculation_indexes.length ? ` · ${en ? "calculations" : "计算"}: ${part.calculation_indexes.map((n) => n + 1).join(", ")}` : ""}</li>)}</ul></> : <p>{en ? "No claim-level breakdown was recorded; completeness is unknown." : "未记录逐项分解；结论覆盖是否完整未知。"}</p>}
    {entry.model_review_opinion ? <p>{en ? "Recorded model opinion" : "已记录的模型意见"}: {({
      supports: en ? "supports" : "认为支持", does_not_support: en ? "does not support" : "认为不支持",
      contradicts: en ? "contradicts" : "认为矛盾", uncertain: en ? "uncertain" : "不确定",
    } as Record<string, string>)[entry.model_review_opinion] ?? (en ? "unknown" : "未知")} {en ? "(not independent verification)" : "（不构成独立核验）"}</p> : null}
    {entry.counterpoint_link_status && entry.counterpoint_link_status !== "not_applicable" ? <p>{en ? "Counterpoint relationship" : "反方关联"}: {({
      user_recorded: en ? "user recorded" : "用户记录", model_suggested: en ? "model suggested" : "模型建议", unlinked: en ? "not linked" : "未关联",
    } as Record<string, string>)[entry.counterpoint_link_status] ?? (en ? "unknown" : "未知")}</p> : null}
    {Array.isArray(map.facts) && map.facts.some((fact) => fact?.fact_id && ids.has(fact.fact_id) && fact.reference_kind === "structured_query_record") ? <p>{en ? "Some figures reference structured query records and stored-snapshot offsets, not an original document passage. Verify record identity, period and units." : "部分数值指向结构化查询记录及保存快照的位置，并非原始文件引文；请核对记录身份、期间和单位。"}</p> : null}
    {matches.length ? <p>{en ? "These figures share an exact recorded source passage with a quotation. A matching passage does not establish that the figure supports this finding; check the entity, period, unit and meaning." : "以下数值与引文的已记录原文位置完全相同。位置相同不代表数值支持该结论，请核对对象、期间、单位和含义。"}</p> : null}
    {matches.length ? <ul>{matches.map((fact) => <li key={fact.fact_id}>{fact.label}: {fact.value} {fact.unit}{fact.currency ? ` · ${fact.currency}` : ""} · {fact.period} <small>({fact.fact_id})</small></li>)}</ul> : null}
    {related.length ? <p>{en ? "Calculations using these recorded figures (not necessarily supporting this finding): " : "使用这些已记录数值的计算（不一定支持本结论）："}{related.map((item) => item.calculation_index + 1).join("、")}</p> : null}
    {map.truncated ? <p role="status">{en ? "The recorded link index is truncated; some relationships are not shown." : "关联索引已截断，部分关系未显示。"}</p> : null}
  </details>;
}
