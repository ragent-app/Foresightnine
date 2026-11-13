import type { ResearchFact } from "../general-research-client";

export function StructuredFactDetails({ fact, en }: { fact: ResearchFact; en: boolean }) {
  if (!fact.extraction_method || !fact.structured_metadata) return null;
  const meta = fact.structured_metadata;
  return <details className="general-source"><summary>{en ? "Structured source context" : "结构化来源上下文"}</summary>
    <p>{en ? "Extracted from a stored source snapshot. The source, public availability and financial interpretation have not been independently verified." : "从已保存资料快照提取；来源、公开可得时间及金融含义尚未独立核验。"}</p>
    <ul>{Object.entries(meta).map(([key, value]) => <li key={key}>{key}: {value === null || value === undefined ? (en ? "unknown" : "未知") : String(value)}</li>)}</ul>
  </details>;
}
