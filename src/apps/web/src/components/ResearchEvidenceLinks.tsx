import type { ResearchFact } from "../general-research-client";

type CitationLink = { citation_index: number; same_passage_fact_ids: string[] };
type ClaimLinks = { claim_index: number; citations: CitationLink[] };
type CalculationLink = { calculation_index: number; inputs: { fact_id: string | null; fact_reference: string }[] };
type EvidenceLinks = { schema_version: string; truncated: boolean; claims: ClaimLinks[]; calculations: CalculationLink[] };

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
  if (!matches.length && !map.truncated) return null;
  const related = map.calculations.filter((calc) => calc && Number.isInteger(calc.calculation_index) &&
    calc.calculation_index >= 0 && calc.calculation_index < calculationCount && Array.isArray(calc.inputs) &&
    calc.inputs.some((input) => input?.fact_reference === "unique" && typeof input.fact_id === "string" && ids.has(input.fact_id)));
  return <details className="general-source">
    <summary>{en ? "Recorded evidence links" : "已记录的证据关联"}</summary>
    <p>{en ? "These figures share an exact recorded source passage with a quotation. A matching passage does not establish that the figure supports this finding; check the entity, period, unit and meaning." : "以下数值与引文的已记录原文位置完全相同。位置相同不代表数值支持该结论，请核对对象、期间、单位和含义。"}</p>
    {matches.length ? <ul>{matches.map((fact) => <li key={fact.fact_id}>{fact.label}: {fact.value} {fact.unit}{fact.currency ? ` · ${fact.currency}` : ""} · {fact.period} <small>({fact.fact_id})</small></li>)}</ul> : null}
    {related.length ? <p>{en ? "Calculations using these recorded figures (not necessarily supporting this finding): " : "使用这些已记录数值的计算（不一定支持本结论）："}{related.map((item) => item.calculation_index + 1).join("、")}</p> : null}
    {map.truncated ? <p role="status">{en ? "The recorded link index is truncated; some relationships are not shown." : "关联索引已截断，部分关系未显示。"}</p> : null}
  </details>;
}
