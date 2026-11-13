import type { ResearchClaim } from "../general-research-client";

interface Props { value: unknown; claims: ResearchClaim[]; en: boolean }
type Question = { question: string; claim_ids: string[] };

export function ResearchQuestions({ value, claims, en }: Props) {
  if (!Array.isArray(value)) return null;
  const questions = (value as Question[]).filter((item) => item && typeof item.question === "string" && Array.isArray(item.claim_ids));
  if (!questions.length) return null;
  const findings = new Map(claims.filter((item) => typeof item.claim_id === "string").map((item) => [item.claim_id, item]));
  return <section className="general-source">
    <h3>{en ? "Model-suggested research subquestions" : "模型建议的研究子问题"}</h3>
    <p>{en ? "Suggestions were generated during this research run. A linked finding does not prove the question is answered or its evidence is sufficient." : "这些子问题由本次研究模型提出。关联结论不代表问题已回答或证据已充分。"}</p>
    <ol>{questions.map((item, index) => <li key={index}>{item.question}
      {item.claim_ids.length ? <ul>{item.claim_ids.map((id) => <li key={id}>{findings.get(id)?.text ?? (en ? "Finding unavailable" : "结论不可用")}</li>)}</ul> : <p role="status">{en ? "No finding linked; coverage is unknown." : "未关联结论，覆盖情况未知。"}</p>}
    </li>)}</ol>
  </section>;
}
