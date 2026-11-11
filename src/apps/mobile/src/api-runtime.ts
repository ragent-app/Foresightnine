import { ApiClient, ApiError, apiModeFor } from "@foresightnine/api-client";
import type { ChallengeRecord, Worldline, WorldlineBundle } from "@foresightnine/contracts";
import { getRandomBytes } from "expo-crypto";
import { rubricFor } from "./fixtures";
import { configuredApiBaseUrl } from "./runtime-config";
export { configuredApiBaseUrl, configuredApiBaseUrlFromEnvironment } from "./runtime-config";
import type { Branch, BranchKind, Language, RubricItem, TranscriptTurn } from "./types";
import { authenticatedResearchFetcher, type ResearchSessionAccess } from "./research-auth-fetch";

const CRITERIA: Record<string, string> = {
  premise_clarity: "premise", time_boundary: "time", trigger_observability: "trigger", causal_chain: "causal", counterfactuals: "counterfactual", evidence_quality: "evidence", asset_transmission: "transmission", already_priced: "priced", falsifiability: "falsifiable", user_confirmation: "confirmation",
};

export function createMobileApiClient(access: ResearchSessionAccess): ApiClient | null {
  const baseUrl = configuredApiBaseUrl();
  return baseUrl ? new ApiClient({ baseUrl, fetcher: authenticatedResearchFetcher(baseUrl, access), idempotencyKeyFactory: () => `f9-${Array.from(getRandomBytes(16), (byte) => byte.toString(16).padStart(2, "0")).join("")}` }) : null;
}

export function mobileApiMode(): "api" | "offline-mock" { return apiModeFor(configuredApiBaseUrl()); }

export function apiErrorNotice(error: unknown, language: Language): string {
  if (error instanceof ApiError && error.status === 401) return language === "en" ? "Sign-in expired. Sign in again before continuing this research." : "登录已过期，请重新登录后继续研究。";
  if (error instanceof ApiError && error.retryAfter !== undefined) return language === "en" ? `API rate limited; retry after ${error.retryAfter}s.` : `API 触发限流，请在 ${error.retryAfter} 秒后重试。`;
  return language === "en" ? "API unavailable or rejected; provider output is hidden." : "API 暂不可用或拒绝请求；供应商原始输出已隐藏。";
}

export interface MobileChallengeUpdate {
  readonly challengeId: string;
  readonly turns: readonly TranscriptTurn[];
  readonly rubric: readonly RubricItem[];
  readonly status: "streaming" | "complete";
}

export function challengeToMobileUi(record: ChallengeRecord, viewpoint: string, language: Language, replyBody = "", priorTurns: readonly TranscriptTurn[] = []): MobileChallengeUpdate {
  const challengeText = [...record.objections, ...record.missing_evidence.map((item) => `${language === "en" ? "Missing evidence" : "缺失证据"}: ${item}`), ...record.questions].join("\n") || (language === "en" ? "No challenge text returned." : "API 未返回挑战文本。");
  const turns: TranscriptTurn[] = priorTurns.length ? [...priorTurns] : [
    { id: `api-system-${record.turn_id}`, author: "system", body: language === "en" ? "Server-backed structured challenge response." : "服务端返回的结构化反方审查结果。" },
    { id: `api-user-${record.turn_id}`, author: "user", body: viewpoint },
  ];
  const append = (turn: TranscriptTurn) => { if (!turns.some((item) => item.id === turn.id)) turns.push(turn); };
  if (replyBody.trim()) append({ id: `api-reply-${record.turn_id}`, author: "user", body: replyBody.trim() });
  append({ id: `api-challenger-${record.turn_id}`, author: "challenger", body: challengeText, review: {
    questions: record.questions.map((item) => item.trim()).filter(Boolean),
    objections: record.objections.map((item) => item.trim()).filter(Boolean),
    missingEvidence: record.missing_evidence.map((item) => item.trim()).filter(Boolean),
  } });
  const base = rubricFor(language);
  const scores = new Map(record.rubric_scores.map((item) => [CRITERIA[item.criterion], item]));
  // A returned record is completed output, not an in-flight model stream.
  // Confirmation still requires a real reply and the independent rubric/evidence gates.
  return { challengeId: record.challenge_id, turns, rubric: base.map((item) => { const score = scores.get(item.id); return score ? { ...item, score: score.score, rationale: score.rationale } : item; }), status: "complete" };
}

function asInterval(range: { readonly minimum: number; readonly maximum: number }) { return { low: range.minimum, high: range.maximum }; }

function branchDescription(worldline: Worldline, language: Language): string { return worldline.asset_impacts.map((impact) => impact.first_order_reason).join("\n") || (language === "en" ? "Server-generated worldline." : "服务端生成的世界线。"); }

export function worldlineBundleToBranches(bundle: WorldlineBundle, language: Language): readonly Branch[] {
  return bundle.worldlines.map((worldline) => {
    const probability = bundle.probability_assessments.find((item) => item.worldline_id === worldline.worldline_id);
    if (!probability) throw new Error("API probability assessment does not match worldline.");
    return {
      kind: worldline.branch_kind as BranchKind,
      title: worldline.title,
      baselineDate: worldline.baseline_date,
      horizonEnd: worldline.horizon_end,
      dataAsOf: worldline.nodes[0]?.data_as_of ?? worldline.baseline_date,
      fixtureState: "historical_reference_and_simulated",
      premise: asInterval(probability.premise_probability_range), premisePoint: probability.premise_probability,
      conditional: asInterval(probability.conditional_probability_range), conditionalPoint: probability.conditional_probability,
      joint: asInterval(probability.joint_probability_range), jointPoint: probability.joint_probability,
      description: branchDescription(worldline, language),
      reveal: worldline.event_reveal_schedule.map((item) => `${item.event_date} → ${item.reveal_date}`).join(" · "),
      nodes: worldline.nodes.map((node) => ({ id: node.node_id, time: node.time_window, event: node.event, occurrenceDate: node.event_date, revealDate: worldline.event_reveal_schedule.find((item) => item.event_id === node.node_id)?.reveal_date ?? node.event_date, dataAsOf: node.data_as_of, observableIndicators: node.observable_indicators, causalParents: node.causal_parents, assetAnchors: worldline.asset_anchors, sourceRefs: worldline.source_refs.map((item) => item.ref_id), stateChange: node.state_changes.join("; ") })),
      assetAnchors: worldline.asset_anchors,
      sourceRefs: worldline.source_refs.map((item) => item.ref_id),
    };
  });
}
