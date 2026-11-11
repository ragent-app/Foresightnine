import type { ApiClient } from "@foresightnine/api-client";
import { challengeToUi } from "./api-runtime";
import type { AppState, AssetFixture } from './types';
import { initialState, safeStageFor } from './state';

export const WORKSPACE_VERSION = 1;
const LIMIT = 4 * 1024 * 1024;
const PRIVATE_KEY = /^(access_token|refresh_token|cancel_token|resume_token|api_key|private_key|password|secret|cookie)$/i;
export interface ResearchWorkspace { version: 1; scope: string; savedAt: string; state: AppState; assets: readonly AssetFixture[] }
export function workspaceScope(apiBase: string | null, accountId: string | null): string {
  return `${apiBase ?? 'offline'}|${accountId ?? 'anonymous'}`;
}
export function workspaceKey(scope: string): string { return `foresightnine.web.workspace.v1:${encodeURIComponent(scope)}`; }
function object(value: unknown): value is Record<string, unknown> { return value !== null && typeof value === 'object' && !Array.isArray(value); }
function strings(value: unknown): value is string[] { return Array.isArray(value) && value.every(x => typeof x === 'string'); }
function pack(value: unknown, depth = 0): unknown {
  if (depth > 40) throw new Error('Workspace depth exceeded');
  if (value instanceof Float64Array) return { $kind: 'float64', values: Array.from(value, x => pack(x, depth + 1)) };
  if (value instanceof Map) return { $kind: 'map', values: [...value].map(([k, v]) => [pack(k, depth + 1), pack(v, depth + 1)]) };
  if (typeof value === 'number' && (!Number.isFinite(value) || Object.is(value, -0))) return { $kind: 'number', value: Object.is(value, -0) ? '-0' : String(value) };
  if (Array.isArray(value)) return value.map(x => pack(x, depth + 1));
  if (object(value)) return Object.fromEntries(Object.entries(value).map(([k, v]) => {
    if (PRIVATE_KEY.test(k) || ['__proto__', 'constructor', 'prototype', '$kind'].includes(k)) throw new Error('Unexpected workspace field');
    return [k, pack(v, depth + 1)];
  }));
  return value;
}
function unpack(value: unknown, depth = 0): unknown {
  if (depth > 40) throw new Error('Workspace depth exceeded');
  if (Array.isArray(value)) return value.map(x => unpack(x, depth + 1));
  if (!object(value)) return value;
  if (value.$kind === 'number' && Object.keys(value).length === 2 && ['NaN', 'Infinity', '-Infinity', '-0'].includes(String(value.value))) return Number(value.value);
  if (value.$kind === 'float64' && Object.keys(value).length === 2 && Array.isArray(value.values)) {
    const values = value.values.map(x => unpack(x, depth + 1));
    if (!values.every(x => typeof x === 'number')) throw new Error('Invalid numeric vector');
    return Float64Array.from(values as number[]);
  }
  if (value.$kind === 'map' && Object.keys(value).length === 2 && Array.isArray(value.values)) {
    const values = value.values.map(x => unpack(x, depth + 1));
    if (!values.every(x => Array.isArray(x) && x.length === 2 && typeof x[0] === 'number')) throw new Error('Invalid factor map');
    return new Map(values as [number, unknown][]);
  }
  if (Object.keys(value).some(k => PRIVATE_KEY.test(k) || ['__proto__','prototype','constructor','$kind'].includes(k))) throw new Error('Invalid workspace field');
  return Object.fromEntries(Object.entries(value).map(([k,v]) => [k, unpack(v, depth + 1)]));
}
function validState(s: unknown): s is AppState {
  if (!object(s) || Object.keys(s).some(k => !(k in initialState))) return false;
  for (const k of ['viewpoint','capital','replyDraft','evidenceSearchQuery','reservationNote','fmStep','exportNotice','apiError'] as const) if (typeof s[k] !== 'string') return false;
  if (s.reviewConfirmation !== undefined && !["pending","confirmed","reserved"].includes(String(s.reviewConfirmation))) return false;
  if ((s.viewpoint as string).length > 2000 || (s.replyDraft as string).length > 4000) return false;
  for (const [k, values] of Object.entries({stage:['thesis','challenge','worldline','fm','portfolio','nav','console'], language:['zh','en'],currency:['CNY','USD','EUR'],challengeStatus:['idle','streaming','submitting_reply','cancelled','recovering','failed','complete'],confirmation:['pending','confirmed','reserved'],selectedBranch:['weak','main','strong'],fmStatus:['idle','computing','complete','cancelled','failed'],portfolioMode:['raw','public'],stressScenario:['weak','main','strong']})) if (!values.includes(String(s[k]))) return false;
  for (const k of ['draftRestored','unresolvedIssuesAcknowledged','drawdownVisible']) if (typeof s[k] !== 'boolean') return false;
  for (const k of ['apiSessionId','apiChallengeId','apiOperationId','apiCompletedOperationId']) if (s[k] !== null && typeof s[k] !== 'string') return false;
  if (![null,'initial','reply'].includes(s.apiPendingPhase as null) || typeof s.fmProgress !== 'number') return false;
  if (!Array.isArray(s.challengeTurns) || s.challengeTurns.length > 500 || !s.challengeTurns.every(x => object(x) && ['id','time','body'].every(k => typeof x[k] === 'string') && ['user','challenger','system'].includes(String(x.author)) && (x.review === undefined || object(x.review) && ['objections','missingEvidence','questions'].every(k => strings((x.review as Record<string, unknown>)[k]))))) return false;
  if (!Array.isArray(s.rubric) || s.rubric.length !== 10 || !s.rubric.every(x => object(x) && typeof x.label === 'string' && typeof x.rationale === 'string' && typeof x.score === 'number' && x.score >= 0 && x.score <= 4 && x.max === 4)) return false;
  if (!object(s.evidenceDraft) || !['url','title','publisher','published_at','excerpt','user_note','grade','supports_or_challenges'].every(k => typeof (s.evidenceDraft as Record<string, unknown>)[k] === 'string')) return false;
  if (!Array.isArray(s.evidence) || s.evidence.length > 500 || !s.evidence.every(x => object(x) && ['evidence_id','challenge_id','title','excerpt','user_note','content_hash','source_type','grade','supports_or_challenges','retrieved_at'].every(k => typeof x[k] === 'string') && typeof x.independently_reviewable === 'boolean')) return false;
  if (s.serverBranches !== null && (!Array.isArray(s.serverBranches) || s.serverBranches.length !== 3 || !s.serverBranches.every(x => object(x) && ['kind','title','range','color','description','reveal','invalidation'].every(k => typeof x[k] === 'string') && strings(x.nodes) && object(x.worldline) && Array.isArray(x.worldline.event_reveal_schedule) && object(x.assessment) && object(x.assessment.conditional_probability_range)))) return false;
  if (s.fmResult !== null) {
    const f = s.fmResult;
    if (!object(f) || typeof f.cutoff !== 'string' || !Array.isArray(f.selectedFactorIds) || !Array.isArray(f.lastFactorDescriptors) || !(f.factorSignalsByTime instanceof Map) || !object(f.rawClientResult) || !(f.rawClientResult.targetWeights instanceof Float64Array) || !(f.rawClientResult.forecastReturns instanceof Float64Array) || !object(f.weights) || !object(f.combination) || !(f.forecasts instanceof Float64Array) || !Array.isArray(f.reasons)) return false;
  }
  return object(s.freshness) && typeof s.freshness.runtimeAsOf === 'string';
}
export function encodeWorkspace(scope: string, state: AppState, assets: readonly AssetFixture[], now = new Date()): string {
  // Deliberate whitelist: authentication and operation control tokens are never state fields.
  // Keep the persisted v1 schema compatible with the previous deployment.
  // This runtime-only receipt is re-read authoritatively by verifyWorkspace.
  const selected = Object.fromEntries(Object.keys(initialState).filter(k => k !== "reviewConfirmation").map(k => [k, state[k as keyof AppState]]));
  const text = JSON.stringify(pack({version: WORKSPACE_VERSION, scope, savedAt: now.toISOString(), state: selected, assets}));
  if (text.length > LIMIT) throw new Error('Workspace too large');
  return text;
}
export function decodeWorkspace(text: string, scope: string): ResearchWorkspace | null {
  if (text.length > LIMIT) return null;
  try {
    const w = unpack(JSON.parse(text));
    if (!object(w) || w.version !== 1 || w.scope !== scope || typeof w.savedAt !== 'string' || !Number.isFinite(Date.parse(w.savedAt)) || !validState(w.state)) return null;
    if (!Array.isArray(w.assets) || !w.assets.every(x => object(x) && typeof x.assetId === 'string' && typeof x.price === 'number' && Number.isFinite(x.price) && ['displayName','economicExposureId','instrumentId','assetClass','asOf'].every(k => typeof x[k] === 'string') && ['CNY','USD','EUR'].includes(String(x.quoteCurrency)) && typeof x.lotSize === 'number' && Number.isFinite(x.lotSize) && x.lotSize > 0 && typeof x.fractionalSupported === 'boolean' && typeof x.tradable === 'boolean' && ['high','medium','low','restricted'].includes(String(x.liquidity)) && ['low','high'].includes(String(x.volatility)) && ['current','historical_only','stale'].includes(String(x.freshness)))) return null;
    const state: AppState = {...w.state, reviewConfirmation:w.state.reviewConfirmation ?? "pending", draftRestored:true, exportNotice:'', freshness:{...w.state.freshness,providerStatus:'unavailable',fxProviderStatus:'unavailable'}, ...(w.state.fmStatus === 'computing' ? {fmStatus:'cancelled',fmProgress:0,fmStep:'计算已中断，可重新运行。'} as const : {})};
    return {version:1, scope, savedAt:w.savedAt, state:{...state,stage:safeStageFor(state)},assets:w.assets as unknown as AssetFixture[]};
  } catch { return null; }
}

