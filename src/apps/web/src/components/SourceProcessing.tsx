import type { SourceMetadata } from "../general-research-client";

interface Props { value: unknown; sources: SourceMetadata[]; en: boolean }

const labels: Record<string, readonly [string, string]> = {
  simulated: ["simulated source excluded", "模拟资料已排除"],
  source_not_authorized: ["source use not authorized", "资料用途未获授权"],
  model_permission_missing: ["model processing not permitted", "未授权模型处理"],
  display_permission_missing: ["display not permitted", "未授权展示"],
  availability_unestablished: ["availability at cutoff not established", "截止时点可用性未确认"],
  source_limit: ["source count limit reached", "超过资料数量上限"],
  complete: ["complete extracted text selected", "已选入完整提取文本"],
  partial: ["partial extracted text selected", "仅选入部分提取文本"],
  not_selected: ["not selected for the initial prompt", "未选入首次模型输入"],
  unknown: ["initial selection unknown", "首次选取范围未知"],
};

export function SourceProcessing({ value, sources, en }: Props) {
  if (!Array.isArray(value) || !value.length) return null;
  const entries = value.filter((item): item is { source_id: string; eligibility: string; initial_selection: string } =>
    item && typeof item === "object" && typeof item.source_id === "string" &&
    typeof item.eligibility === "string" && typeof item.initial_selection === "string");
  if (!entries.length) return null;
  const titles = new Map(sources.map((source) => [source.source_id, source.title || source.source_id]));
  return <details className="general-source"><summary>{en ? "Selected source processing" : "已选资料处理情况"}</summary>
    <p>{en ? "Initial prompt selection only; selection does not prove source quality, model comprehension or completeness of the original extraction." : "仅表示首次模型输入的选取情况；选入不证明资料质量、模型理解或原件提取完整。"}</p>
    <ul>{entries.map((item, index) => {
      const code = item.eligibility === "eligible" ? item.initial_selection : item.eligibility;
      return <li key={`${item.source_id}-${index}`}>{titles.get(item.source_id) ?? item.source_id}: {labels[code]?.[en ? 0 : 1] ?? (en ? "status unavailable" : "状态不可用")}</li>;
    })}</ul>
  </details>;
}
