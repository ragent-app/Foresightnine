import type { ResearchClaim, SourceMetadata } from "../general-research-client";

interface Props { value: unknown; links: unknown; claims: ResearchClaim[]; sources: SourceMetadata[]; en: boolean }
type Question = { question: string; claim_ids: string[] };
type Counterpoint = { claim_id: string; attribution: string; source_ids: string[] };
type QuestionLinks = { question_index: number; source_ids: string[]; counterpoints: Counterpoint[] };

function readLinks(value: unknown): { rows: Map<number, QuestionLinks>; truncated: boolean } {
  const rows = new Map<number, QuestionLinks>();
  if (!value || typeof value !== "object" || Array.isArray(value)) return { rows, truncated: false };
  const snapshot = value as Record<string, unknown>;
  if (snapshot.schema_version !== "1" || !Array.isArray(snapshot.questions)) return { rows, truncated: false };
  for (const raw of snapshot.questions) {
    if (!raw || typeof raw !== "object" || !Number.isInteger(raw.question_index)) continue;
    const sourceIds = Array.isArray(raw.source_ids) ? raw.source_ids.filter((id: unknown): id is string => typeof id === "string") : [];
    const counterpoints: Counterpoint[] = Array.isArray(raw.counterpoints) ? raw.counterpoints.filter((item: unknown): item is Counterpoint => {
      if (!item || typeof item !== "object" || Array.isArray(item)) return false;
      const entry = item as Record<string, unknown>;
      return typeof entry.claim_id === "string" && (entry.attribution === "recorded" || entry.attribution === "model_suggested") && Array.isArray(entry.source_ids) && entry.source_ids.every((id: unknown) => typeof id === "string");
    }) : [];
    rows.set(raw.question_index, { question_index: raw.question_index, source_ids: sourceIds, counterpoints });
  }
  return { rows, truncated: snapshot.truncated === true };
}

export function ResearchQuestions({ value, links, claims, sources, en }: Props) {
  if (!Array.isArray(value)) return null;
  const questions = (value as Question[]).map((item, index) => ({ item, index })).filter(({ item }) => item && typeof item.question === "string" && Array.isArray(item.claim_ids));
  if (!questions.length) return null;
  const findings = new Map(claims.filter((item) => typeof item.claim_id === "string").map((item) => [item.claim_id, item]));
  const sourceNames = new Map(sources.map((source) => [source.source_id, source.title || source.source_id]));
  const snapshot = readLinks(links);
  const names = (ids: string[]) => ids.map((id) => sourceNames.get(id) ?? id).join("、");
  return <section className="general-source">
    <h3>{en ? "Model-suggested research subquestions" : "模型建议的研究子问题"}</h3>
    <p>{en ? "Links show recorded relationships, not proof that a question is answered or its evidence is sufficient." : "关联展示已记录的关系，不代表问题已回答或证据已充分。"}</p>
    {snapshot.truncated ? <p role="status">{en ? "Some relationships were omitted from this view; inspect the full research record." : "部分关联未在此展示，请查看完整研究记录。"}</p> : null}
    <ol>{questions.map(({ item, index }) => {
      const context = snapshot.rows.get(index);
      return <li key={index}>{item.question}
        {item.claim_ids.length ? <ul>{item.claim_ids.map((id) => <li key={id}>{findings.get(id)?.text ?? (en ? "Finding unavailable" : "结论不可用")}</li>)}</ul> : <p role="status">{en ? "No finding linked; coverage is unknown." : "未关联结论，覆盖情况未知。"}</p>}
        {context?.source_ids.length ? <p>{en ? "Finding sources: " : "结论来源："}{names(context.source_ids)}</p> : null}
        {context?.counterpoints.length ? <><p>{en ? "Related counterpoints (relationships unverified):" : "关联反方（关系尚未核验）："}</p><ul>{context.counterpoints.map((counter, i) => <li key={`${counter.claim_id}:${counter.attribution}:${i}`}>
          {findings.get(counter.claim_id)?.text ?? (en ? "Counterpoint unavailable" : "反方内容不可用")}
          {" · "}{counter.attribution === "model_suggested" ? (en ? "model-suggested link" : "模型建议的关联") : (en ? "recorded link" : "已记录的关联")}
          {counter.source_ids.length ? ` · ${en ? "Sources" : "来源"}: ${names(counter.source_ids)}` : null}
        </li>)}</ul></> : null}
      </li>;
    })}</ol>
  </section>;
}
