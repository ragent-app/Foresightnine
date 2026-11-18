interface Props { value: unknown; en: boolean }

const statuses: Record<string, readonly [string, string]> = {
  completed: ["Completed; results are unverified leads.", "已完成；结果是待核验的线索。"],
  partial: ["Partially completed; some candidates were omitted.", "部分完成；部分候选未保留。"],
  failed: ["The search did not complete.", "搜索未完成。"],
  unavailable: ["Search is unavailable.", "搜索不可用。"],
  budget_exhausted: ["Search or time budget was exhausted.", "搜索次数或时间预算不足。"],
  cost_unknown: ["Search was skipped because its price is unknown under the cost limit.", "费用上限下搜索价格未知，已跳过。"],
};
const reasons: Record<string, readonly [string, string]> = {
  timeout: ["Timed out.", "请求超时。"],
  rate_limited: ["Rate limited.", "请求受到限流。"],
  unavailable: ["Provider unavailable.", "搜索服务不可用。"],
  invalid_response: ["Provider response was invalid.", "搜索服务返回的数据无效。"],
  rejected: ["Provider rejected the request.", "搜索服务拒绝了请求。"],
  budget_exhausted: ["Budget exhausted.", "预算不足。"],
  other_failure: ["Failure cause was not classified.", "失败原因未分类。"],
};

export function ResearchSearchStatus({ value, en }: Props) {
  if (!Array.isArray(value)) return null;
  const items = value.filter((item): item is { stage: string; status: string; reason?: string | null; retryable?: boolean } =>
    !!item && typeof item === "object" && (item.stage === "initial" || item.stage === "gap") && typeof item.status === "string" && !!statuses[item.status]);
  if (!items.length) return null;
  return <section className="general-source"><h3>{en ? "Source discovery" : "来源发现"}</h3><ul>{items.map((item, index) => <li key={`${item.stage}:${index}`}>
    {item.stage === "gap" ? (en ? "Gap search: " : "缺口搜索：") : (en ? "Initial search: " : "首次搜索：")}
    {statuses[item.status]?.[en ? 0 : 1] ?? item.status} {item.reason ? reasons[item.reason]?.[en ? 0 : 1] : null}
    {item.retryable ? (en ? " A later attempt may succeed." : "稍后重试可能成功。") : null}
  </li>)}</ul></section>;
}
