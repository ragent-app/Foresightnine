import { ApiClient, ApiError, ApiTransportError, apiModeFor } from "@foresightnine/api-client";
import type { ChallengeRecord, Worldline, WorldlineBundle } from "@foresightnine/contracts";
import type { BranchView, ChallengeStatus, Language, RubricView, TranscriptTurn } from "./types";
import { configuredApiBaseUrl } from "./runtime-config";
export { configuredApiBaseUrl, configuredApiBaseUrlFromEnvironment } from "./runtime-config";

const RUBRIC_LABELS: Record<string, [string, string]> = {
  premise_clarity: ["前提清晰度", "Premise clarity"],
  time_boundary: ["时间边界", "Time boundary"],
  trigger_observability: ["触发可观察性", "Observable trigger"],
  causal_chain: ["因果链", "Causal chain"],
  counterfactuals: ["反事实与替代解释", "Counterfactuals"],
  evidence_quality: ["证据质量", "Evidence quality"],
  asset_transmission: ["资产传导", "Asset transmission"],
  already_priced: ["市场已定价程度", "Priced-in risk"],
  falsifiability: ["可证伪性", "Falsifiability"],
  user_confirmation: ["用户确认", "User confirmation"],
};

function compact(items: readonly string[], fallback: string): string {
  const values = items.map((item) => item.trim()).filter(Boolean);
  return values.length > 0 ? values.join("\n") : fallback;
}

export function createWebApiClient(): ApiClient | null {
  const baseUrl = configuredApiBaseUrl();
  return baseUrl ? new ApiClient({ baseUrl }) : null;
}

export function webApiMode(): "api" | "offline-mock" { return apiModeFor(configuredApiBaseUrl()); }

export function apiErrorNotice(error: unknown, language: Language): string {
  if (error instanceof ApiError) {
    if (error.retryAfter !== undefined) return language === "en" ? `API is rate limited; retry after ${error.retryAfter}s.` : `API 触发限流，请在 ${error.retryAfter} 秒后重试。`;
    return language === "en" ? "API request was rejected. No provider output was exposed." : "API 请求被拒绝；未展示模型供应商原始输出。";
  }
  if (error instanceof Error && error.name === "ContractValidationError") return language === "en" ? "API returned a response that failed the client contract check." : "API 返回结果未通过客户端合同校验。";
  if (error instanceof ApiTransportError && error.reason === "response") return language === "en" ? "The API response failed a safe client-side integrity check." : "API 响应未通过客户端安全完整性检查。";
  if (error instanceof ApiTransportError) return language === "en" ? "The browser could not complete the API transport request." : "浏览器未能完成 API 传输请求。";
  return language === "en" ? "API is unavailable; use the explicitly labelled offline/mock mode." : "API 暂不可用；请使用明确标注的离线/模拟模式。";
}

export interface ChallengeUiUpdate {
  readonly challengeId: string;
  readonly turns: readonly TranscriptTurn[];
  readonly rubric: readonly RubricView[];
  readonly status: ChallengeStatus;
  readonly unresolvedIssues: readonly string[];
}

function challengeUiStatus(record: ChallengeRecord, phase: "initial" | "reply"): ChallengeStatus {
  if (phase !== "reply") return "streaming";
  return record.status === "CHALLENGING" || record.status === "EVIDENCE_REQUIRED" || record.status === "CONFIRMED" ? "complete" : "streaming";
}

export function challengeToUi(record: ChallengeRecord, viewpoint: string, language: Language, phase: "initial" | "reply", replyBody = ""): ChallengeUiUpdate {
  const challenger = compact([
    ...record.objections,
    ...record.missing_evidence.map((item) => `${language === "en" ? "Missing evidence" : "缺失证据"}: ${item}`),
    ...record.questions,
  ], language === "en" ? "The API returned no challenge text." : "API 未返回挑战文本。");
  const turns: TranscriptTurn[] = [
    { id: `api-system-${record.turn_id}`, author: "system", time: new Date().toISOString(), body: language === "en" ? "Server-backed structured challenge response." : "服务端返回的结构化反方审查结果。" },
    { id: `api-user-${record.turn_id}`, author: "user", time: new Date().toISOString(), body: viewpoint },
    { id: `api-challenger-${record.turn_id}`, author: "challenger", time: new Date().toISOString(), body: challenger },
  ];
  if (phase === "reply") turns.push({ id: `api-reply-${record.turn_id}`, author: "user", time: new Date().toISOString(), body: replyBody || (language === "en" ? "Reply submitted to the API." : "用户回复已提交至 API。") });
  const rubric = record.rubric_scores.map((item): RubricView => {
    const labels = RUBRIC_LABELS[item.criterion] ?? [item.criterion, item.criterion];
    return { label: language === "en" ? labels[1] : labels[0], score: item.score, max: 4, rationale: item.rationale };
  });
  return { challengeId: record.challenge_id, turns, rubric, status: challengeUiStatus(record, phase), unresolvedIssues: record.unresolved_issues };
}

function branchDescription(worldline: Worldline, language: Language): string {
  const impacts = worldline.asset_impacts.map((impact) => impact.first_order_reason);
  return compact(impacts, language === "en" ? "Server-generated worldline with no impact summary." : "服务端世界线未提供资产影响摘要。");
}

export function worldlineBundleToBranches(bundle: WorldlineBundle, language: Language): readonly BranchView[] {
  return bundle.worldlines.map((worldline) => {
    const assessment = bundle.probability_assessments.find((item) => item.worldline_id === worldline.worldline_id);
    if (!assessment) throw new Error("API probability assessment does not match worldline.");
    return {
      kind: worldline.branch_kind,
      title: worldline.title,
      range: `${(assessment.joint_probability_range.minimum * 100).toFixed(1)}–${(assessment.joint_probability_range.maximum * 100).toFixed(1)}%`,
      color: worldline.branch_kind === "weak" ? "#B7796A" : worldline.branch_kind === "strong" ? "#6C8F7A" : "#6B7C9D",
      description: branchDescription(worldline, language),
      reveal: worldline.event_reveal_schedule.map((item) => `${item.event_date} → ${item.reveal_date}`).join(" · "),
      invalidation: worldline.invalidation_conditions.join("；"),
      nodes: worldline.nodes.map((node) => `${node.event_date} · ${node.event}`),
      assessment,
      worldline,
    };
  });
}
