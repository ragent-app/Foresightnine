interface Props { scope: unknown; needs: unknown; absences: unknown; completion: unknown; en: boolean }

const scopeCoverage: Record<string, readonly [string, string]> = {
  not_requested: ["not requested", "未请求"], not_applicable: ["no findings", "无结论"],
  not_declared: ["no declarations", "未声明"], partial_model_declarations: ["partially declared", "部分声明"],
  complete_model_declarations: ["all findings declared by model", "模型已声明全部条目"],
};

export function ResearchScope({ scope, needs, absences, completion, en }: Props) {
  if (!scope || typeof scope !== "object" || !("attempted" in scope) || scope.attempted !== true) return null;
  const partial = !("scope" in scope) || scope.scope !== "complete_input_selected";
  const actions = needs && typeof needs === "object" && "items" in needs && Array.isArray(needs.items) ? needs.items : [];
  const observations = absences && typeof absences === "object" && "observations" in absences && Array.isArray(absences.observations) ? absences.observations : [];
  return <section className="general-source" aria-label={en ? "Evidence scope and next steps" : "证据范围与下一步"}>
    <h3>{en ? "Evidence scope and next steps" : "证据范围与下一步"}</h3>
    {'claim_scope_coverage' in scope ? <p>{en
      ? `Model scope declarations: ${scopeCoverage[String(scope.claim_scope_coverage)]?.[0] ?? 'unknown'}; review declarations: ${scopeCoverage[String('review_scope_coverage' in scope ? scope.review_scope_coverage : 'unknown')]?.[0] ?? 'unknown'}. These are coverage records, not independent verification.`
      : `模型范围声明覆盖：${scopeCoverage[String(scope.claim_scope_coverage)]?.[1] ?? '未知'}；复查声明覆盖：${scopeCoverage[String('review_scope_coverage' in scope ? scope.review_scope_coverage : 'unknown')]?.[1] ?? '未知'}。这些仅是记录情况，不构成独立核验。`}</p> : null}
    {completion && typeof completion === "object" && "execution_status" in completion ? <p>{completion.execution_status === "known_gap"
      ? (en ? "A requested action or reported evidence need remains unresolved." : "有请求的步骤或已报告的证据需求尚未解决。")
      : (en ? "No execution gap was recorded; task coverage and source support remain unverified." : "没有已记录的执行缺口；任务覆盖和来源支持程度仍未核验。")}</p> : null}
    <p role="status">{partial
      ? (en ? "Only part of the supplied material was selected, or selection is unknown. Claims and notes about missing information need review; unseen material cannot establish absence." : "已提供资料仅部分选入，或选入范围未知。关于缺失信息的结论和说明需要复核；不能据未查看内容认定不存在。")
      : (en ? "The complete extracted input was selected. Selection does not prove exhaustive review or that a missing item does not exist." : "已选入完整提取文本。选入不代表已穷尽核查，也不能证明某项信息不存在。")}</p>
    {actions.length > 0 ? <ul>{actions.map((item: unknown, index: number) => {
      if (!item || typeof item !== "object" || !("evidence_type" in item)) return null;
      if (item.evidence_type === "unexamined_or_unselected_source_material") return <li key={index}>{en ? "Inspect the unselected material or supply relevant additional material; it has not yet been verified." : "核查未选入的资料，或补充相关资料；这些材料尚未核验。"}</li>;
      if (item.evidence_type === "material_for_unanswered_question") return <li key={index}>{en ? "Identify and supply evidence for suggested questions without a recorded finding; the need remains unverified." : "为尚无已记录结论的建议问题寻找并补充相关证据；该缺口仍需核查。"}</li>;
      if (item.evidence_type === "model_suggested_material") {
        const proposal = item as { requested_fact?: unknown; reason?: unknown; source_kind?: unknown; action?: unknown };
        return <li key={index}>{en ? "Suggested missing evidence (not verified): " : "建议补充的证据（未核验）："}{typeof proposal.requested_fact === "string" ? proposal.requested_fact : ""}
          {typeof proposal.reason === "string" ? ` — ${proposal.reason}` : ""}
          {typeof proposal.source_kind === "string" ? `; ${en ? "material type" : "资料类型"}: ${proposal.source_kind}` : ""}
          {typeof proposal.action === "string" ? `; ${en ? "next step" : "下一步"}: ${proposal.action}` : ""}</li>;
      }
      return null;
    })}</ul> : null}
    {observations.length > 0 ? <><h4>{en ? "Unconfirmed absence observations" : "未确认的缺失观察"}</h4><ul>{observations.map((item: unknown, index: number) =>
      item && typeof item === "object" && "text" in item && typeof item.text === "string"
        ? <li key={index}>{item.text} <strong>{en ? "Not established; check the source and selected scope." : "尚未成立；请核对原文和已选范围。"}</strong></li> : null)}</ul></> : null}
  </section>;
}
