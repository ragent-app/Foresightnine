import type { ResearchFact } from "../general-research-client";

export function DirectionDerivation({ fact, en }: { fact: ResearchFact; en: boolean }) {
  const proof = fact.direction_derivation;
  if (!proof || proof.method !== "source_direction_percent_v1" || proof.direction !== "decrease" ||
      proof.verification !== "textual_conversion_not_independent_fact_verification" ||
      !fact.source_id || proof.source_id !== fact.source_id || !fact.quotation || !proof.subject) return null;
  return <div className="general-source" aria-label={en ? "Direction and calculation input" : "方向与计算输入"}>
    <p>{en ? "Source expression" : "来源原文"}: “{fact.quotation}”</p>
    <p>{en ? "Source" : "来源"}: {fact.source_id}</p>
    <p>{en ? "Value used" : "采用数值"}: {fact.value} {fact.unit} ({fact.period})</p>
    <p>{en
      ? `Rule-based explanation: the source describes a decrease in ${proof.subject}, so the change is represented as negative. This conversion does not independently verify the fact or its interpretation.`
      : `规则解释：原文描述 ${proof.subject} 下降，因此将变化表示为负数。事实及其含义尚未独立核验。`}</p>
  </div>;
}