export async function verifyWorkspace(workspace: ResearchWorkspace, client: Pick<ApiClient, "getChallenge" | "streamOperationEvents"> | null, signal: AbortSignal): Promise<AppState> {
  if (signal.aborted) throw new DOMException("Aborted", "AbortError");
  let restored = workspace.state;
  let challengeId = restored.apiChallengeId;
  if (client && ["streaming", "submitting_reply", "recovering"].includes(restored.challengeStatus) && !restored.apiPendingPhase) {
    throw new Error("Unacknowledged request; automatic resubmission is disabled");
  }
  if (client && restored.apiPendingPhase) {
    if (!restored.apiOperationId) throw new Error("Missing operation receipt");
    let terminal = false;
    // Read-only durable event replay: restoration does not invoke any model POST.
    for await (const event of client.streamOperationEvents(restored.apiOperationId, {signal, lastEventId:`${restored.apiOperationId}:0`})) {
      if (event.data.event === "operation.error") {
        terminal = true;
        restored = {...restored, apiPendingPhase:null, apiOperationId:restored.apiCompletedOperationId,
          apiError:restored.language === "en" ? "The previous request ended without a result. Your answer draft is retained." : "上次请求未生成结果，答复草稿仍保留。"};
        if (!restored.apiOperationId) return {...initialState, language:restored.language, viewpoint:restored.viewpoint, capital:restored.capital, currency:restored.currency, replyDraft:restored.replyDraft, evidenceDraft:restored.evidenceDraft, evidenceSearchQuery:restored.evidenceSearchQuery, draftRestored:true, apiError:restored.apiError};
        break;
      }
      if (event.data.event === "operation.complete") {
        terminal = true;
        challengeId = event.data.result_ref.startsWith("challenge:") ? event.data.result_ref : null;
        break;
      }
    }
    if (!terminal || !challengeId) throw new Error("Previous operation has no terminal result");
  }
  if (client && challengeId) {
    if (!restored.apiOperationId) throw new Error("Missing authoritative operation receipt");
    const receipt = await client.getChallenge(challengeId, {operationId:restored.apiOperationId, signal});
    const record = receipt.challenge;
    if (record.challenge_id !== challengeId) throw new Error("Research identity mismatch");
    const update = challengeToUi(record, restored.viewpoint, restored.language, restored.apiPendingPhase ?? "evidence", restored.replyDraft, restored.challengeTurns);
    // Server confirmation alone is not a completed worldline bundle. A refresh
    // between confirmation and generation must not expose fixture branches.
    const reviewConfirmation = record.status === "CONFIRMED" ? record.user_confirmation === "confirmed_with_reservations" ? "reserved" : record.user_confirmation === "confirmed" ? "confirmed" : "pending" : "pending";
    const confirmation = restored.serverBranches ? reviewConfirmation : "pending";
    restored = {...restored, apiChallengeId:challengeId, apiCompletedOperationId:restored.apiOperationId, apiPendingPhase:null, challengeStatus:update.status, rubric:update.rubric,
      challengeTurns:restored.apiPendingPhase ? update.turns : restored.challengeTurns,
      replyDraft:restored.apiPendingPhase === "reply" ? "" : restored.replyDraft, confirmation, reviewConfirmation,
      ...(confirmation === "pending" ? {serverBranches:null, fmResult:null, fmStatus:"idle" as const} : {})};
  }
  if (signal.aborted) throw new DOMException("Aborted", "AbortError");
  return restored;
}
