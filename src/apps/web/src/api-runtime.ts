import { ApiClient, ApiError, ApiTransportError, apiModeFor } from "@foresightnine/api-client";
import type { ChallengeRecord, ErrorResponse, Worldline, WorldlineBundle } from "@foresightnine/contracts";
import type { BranchView, ChallengeStatus, Language, RubricView, TranscriptTurn } from "./types";
import { configuredApiBaseUrl } from "./runtime-config";
import { clearOAuthSession, readOAuthSession } from "./oauth-client";
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

/**
 * Keep the research client's bearer bound to the configured API origin. The
 * session is read for every request so OAuth login/logout is reflected without
 * rebuilding the client, while redirecting or sending credentials cross-origin
 * is rejected by the browser fetcher.
 */
export const OAUTH_SESSION_INVALID_EVENT = "foresightnine:oauth-session-invalid";

function invalidateBrowserOAuthSession(): void {
  if (typeof sessionStorage !== "undefined") clearOAuthSession();
  if (typeof window !== "undefined") window.dispatchEvent(new Event(OAUTH_SESSION_INVALID_EVENT));
}

export function sameOriginOAuthFetcher(
  apiBaseUrl: string,
  sessionReader: () => ReturnType<typeof readOAuthSession> = readOAuthSession,
  onSessionInvalid: () => void = invalidateBrowserOAuthSession,
): typeof fetch {
  const allowedOrigin = new URL(apiBaseUrl).origin;
  return async (input, init) => {
    const target = new URL(typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url);
    if (target.origin !== allowedOrigin) throw new TypeError("Cross-origin API request blocked.");
    const headers = new Headers(input instanceof Request ? input.headers : undefined);
    new Headers(init?.headers).forEach((value, key) => headers.set(key, value));
    const session = sessionReader();
    if (session) headers.set("Authorization", `Bearer ${session.access_token}`);
    else headers.delete("Authorization");
    const response = await fetch(input, { ...init, headers, credentials: "omit", redirect: "error" });
    if (session && response.status === 401) onSessionInvalid();
    return response;
  };
}

export function createWebApiClient(): ApiClient | null {
  const baseUrl = configuredApiBaseUrl();
  return baseUrl ? new ApiClient({ baseUrl, fetcher: sameOriginOAuthFetcher(baseUrl) }) : null;
}

export function webApiMode(): "api" | "offline-mock" { return apiModeFor(configuredApiBaseUrl()); }

export function apiErrorNotice(error: unknown, language: Language): string {
  if (error instanceof ApiError) {
    if (error.status === 429) {
      const wait = error.retryAfter === undefined ? "" : language === "en" ? ` Retry after ${error.retryAfter}s.` : ` 请在 ${error.retryAfter} 秒后重试。`;
      return language === "en" ? `API rate limit reached.${wait}` : `API 触发限流。${wait}`;
    }
    if (error.status === 503 || error.retryable) {
      const wait = error.retryAfter === undefined ? "" : language === "en" ? ` Retry after ${error.retryAfter}s.` : ` 请在 ${error.retryAfter} 秒后重试。`;
      return language === "en" ? `The model provider is temporarily unavailable.${wait}` : `模型供应商暂时不可用。${wait}`;
    }
    return language === "en" ? "API request was rejected. No provider output was exposed." : "API 请求被拒绝；未展示模型供应商原始输出。";
  }
  if (error instanceof Error && error.name === "ContractValidationError") return language === "en" ? "API returned a response that failed the client contract check." : "API 返回结果未通过客户端合同校验。";
  if (error instanceof ApiTransportError && error.reason === "response") return language === "en" ? "The API response failed a safe client-side integrity check." : "API 响应未通过客户端安全完整性检查。";
  if (error instanceof ApiTransportError) return language === "en" ? "The browser could not complete the API transport request." : "浏览器未能完成 API 传输请求。";
  return language === "en" ? "API is unavailable; use the explicitly labelled offline/mock mode." : "API 暂不可用；请使用明确标注的离线/模拟模式。";
}

/** Convert a typed SSE operation failure into the same redacted notice used by HTTP failures. */
export function operationErrorNotice(error: ErrorResponse, retryable: boolean, language: Language): string {
  return apiErrorNotice(new ApiError(retryable ? 503 : 500, { ...error, retryable }), language);
}

export interface ChallengeUiUpdate {
  readonly challengeId: string;
  readonly turns: readonly TranscriptTurn[];
  readonly rubric: readonly RubricView[];
  readonly status: ChallengeStatus;
  readonly unresolvedIssues: readonly string[];
}

function challengeUiStatus(record: ChallengeRecord): ChallengeStatus {
  // Receiving an authoritative ChallengeRecord means the model operation has
  // finished and control belongs to the user. Server status describes the
  // decision workflow (awaiting reply/evidence/confirmation), not a still-live
  // model stream. Confirmation remains protected by transcript/evidence/rubric
  // gates, while `complete` enables the next user action.
  void record;
  return "complete";
}

function upsertTurn(turns: readonly TranscriptTurn[], turn: TranscriptTurn): readonly TranscriptTurn[] {
  return turns.some((item) => item.id === turn.id) ? turns : [...turns, turn];
}

/**
 * Merge the authoritative record into the locally retained transcript.  The
 * API record is the authoritative challenger output, while the user's reply
 * is retained locally because ChallengeRecord intentionally represents the
 * current challenger turn rather than an unbounded transcript.
 */
export function challengeToUi(record: ChallengeRecord, viewpoint: string, language: Language, phase: "initial" | "reply" | "evidence", replyBody = "", priorTurns: readonly TranscriptTurn[] = []): ChallengeUiUpdate {
  const challenger = compact([
    ...record.objections,
    ...record.missing_evidence.map((item) => `${language === "en" ? "Missing evidence" : "缺失证据"}: ${item}`),
    ...record.questions,
  ], language === "en" ? "The API returned no challenge text." : "API 未返回挑战文本。");
  const timestamp = record.created_at;
  let turns: readonly TranscriptTurn[] = priorTurns;
  if (phase === "initial" && !turns.some((turn) => turn.author === "user" && turn.body === viewpoint)) turns = upsertTurn(turns, { id: `api-user-${record.turn_id}`, author: "user", time: timestamp, body: viewpoint });
  if (phase === "reply") turns = upsertTurn(turns, { id: `api-reply-${record.turn_id}`, author: "user", time: timestamp, body: replyBody || (language === "en" ? "Reply submitted to the API." : "用户回复已提交至 API。") });
  turns = upsertTurn(turns, {
    id: `api-challenger-${record.turn_id}`,
    author: "challenger",
    time: timestamp,
    body: challenger,
    review: {
      objections: record.objections.map((item) => item.trim()).filter(Boolean),
      missingEvidence: record.missing_evidence.map((item) => item.trim()).filter(Boolean),
      questions: record.questions.map((item) => item.trim()).filter(Boolean),
    },
  });
  const rubric = record.rubric_scores.map((item): RubricView => {
    const labels = RUBRIC_LABELS[item.criterion] ?? [item.criterion, item.criterion];
    return { label: language === "en" ? labels[1] : labels[0], score: item.score, max: 4, rationale: item.rationale };
  });
  return { challengeId: record.challenge_id, turns, rubric, status: challengeUiStatus(record), unresolvedIssues: record.unresolved_issues };
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
