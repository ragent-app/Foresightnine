interface Props { value: unknown; en: boolean }

const stages: Record<string, readonly [string, string]> = {
  planning: ["Planning", "规划"], sources: ["Sources", "资料"], analysis: ["Analysis", "分析"],
  review: ["Follow-up and review", "补充与复查"], delivery: ["Delivery", "交付"],
};
const statuses: Record<string, readonly [string, string]> = {
  completed: ["Executed", "已执行"], pending: ["Pending when saved", "保存时尚未完成"],
  attention: ["Needs attention", "需要关注"], not_requested: ["Not requested", "未请求"],
  unknown: ["Not recorded", "未记录"],
};

export function ResearchStageSummary({ value, en }: Props) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const summary = value as { schema_version?: unknown; stages?: unknown };
  if (summary.schema_version !== "1" || !Array.isArray(summary.stages)) return null;
  const rows = summary.stages.filter((item): item is { stage: string; status: string } =>
    !!item && typeof item === "object" && typeof item.stage === "string" && typeof item.status === "string" && !!stages[item.stage] && !!statuses[item.status]);
  if (!rows.length) return null;
  return <section className="general-source"><h3>{en ? "Saved research workflow" : "已保存的研究流程"}</h3>
    <p>{en ? "These are execution states at this version; they do not verify the findings." : "这些是本版本保存时的执行状态，不代表结论已经核验。"}</p>
    <ol>{rows.map((item) => <li key={item.stage}>{stages[item.stage]?.[en ? 0 : 1] ?? item.stage}：{statuses[item.status]?.[en ? 0 : 1] ?? item.status}</li>)}</ol>
  </section>;
}
