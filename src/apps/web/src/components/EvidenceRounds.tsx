interface Props { value: unknown; en: boolean }

export function EvidenceRounds({ value, en }: Props) {
  if (!Array.isArray(value) || !value.length) return null;
  const labels: Record<string, [string, string]> = {
    retrieved: ["retrieved", "已获取"], duplicate: ["duplicate", "重复"], restricted: ["restricted", "受限"],
    unconfigured: ["not configured", "未配置"], failed: ["failed", "失败"], invalid: ["invalid", "无效"],
    unprocessed: ["not processed", "未处理"],
  };
  return <details className="general-source"><summary>{en ? "Evidence collection rounds" : "自主取证轮次"}</summary>
    <p>{en ? "Search candidates are leads. New ranges record model input, not complete reading or verified support." : "搜索候选只是线索；新增范围记录模型输入，不代表全文阅读或语义支持已核验。"}</p>
    <ol>{value.map((item: unknown, index: number) => {
      if (!item || typeof item !== "object") return null;
      const row = item as Record<string, unknown>;
      const outcomes = Array.isArray(row.outcomes) ? row.outcomes : [];
      const sources = Array.isArray(row.new_source_ids) ? row.new_source_ids.filter((id): id is string => typeof id === "string") : [];
      const remaining = Array.isArray(row.remaining_source_ids) ? row.remaining_source_ids.filter((id): id is string => typeof id === "string") : [];
      return <li key={index}>{en ? "Round" : "第"} {typeof row.round === "number" ? row.round : index + 1}{en ? "" : " 轮"}
        {sources.length ? <span> · {en ? "new sources" : "新增来源"}: {sources.join(", ")}</span> : null}
        {remaining.length ? <span> · {en ? "unprocessed sources" : "尚未处理来源"}: {remaining.join(", ")}</span> : null}
        {Array.isArray(row.new_ranges) && row.new_ranges.length ? <ul>{row.new_ranges.map((span: unknown, i: number) => {
          if (!span || typeof span !== "object") return null;
          const range = span as Record<string, unknown>;
          if (typeof range.source_id !== "string" || !Number.isSafeInteger(range.character_start) || !Number.isSafeInteger(range.character_end)) return null;
          return <li key={i}>{en ? "Input range in extracted text" : "提取文本送入范围"}: {range.source_id} [{String(range.character_start)}, {String(range.character_end)})</li>;
        })}</ul> : null}
        {outcomes.length ? <ul>{outcomes.map((outcome: unknown, i: number) => {
          if (!outcome || typeof outcome !== "object") return null;
          const record = outcome as Record<string, unknown>;
          const status = typeof record.status === "string" ? record.status : "";
          return <li key={i}>{en ? "Candidate" : "候选"} {typeof record.candidate_index === "number" ? record.candidate_index + 1 : i + 1}: {labels[status]?.[en ? 0 : 1] ?? (en ? "status unknown" : "状态未知")}{typeof record.source_id === "string" ? ` · ${record.source_id}` : ""}</li>;
        })}</ul> : <span> · {en ? "No retrieval outcomes recorded" : "未记录获取结果"}</span>}
      </li>;
    })}</ol>
  </details>;
}
