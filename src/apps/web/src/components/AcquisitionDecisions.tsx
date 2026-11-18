interface Props { value: unknown; en: boolean }

type Localized = readonly [string, string];
const actionLabels: Record<string, Localized> = {
  discover: ["Find sources", "寻找资料"], retrieve: ["Get original material", "获取原始资料"],
  structured: ["Get structured data", "获取结构化数据"], counterpoint: ["Consider counterarguments", "检查反方观点"],
  next_page: ["Continue reviewing available information", "继续查看已有信息"],
  read: ["Read selected material", "读取选定资料"], review: ["Review evidence", "复查依据"],
  deliver: ["Deliver research", "交付研究成果"], deliver_partial: ["Deliver available work", "交付已有成果"],
};
const reasonLabels: Record<string, Localized> = {
  selected: ["Selected for this research", "针对当前研究选择"],
  unavailable: ["The requested action is unavailable", "所需行动暂不可用"],
  partial_delivery: ["Deliver available work with gaps", "交付已有成果并保留缺口"],
  evidence_reviewed: ["Evidence review recorded", "已记录依据复查"],
  no_source: ["No source has been linked", "尚未关联资料"],
  missing_evidence: ["Further evidence is needed", "仍需补充依据"],
  unresolved_requirement: ["A research question remains open", "研究问题仍未解决"],
  conflict: ["Recorded evidence conflicts", "已记录的依据存在分歧"],
  review_conflict: ["Evidence review found a conflict", "复查发现依据存在分歧"],
  not_reviewed: ["Evidence has not been reviewed", "依据尚未复查"],
  no_progress: ["No further progress was recorded", "未发现新的可执行进展"],
  no_actionable_source: ["No permitted source can be used", "没有可使用的已授权来源"],
  sufficient: ["No further action recorded", "未记录进一步行动"],
};
const resultLabels: Record<string, Localized> = {
  completed: ["Completed", "已完成"], success: ["Completed", "已完成"],
  selected: ["Selected", "已选择"], attempted: ["Attempted", "已尝试"],
  stopped: ["Stopped", "已停止"],
  partial: ["Partly completed", "部分完成"], failed: ["Failed", "失败"],
  unavailable: ["Unavailable", "不可用"], restricted: ["Not permitted", "未获许可"],
  not_requested: ["Not requested", "未请求"], no_evidence: ["No new evidence", "没有新增依据"],
  no_progress: ["No new progress", "没有新进展"], unknown: ["Unknown", "未知"],
};

export function AcquisitionDecisions({ value, en }: Props) {
  if (!Array.isArray(value) || value.length === 0) return null;
  const text = (map: Record<string, Localized>, key: unknown, fallback: Localized) =>
    (typeof key === "string" ? map[key] : undefined)?.[en ? 0 : 1] ?? fallback[en ? 0 : 1];
  return <details className="general-source"><summary>{en ? "Evidence decisions" : "取证决定"}</summary>
    <p>{en ? "These records show actions and their results; they do not prove a finding is supported." : "这里记录取证行动和结果，不代表结论已获证实。"}</p>
    <ol>{value.map((item: unknown, index: number) => {
      if (!item || typeof item !== "object" || Array.isArray(item)) return null;
      const row = item as Record<string, unknown>;
      const requirement = typeof row.requirement_id === "string" && /^question:[0-9a-f]{8,64}$/.test(row.requirement_id)
        ? `${en ? "Research question" : "研究问题"} (${row.requirement_id.slice(9, 17)})`
        : row.requirement_id === "user_request" ? (en ? "Original question" : "原问题") : (en ? "Requirement not recorded" : "未记录对应问题");
      const sources = Array.isArray(row.source_ids) ? row.source_ids.filter((id): id is string => typeof id === "string") : [];
      const gaps = Array.isArray(row.remaining_gap_codes) ? row.remaining_gap_codes : [];
      return <li key={index}><strong>{requirement}</strong> · {text(actionLabels, row.action, ["Action not recorded", "未记录行动"])}
        <p>{en ? "Reason" : "原因"}: {text(reasonLabels, row.reason_code, ["Not recorded or unavailable", "未记录或暂不可用"])} · {en ? "Result" : "结果"}: {text(resultLabels, row.result_status, ["Unknown", "未知"])}</p>
        {sources.length ? <p>{en ? "Related sources" : "关联资料"}: {sources.join(", ")}</p> : null}
        {Array.isArray(row.selected_ranges) && row.selected_ranges.length ? <ul>{row.selected_ranges.map((span: unknown, i: number) => {
          if (!span || typeof span !== "object" || Array.isArray(span)) return null;
          const range = span as Record<string, unknown>;
          if (typeof range.source_id !== "string" || !Number.isSafeInteger(range.character_start) || !Number.isSafeInteger(range.character_end)) return null;
          return <li key={i}>{en ? "Read range in extracted text" : "提取文本读取范围"}: {range.source_id} [{String(range.character_start)}, {String(range.character_end)})</li>;
        })}</ul> : null}
        {gaps.length ? <p>{en ? "Remaining gaps are recorded; check the research questions and evidence below." : "仍有未解决事项，请查看下方研究问题和依据。"}</p> : null}
        {row.stop_reason ? <p>{en ? "Stopping reason" : "停止原因"}: {text(reasonLabels, row.stop_reason, ["Not available", "暂不可用"])}</p> : null}
      </li>;
    })}</ol>
  </details>;
}
