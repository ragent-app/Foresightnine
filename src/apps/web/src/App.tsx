import { hasResearchProgress, readStudies, saveStudy, studiesKey, type StudyEntry } from "./study-library";
import { StudySwitcher } from "./components/StudySwitcher";
import { buildNavExport, type NavExportData } from "./nav-data";
import { navAssumptionsKey, useNavAssumptions } from "./nav-assumptions";
import { completeWorldlineFlow } from "./worldline-flow";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { AppShell, Disclosure } from "./components/AppShell";
import { LanguageProvider, t } from "./i18n";
import { Button, StatusDot, Tag } from "./components/Controls";
import { ThesisInput } from "./components/ThesisInput";
import { ChallengePanel } from "./components/ChallengePanel";
import { WorldlinePanel, WorldlineExplanation } from "./components/WorldlinePanel";
import { FmPanel, FmExplanation } from "./components/FmPanel";
import { PortfolioPanel } from "./components/PortfolioPanel";
import { NavPanel } from "./components/NavPanel";
import { ConsolePanel } from "./components/MethodsPanel";
import { buildMethodsGuide, methodsGuideFilename } from "./methods-guide";
import { SocialLoginPanel, type OAuthProviderAvailability, type OAuthReadinessStatus } from "./components/SocialLoginPanel";
import { ASSET_FIXTURES, assetsForMarketBundle, evidenceHash, fmInput, isEvidenceIndependentlyReviewable, makeBranches, MARKET_DATA_AS_OF, sourceMaterialHash } from "./fixtures";
import { allocationRows, canEnterStage, canStartFm, isReviewLocked, initialState, reducer, safeStageFor } from "./state";
import { downloadText, csvExport, navExport, researchExport } from "./exports";
import { currencySymbol, parseCapital, validateSearchEvidence } from "./validation";
import { runFmInWorker } from "./fm-client";
import { apiErrorNotice, challengeToUi, configuredApiBaseUrl, createWebApiClient, OAUTH_SESSION_INVALID_EVENT, operationErrorNotice, webApiMode, worldlineBundleToBranches } from "./api-runtime";
import { configuredMarketDataRuntimeConfig } from "./runtime-config";
import { createMarketRuntimeController } from "@foresightnine/market-data";
import { clearOAuthSession, configuredOAuthReturnUri, deleteOAuthAccount, exchangeOAuthTicket, fetchAccountBillingSummary, logoutOAuthSession, oauthStartUrl, parseOAuthCallbackUrl, readOAuthSession, saveOAuthSession, type AccountBillingSummary, type OAuthSession } from "./oauth-client";
import { authenticatedMarketFetcher } from "./market-config-client";
import type { Currency, EvidenceDraft, EvidenceView, FreshnessContext, Language, Stage } from "./types";
import type { CreateChallengeRequest, Operation } from "@foresightnine/contracts";
import { createIdempotencyKey } from "@foresightnine/api-client";

import { decodeWorkspace, encodeWorkspace, verifyWorkspace, workspaceKey, workspaceScope } from "./research-workspace";

const DRAFT_KEY = "foresightnine.web.draft.v1";
const OAUTH_PROVIDERS = ["google", "github"] as const;
const OAUTH_UNAVAILABLE: readonly OAuthProviderAvailability[] = OAUTH_PROVIDERS.map((provider) => ({ provider, enabled: false }));

function fxRatesFingerprint(rates: Readonly<Record<string, number>> | undefined): string {
  return JSON.stringify(Object.entries(rates ?? {}).sort(([left], [right]) => left.localeCompare(right)));
}

export function reconcileMarketFreshness(previous: FreshnessContext, marketDataState: ReturnType<ReturnType<typeof createMarketRuntimeController>["getState"]>): FreshnessContext {
    const providerStatus: FreshnessContext["providerStatus"] = marketDataState.status === "ready" && marketDataState.mode !== "mock"
      ? "fresh"
      : marketDataState.status === "limited" ? "stale" : "unavailable";
    const fxProviderStatus: FreshnessContext["fxProviderStatus"] = providerStatus === "fresh" && marketDataState.bundle?.fx ? "fresh" : "unavailable";
    const runtimeAsOf = marketDataState.bundle?.cutoff ?? previous.runtimeAsOf;
    // An idle runtime after refresh has no new data. Preserve the historical
    // conversion snapshot without restoring any current-data eligibility.
    const fxAsOf = marketDataState.bundle ? marketDataState.bundle.fx?.asOf : previous.fxAsOf;
    const fxCnyPerUnit = marketDataState.bundle ? marketDataState.bundle.fx?.cnyPerUnit : previous.fxCnyPerUnit;
    return { runtimeAsOf, providerStatus, fxProviderStatus, ...(fxAsOf === undefined ? {} : { fxAsOf }), ...(fxCnyPerUnit === undefined ? {} : { fxCnyPerUnit }) };
}

interface OAuthReadiness {
  readonly providers: readonly OAuthProviderAvailability[];
  readonly status: OAuthReadinessStatus;
}

function parseOAuthReadiness(value: unknown): readonly OAuthProviderAvailability[] {
  if (!value || typeof value !== "object" || !Array.isArray((value as { providers?: unknown }).providers)) throw new Error("Invalid OAuth readiness response.");
  const records = (value as { providers: unknown[] }).providers;
  if (records.length !== OAUTH_PROVIDERS.length || Object.keys(value).length !== 1) throw new Error("Invalid OAuth readiness response.");
  const parsed = records.map((record): OAuthProviderAvailability => {
    if (!record || typeof record !== "object" || Object.keys(record).length !== 2) throw new Error("Invalid OAuth readiness response.");
    const item = record as { provider?: unknown; enabled?: unknown };
    if ((item.provider !== "google" && item.provider !== "github") || typeof item.enabled !== "boolean") throw new Error("Invalid OAuth readiness response.");
    return { provider: item.provider, enabled: item.enabled };
  });
  if (new Set(parsed.map(({ provider }) => provider)).size !== OAUTH_PROVIDERS.length) throw new Error("Invalid OAuth readiness response.");
  return OAUTH_PROVIDERS.map((provider) => parsed.find((item) => item.provider === provider) as OAuthProviderAvailability);
}

export interface GenerationGuard {
  readonly current: () => number;
  readonly invalidate: () => number;
  readonly isCurrent: (token: number) => boolean;
}

type ChallengeRestart =
  | { readonly kind: "initial"; readonly request: CreateChallengeRequest }
  | { readonly kind: "reply"; readonly challengeId: string; readonly message: string };

interface ChallengeOperationContext {
  operation: Operation;
  readonly phase: "initial" | "reply";
  readonly viewpoint: string;
  readonly language: Language;
  readonly replyBody: string;
  readonly priorTurns: ReturnType<typeof reducer>["challengeTurns"];
  readonly restart: ChallengeRestart;
  lastEventId: string;
  cancelled: boolean;
  cancelIdempotencyKey: string;
}

export interface PendingChallengeReceipt {
  cancelRequested: boolean;
  cancelIdempotencyKey: string;
  submissionIdempotencyKey: string;
  recover?: (token: number) => Promise<void>;
}

export function replayPendingReceipt<T>(
  pending: Pick<PendingChallengeReceipt, "submissionIdempotencyKey">,
  submit: (idempotencyKey: string) => Promise<T>,
): Promise<T> {
  return submit(pending.submissionIdempotencyKey);
}

/** Terminal replay receipts must restart event replay before the terminal frame. */
export function operationReplayCursor(operation: Pick<Operation, "operation_id" | "last_event_id" | "status">): string {
  return ["completed", "failed", "cancelled"].includes(operation.status) ? `${operation.operation_id}:0` : operation.last_event_id;
}

export async function cancelAcceptedPendingReceipt(
  client: { cancelOperation(operationId: string, cancelToken: string, options?: { idempotencyKey?: string }): Promise<{ operation: Operation }> },
  pending: PendingChallengeReceipt,
  receipt: { readonly operation: Pick<Operation, "operation_id" | "cancel_token"> },
): Promise<Operation | null> {
  if (!pending.cancelRequested) return null;
  const cancelled = await client.cancelOperation(
    receipt.operation.operation_id,
    receipt.operation.cancel_token,
    { idempotencyKey: pending.cancelIdempotencyKey },
  );
  if (cancelled.operation.status !== "cancelled" && cancelled.operation.status !== "completed") {
    throw new Error("Compensating cancellation did not reach a terminal operation state.");
  }
  return cancelled.operation;
}

function challengeIdFromResultRef(value: string): string | null {
  return value.startsWith("challenge:") && value.length > "challenge:".length ? value : null;
}

function wasAborted(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

/** Resume only live work; terminal operations need a fresh idempotent command. */
export function recoveryActionFor(status: Operation["status"]): "resume" | "replay" | "restart" | "none" {
  if (status === "running" || status === "pending") return "resume";
  if (status === "completed") return "replay";
  if (status === "cancelled" || status === "failed") return "restart";
  return "none";
}

/** A small testable token guard for timers, workers, and other async callbacks. */
export function createGenerationGuard(): GenerationGuard {
  let generation = 0;
  return { current: () => generation, invalidate: () => { generation += 1; return generation; }, isCurrent: (token) => token === generation };
}

export function authoritativeChallengeFetchOptions(operation: Pick<Operation, "operation_id">, signal: AbortSignal) {
  return { signal, operationId: operation.operation_id };
}

export function App() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [draftHydrated, setDraftHydrated] = useState(false);
  const [authReady, setAuthReady] = useState(false);
  const [restoreRetry, setRestoreRetry] = useState(0);
  const [restoreFailure, setRestoreFailure] = useState(false);
  const [studyLibrary, setStudyLibrary] = useState<{scope:string;entries:readonly StudyEntry[]}>({scope:"",entries:[]});
  const [studyMessage, setStudyMessage] = useState({text:"",error:false});
  const [studySwitching, setStudySwitching] = useState(false);
  const studySwitchAbort = useRef<AbortController | null>(null);
  const restoredScope = useRef<string | null>(null);
  const apiBaseUrl = configuredApiBaseUrl();
  const apiClient = useMemo(() => createWebApiClient(), []);
  const marketRuntime = useMemo(() => createMarketRuntimeController(apiBaseUrl ? { fetcher: authenticatedMarketFetcher(apiBaseUrl, readOAuthSession) } : {}), [apiBaseUrl]);
  const marketConfig = useMemo(() => configuredMarketDataRuntimeConfig(), []);
  const [marketDataState, setMarketDataState] = useState(marketRuntime.getState());
  const [marketAssets, setMarketAssets] = useState(ASSET_FIXTURES);
  const marketDataAsOf = marketDataState.bundle?.cutoff ?? state.fmResult?.cutoff ?? MARKET_DATA_AS_OF;
  const oauthReturnUri = configuredOAuthReturnUri();
  const [oauthSession, setOAuthSession] = useState<OAuthSession | null>(null);
  const [billingSummary, setBillingSummary] = useState<AccountBillingSummary | null>(null);
  const [oauthNotice, setOAuthNotice] = useState<string | null>(null);
  const oauthGeneration = useRef(0);
  const languageRef = useRef(state.language);
  languageRef.current = state.language;
  const [oauthReadiness, setOAuthReadiness] = useState<OAuthReadiness>({ providers: OAUTH_UNAVAILABLE, status: apiBaseUrl ? "loading" : "offline" });
  const apiAbort = useRef<AbortController | null>(null);
  const challengeStreamAbort = useRef<AbortController | null>(null);
  const activeChallengeOperation = useRef<ChallengeOperationContext | null>(null);
  const recoveryInFlight = useRef(false);
  const worldlineSubmission = useRef(false);
  const [worldlineSubmitting, setWorldlineSubmitting] = useState(false);
  const pendingChallengeReceipts = useRef(new Set<PendingChallengeReceipt>());
  const challengeTurnsRef = useRef(state.challengeTurns);
  challengeTurnsRef.current = state.challengeTurns;
  const replyInFlight = useRef(false);
  const evidenceSubmissionInFlight = useRef(false);
  const [evidenceSubmitting, setEvidenceSubmitting] = useState(false);
  const apiMode = webApiMode();

  useEffect(() => {
    const unsubscribe = marketRuntime.subscribe(setMarketDataState);
    marketRuntime.configure(marketConfig);
    return unsubscribe;
  }, [marketConfig, marketRuntime]);

  useEffect(() => {
    const freshness = reconcileMarketFreshness(state.freshness, marketDataState);
    if (state.freshness.providerStatus === freshness.providerStatus && state.freshness.fxProviderStatus === freshness.fxProviderStatus && state.freshness.runtimeAsOf === freshness.runtimeAsOf && state.freshness.fxAsOf === freshness.fxAsOf && fxRatesFingerprint(state.freshness.fxCnyPerUnit) === fxRatesFingerprint(freshness.fxCnyPerUnit)) return;
    dispatch({ type: "set_freshness", freshness });
  }, [marketDataState, state.freshness]);

  useEffect(() => {
    if (!apiBaseUrl) {
      setOAuthReadiness({ providers: OAUTH_UNAVAILABLE, status: "offline" });
      return;
    }
    const controller = new AbortController();
    setOAuthReadiness((current) => ({ ...current, status: "loading" }));
    void fetch(new URL("v1/auth/providers", `${apiBaseUrl}/`).toString(), { method: "GET", headers: { Accept: "application/json" }, credentials: "omit", signal: controller.signal })
      .then((response) => { if (!response.ok) throw new Error("OAuth readiness request failed."); return response.json() as Promise<unknown>; })
      .then((payload) => setOAuthReadiness({ providers: parseOAuthReadiness(payload), status: "ready" }))
      .catch(() => { if (!controller.signal.aborted) setOAuthReadiness({ providers: OAUTH_UNAVAILABLE, status: "unavailable" }); });
    return () => controller.abort();
  }, [apiBaseUrl]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    setOAuthSession(readOAuthSession());
    setAuthReady(true);
    if (!oauthReturnUri) return;
    let callback;
    try { callback = parseOAuthCallbackUrl(window.location.href, oauthReturnUri); } catch {
      const target = new URL(oauthReturnUri); const current = new URL(window.location.href);
      if (current.origin === target.origin && current.pathname === target.pathname) window.history.replaceState({}, "", `${target.pathname}${target.search}`);
      setOAuthNotice(languageRef.current === "en" ? "OAuth callback was rejected." : "登录回调未通过安全校验。"); return;
    }
    if (callback.kind === "none") return;
    const target = new URL(oauthReturnUri);
    window.history.replaceState({}, "", `${target.pathname}${target.search}`);
    if (callback.kind === "error") { setOAuthNotice(languageRef.current === "en" ? "Sign-in was cancelled or unavailable." : "登录已取消或暂不可用。"); return; }
    if (!callback.ticket) return;
    if (!apiBaseUrl) { setOAuthNotice(languageRef.current === "en" ? "Sign-in service is unavailable." : "登录服务暂不可用。" ); return; }
    setAuthReady(false);
    const generation = ++oauthGeneration.current;
    void exchangeOAuthTicket(apiBaseUrl, callback.ticket).then((session) => { if (generation !== oauthGeneration.current) return; saveOAuthSession(session); setOAuthSession(session); setOAuthNotice(null); }).catch(() => { if (generation !== oauthGeneration.current) return; setOAuthNotice(languageRef.current === "en" ? "Sign-in could not be completed." : "登录未能完成。"); }).finally(() => { if (generation === oauthGeneration.current) setAuthReady(true); });
    return () => { oauthGeneration.current += 1; };
  }, [apiBaseUrl, oauthReturnUri]);

  useEffect(() => {
    const invalidate = (): void => {
      clearOAuthSession();
      setOAuthSession(null);
      setBillingSummary(null);
      setOAuthNotice(languageRef.current === "en" ? "Your session expired. Sign in before using account settings or a personal model." : "登录会话已失效；使用账户配置或个人模型前请重新登录。");
    };
    window.addEventListener(OAUTH_SESSION_INVALID_EVENT, invalidate);
    return () => window.removeEventListener(OAUTH_SESSION_INVALID_EVENT, invalidate);
  }, []);

  useEffect(() => {
    if (!apiBaseUrl || !oauthSession) {
      setBillingSummary(null);
      return;
    }
    const controller = new AbortController();
    void fetchAccountBillingSummary(apiBaseUrl, oauthSession.access_token, fetch, controller.signal)
      .then(setBillingSummary)
      .catch(() => { if (!controller.signal.aborted) setBillingSummary(null); });
    return () => controller.abort();
  }, [apiBaseUrl, oauthSession, state.apiCompletedOperationId, state.serverBranches]);

  const startOAuth = useCallback((provider: "google" | "github") => {
    if (typeof window === "undefined" || !apiBaseUrl || !oauthReturnUri) return;
    try { window.location.assign(oauthStartUrl(apiBaseUrl, provider, oauthReturnUri)); } catch { setOAuthNotice(state.language === "en" ? "Sign-in is not configured for this build." : "此版本尚未配置登录回调地址。"); }
  }, [apiBaseUrl, oauthReturnUri, state.language]);
  const logoutOAuth = useCallback(() => {
    if (!apiBaseUrl || !oauthSession) return;
    oauthGeneration.current += 1;
    void logoutOAuthSession(apiBaseUrl, oauthSession.access_token).then(() => { clearOAuthSession(); setOAuthSession(null); setBillingSummary(null); }).catch(() => { clearOAuthSession(); setOAuthSession(null); setBillingSummary(null); setOAuthNotice(state.language === "en" ? "Could not log out; local session was cleared." : "退出登录失败，但本地会话已清除。" ); });
  }, [apiBaseUrl, oauthSession, state.language]);
  const deleteOAuth = useCallback(() => {
    if (!apiBaseUrl || !oauthSession || (typeof window !== "undefined" && !window.confirm(state.language === "en" ? "Delete this account and revoke its sessions?" : "删除账户并撤销所有登录会话？"))) return;
    oauthGeneration.current += 1;
    void deleteOAuthAccount(apiBaseUrl, oauthSession.access_token).then(() => { clearOAuthSession(); setOAuthSession(null); setBillingSummary(null); }).catch(() => { clearOAuthSession(); setOAuthSession(null); setBillingSummary(null); setOAuthNotice(state.language === "en" ? "Account deletion failed; local session was cleared." : "删除账户失败，但本地会话已清除。" ); });
  }, [apiBaseUrl, oauthSession, state.language]);
  const apiChallengeId = state.apiChallengeId;
  const challengeTimer = useRef<number | null>(null);
  const fmAbort = useRef<AbortController | null>(null);
  const asyncGeneration = useRef<GenerationGuard>(createGenerationGuard());
  const capital = parseCapital(state.capital);
  const rows = useMemo(() => allocationRows(state.fmResult, capital, state.currency, state.portfolioMode, state.freshness, marketAssets), [state.fmResult, capital, state.currency, state.portfolioMode, state.freshness, marketAssets]);
  const viewpointBranches = useMemo(() => {
    if (state.serverBranches) return state.serverBranches;
    return apiMode === "api" ? [] : makeBranches(state.viewpoint);
  }, [apiMode, state.serverBranches, state.viewpoint]);

  const invalidateAsync = useCallback((): number => {
    if (challengeTimer.current !== null) window.clearTimeout(challengeTimer.current);
    challengeTimer.current = null;
    studySwitchAbort.current?.abort();
    studySwitchAbort.current = null;
    fmAbort.current?.abort();
    fmAbort.current = null;
    for (const pending of pendingChallengeReceipts.current) pending.cancelRequested = true;
    if (pendingChallengeReceipts.current.size === 0) apiAbort.current?.abort();
    apiAbort.current = null;
    challengeStreamAbort.current?.abort();
    challengeStreamAbort.current = null;
    replyInFlight.current = false;
    recoveryInFlight.current = false;
    evidenceSubmissionInFlight.current = false;
    setEvidenceSubmitting(false);
    return asyncGeneration.current.invalidate();
  }, []);

  useEffect(() => () => { invalidateAsync(); }, [invalidateAsync]);
  const researchScope = workspaceScope(apiBaseUrl, oauthSession?.account.account_id ?? null);
  useEffect(() => {
    const refresh = () => { try { setStudyLibrary({scope:researchScope,entries:readStudies(window.localStorage,researchScope)}); } catch { setStudyLibrary({scope:researchScope,entries:[]}); setStudyMessage({text:state.language === "en" ? "Saved research could not be read. Existing records are retained." : "已存研究读取失败，原记录仍保留。",error:true}); } };
    setStudyMessage({text:"",error:false}); refresh();
    const changed = (event:StorageEvent) => { if (event.key === studiesKey(researchScope)) refresh(); };
    window.addEventListener("storage",changed); return () => window.removeEventListener("storage",changed);
  }, [researchScope]);
  const navDraft = useNavAssumptions(authReady && viewpointBranches.length ? navAssumptionsKey(researchScope, viewpointBranches.map(b => b.worldline.worldline_id).sort().join("|")) : null);
  const navData = useMemo(() => buildNavExport(viewpointBranches, state.fmStatus === "complete" && (state.portfolioMode === "raw" || rows.some(r => r.publicExecutionAllowed)) ? rows : [], capital, navDraft.values), [viewpointBranches, rows, capital, navDraft.values, state.fmStatus, state.portfolioMode]);
  useEffect(() => {
    if (!authReady) return;
    const controller = new AbortController();
    restoredScope.current = null;
    setDraftHydrated(false);
    setStudySwitching(false);
    setRestoreFailure(false);
    invalidateAsync();
    activeChallengeOperation.current = null;
    dispatch({type:"reset"});
    void (async () => {
      let workspace;
      try {
        const raw = window.localStorage.getItem(workspaceKey(researchScope));
        workspace = raw ? decodeWorkspace(raw, researchScope) : null;
        if (raw && !workspace) throw new Error("Invalid saved research workspace");
        if (!workspace) {
          // One-time migration of the former text-only draft, never an auth token.
          const legacy = window.localStorage.getItem(DRAFT_KEY);
          const hasScopedDraft = Object.keys(window.localStorage).some(k => k.startsWith("foresightnine.web.workspace.v1:"));
          if (legacy && !hasScopedDraft) {
            const d = JSON.parse(legacy);
            if (typeof d.viewpoint === "string" && d.viewpoint.length <= 2000 && typeof d.capital === "string" && ["CNY","USD","EUR"].includes(d.currency)) {
              dispatch({type:"restore_draft",draft:{viewpoint:d.viewpoint,capital:d.capital,currency:d.currency,updatedAt:new Date().toISOString()}});
              if (["zh","en"].includes(d.language)) dispatch({type:"set_language",language:d.language});
            }
          }
        } else {
          const restored = await verifyWorkspace(workspace, apiClient, controller.signal);
          if (controller.signal.aborted) return;
          dispatch({type:"restore_workspace",state:restored});
          setMarketAssets(workspace.assets);
        }
        if (controller.signal.aborted) return;
        restoredScope.current = researchScope;
      } catch {
        if (controller.signal.aborted) return;
        if (workspace) dispatch({type:"restore_draft",draft:{viewpoint:workspace.state.viewpoint,capital:workspace.state.capital,currency:workspace.state.currency,updatedAt:workspace.savedAt}});
        setRestoreFailure(true);
        // Leave the original complete workspace untouched for retry; no silent overwrite.
      } finally { if (!controller.signal.aborted) setDraftHydrated(true); }
    })();
    return () => controller.abort();
  }, [authReady, researchScope, apiClient, invalidateAsync, restoreRetry]);

  useEffect(() => {
    if (!draftHydrated || studySwitching || restoredScope.current !== researchScope) return;
    try {
      window.localStorage.setItem(workspaceKey(researchScope), encodeWorkspace(researchScope, state, marketAssets));
      window.localStorage.removeItem(DRAFT_KEY);
    } catch { setStudyMessage({text:state.language === "en" ? "Autosave failed; the previous copy is retained. Export before leaving." : "自动保存失败，旧副本仍保留；离开前请导出当前研究。",error:true}); }
  }, [draftHydrated, researchScope, state, marketAssets, studySwitching]);


  const streamChallengeOperation = useCallback((context: ChallengeOperationContext, token: number) => {
    if (!apiClient) return;
    challengeStreamAbort.current?.abort();
    const controller = new AbortController();
    challengeStreamAbort.current = controller;
    void (async () => {
      let terminalObserved = false;
      try {
        for await (const event of apiClient.streamOperationEvents(context.operation.operation_id, { signal: controller.signal, lastEventId: context.lastEventId })) {
          if (controller.signal.aborted || activeChallengeOperation.current !== context || !asyncGeneration.current.isCurrent(token)) return;
          context.lastEventId = event.id;
          if (event.data.event === "operation.error") {
            terminalObserved = true;
            if (context.cancelled) dispatch({ type: "api_cancel" });
            else dispatch({ type: "api_error", notice: operationErrorNotice(event.data.error, event.data.retryable, context.language) });
            return;
          }
          if (event.data.event !== "operation.complete") continue;
          terminalObserved = true;
          const challengeId = challengeIdFromResultRef(event.data.result_ref);
          if (!challengeId) throw new Error("Challenge operation completed without a challenge result reference.");
          const response = await apiClient.getChallenge(challengeId, authoritativeChallengeFetchOptions(context.operation, controller.signal));
          if (controller.signal.aborted || activeChallengeOperation.current !== context || !asyncGeneration.current.isCurrent(token)) return;
          const update = challengeToUi(response.challenge, context.viewpoint, context.language, context.phase, context.replyBody, context.priorTurns);
          dispatch({ type: "api_challenge_update", ...update });
          dispatch({ type: "api_operation_finished", operationId: context.operation.operation_id });
          if (context.phase === "reply") dispatch({ type: "set_reply_draft", draft: "" });
          if (activeChallengeOperation.current === context) activeChallengeOperation.current = null;
          return;
        }
        if (!terminalObserved && !controller.signal.aborted && activeChallengeOperation.current === context && asyncGeneration.current.isCurrent(token)) {
          dispatch({ type: "api_error", notice: context.language === "en" ? "The operation stream ended before a terminal receipt. Recover to replay durable events." : "操作事件流在终态回执前结束；请恢复以重放持久化事件。" });
        }
      } catch (error) {
        if (!wasAborted(error) && activeChallengeOperation.current === context && asyncGeneration.current.isCurrent(token)) dispatch({ type: "api_error", notice: apiErrorNotice(error, context.language) });
      } finally {
        if (challengeStreamAbort.current === controller) challengeStreamAbort.current = null;
      }
    })();
  }, [apiClient]);

  const startChallenge = useCallback(() => {
    const token = invalidateAsync();
    dispatch({ type: "start_challenge", server: Boolean(apiClient) });
    if (!apiClient) {
      challengeTimer.current = window.setTimeout(() => { if (asyncGeneration.current.isCurrent(token)) dispatch({ type: "challenge_complete" }); }, 900);
      return;
    }
    const controller = new AbortController();
    apiAbort.current = controller;
    void (async () => {
      let pending: PendingChallengeReceipt | null = null;
      try {
        const session = await apiClient.createSession({ client_locale: state.language === "en" ? "en-US" : "zh-CN" }, { signal: controller.signal });
        if (!asyncGeneration.current.isCurrent(token)) return;
        dispatch({ type: "api_session_ready", sessionId: session.session.session_id });
        const request = { session_id: session.session.session_id, thesis: state.viewpoint.trim(), capital, base_currency: state.currency, user_private_evidence_claims: false } as const;
        pending = { cancelRequested: false, cancelIdempotencyKey: createIdempotencyKey(), submissionIdempotencyKey: createIdempotencyKey() };
        pendingChallengeReceipts.current.add(pending);
        const acceptReceipt = async (receipt: { operation: Operation }, receiptToken: number) => {
          pendingChallengeReceipts.current.delete(pending!);
          const operation: ChallengeOperationContext = { operation: receipt.operation, phase: "initial", viewpoint: state.viewpoint, language: state.language, replyBody: "", priorTurns: [], restart: { kind: "initial", request }, lastEventId: operationReplayCursor(receipt.operation), cancelled: pending!.cancelRequested, cancelIdempotencyKey: pending!.cancelIdempotencyKey };
          activeChallengeOperation.current = operation;
          dispatch({type:"api_operation_pending",operationId:receipt.operation.operation_id,phase:"initial"});
          if (pending!.cancelRequested) {
            try {
              const terminal = await cancelAcceptedPendingReceipt(apiClient, pending!, receipt);
              if (terminal) { operation.operation = terminal; operation.lastEventId = terminal.last_event_id; }
              dispatch({ type: "api_cancel" });
            } catch (error) {
              dispatch({ type: "api_error", notice: apiErrorNotice(error, state.language) });
            }
            return;
          }
          if (asyncGeneration.current.isCurrent(receiptToken)) streamChallengeOperation(operation, receiptToken);
        };
        pending.recover = async (receiptToken) => acceptReceipt(await replayPendingReceipt(
          pending!, (idempotencyKey) => apiClient.startChallenge(request, { idempotencyKey }),
        ), receiptToken);
        await acceptReceipt(
          await replayPendingReceipt(pending, (idempotencyKey) => apiClient.startChallenge(request, { signal: controller.signal, idempotencyKey })), token,
        );
      } catch (error) {
        if (asyncGeneration.current.isCurrent(token) || pending?.cancelRequested) dispatch({ type: "api_error", notice: apiErrorNotice(error, state.language) });
      } finally {
        if (apiAbort.current === controller) apiAbort.current = null;
      }
    })();
  }, [apiClient, capital, invalidateAsync, state.currency, state.language, state.viewpoint, streamChallengeOperation]);
  const cancelChallenge = useCallback(() => {
    if (!apiClient) { invalidateAsync(); dispatch({ type: "challenge_cancel" }); return; }
    if (pendingChallengeReceipts.current.size > 0) {
      for (const pending of pendingChallengeReceipts.current) pending.cancelRequested = true;
      asyncGeneration.current.invalidate();
      dispatch({ type: "api_cancel" });
      return;
    }
    const operation = activeChallengeOperation.current;
    if (!operation) {
      asyncGeneration.current.invalidate();
      apiAbort.current?.abort();
      apiAbort.current = null;
      dispatch({ type: "api_cancel" });
      return;
    }
    operation.cancelled = true;
    const token = asyncGeneration.current.invalidate();
    void apiClient.cancelOperation(operation.operation.operation_id, operation.operation.cancel_token, { idempotencyKey: operation.cancelIdempotencyKey }).then(() => {
      if (!asyncGeneration.current.isCurrent(token)) return;
      challengeStreamAbort.current?.abort();
      dispatch({ type: "api_cancel" });
    }).catch((error) => {
      if (!asyncGeneration.current.isCurrent(token)) return;
      challengeStreamAbort.current?.abort();
      dispatch({ type: "api_error", notice: apiErrorNotice(error, state.language) });
    });
  }, [apiClient, invalidateAsync, state.language]);
  const recoverChallenge = useCallback(() => {
    if (!apiClient) {
      const token = invalidateAsync();
      dispatch({ type: "challenge_recover" });
      challengeTimer.current = window.setTimeout(() => { if (asyncGeneration.current.isCurrent(token)) dispatch({ type: "challenge_complete" }); }, 700);
      return;
    }
    const unresolvedReceipt = [...pendingChallengeReceipts.current].find((pending) => pending.recover !== undefined);
    if (unresolvedReceipt?.recover && !recoveryInFlight.current) {
      const token = asyncGeneration.current.current();
      recoveryInFlight.current = true;
      dispatch({ type: "challenge_recover" });
      void unresolvedReceipt.recover(token).catch((error) => {
        dispatch({ type: "api_error", notice: apiErrorNotice(error, state.language) });
      }).finally(() => { recoveryInFlight.current = false; });
      return;
    }
    const operation = activeChallengeOperation.current;
    if (!operation || recoveryInFlight.current) return;
    challengeStreamAbort.current?.abort();
    challengeStreamAbort.current = null;
    const token = asyncGeneration.current.current();
    operation.cancelled = false;
    recoveryInFlight.current = true;
    dispatch({ type: "challenge_recover" });
    void apiClient.resumeOperation(operation.operation.operation_id, operation.operation.resume_token).then((resumed) => {
      if (!asyncGeneration.current.isCurrent(token)) return;
      const action = recoveryActionFor(resumed.operation.status);
      if (action === "resume") {
        streamChallengeOperation(operation, token);
        return;
      }
      if (action === "replay") {
        operation.lastEventId = `${operation.operation.operation_id}:0`;
        streamChallengeOperation(operation, token);
        return;
      }
      if (action !== "restart") return;
      const request = operation.restart;
      const pending: PendingChallengeReceipt = { cancelRequested: false, cancelIdempotencyKey: createIdempotencyKey(), submissionIdempotencyKey: createIdempotencyKey() };
      pendingChallengeReceipts.current.add(pending);
      const replayStart = () => replayPendingReceipt(pending, (idempotencyKey) => request.kind === "initial"
        ? apiClient.startChallenge(request.request, { idempotencyKey })
        : apiClient.startChallengeReply(request.challengeId, { message: request.message }, { idempotencyKey }));
      const acceptRestart = async (receipt: { operation: Operation }, receiptToken: number) => {
        pendingChallengeReceipts.current.delete(pending);
        const replacement: ChallengeOperationContext = { ...operation, operation: receipt.operation, priorTurns: challengeTurnsRef.current, lastEventId: operationReplayCursor(receipt.operation), cancelled: pending.cancelRequested, cancelIdempotencyKey: pending.cancelIdempotencyKey };
        activeChallengeOperation.current = replacement;
        dispatch({type:"api_operation_pending",operationId:receipt.operation.operation_id,phase:replacement.phase});
        if (pending.cancelRequested) {
          try {
            const terminal = await cancelAcceptedPendingReceipt(apiClient, pending, receipt);
            if (terminal) { replacement.operation = terminal; replacement.lastEventId = terminal.last_event_id; }
            dispatch({ type: "api_cancel" });
          } catch (error) {
            dispatch({ type: "api_error", notice: apiErrorNotice(error, state.language) });
          }
          return;
        }
        if (asyncGeneration.current.isCurrent(receiptToken)) streamChallengeOperation(replacement, receiptToken);
      };
      pending.recover = async (receiptToken) => acceptRestart(await replayStart(), receiptToken);
      return pending.recover(token);
    }).catch((error) => { dispatch({ type: "api_error", notice: apiErrorNotice(error, state.language) }); }).finally(() => { recoveryInFlight.current = false; });
  }, [apiClient, invalidateAsync, state.language, streamChallengeOperation]);
  const retryChallenge = useCallback(() => { recoverChallenge(); }, [recoverChallenge]);
  const submitReply = useCallback((body: string) => {
    if (isReviewLocked(state)) return;
    const text = body.trim();
    if (!apiClient || !apiChallengeId) { dispatch({ type: "submit_reply", body }); return; }
    if (!text || replyInFlight.current) return;
    const token = asyncGeneration.current.current();
    replyInFlight.current = true;
    dispatch({ type: "begin_reply_submit", body: text });
    const pending: PendingChallengeReceipt = { cancelRequested: false, cancelIdempotencyKey: createIdempotencyKey(), submissionIdempotencyKey: createIdempotencyKey() };
    pendingChallengeReceipts.current.add(pending);
    const acceptReply = async (receipt: { operation: Operation }, receiptToken: number) => {
      pendingChallengeReceipts.current.delete(pending);
      const operation: ChallengeOperationContext = { operation: receipt.operation, phase: "reply", viewpoint: state.viewpoint, language: state.language, replyBody: text, priorTurns: challengeTurnsRef.current, restart: { kind: "reply", challengeId: apiChallengeId, message: text }, lastEventId: operationReplayCursor(receipt.operation), cancelled: pending.cancelRequested, cancelIdempotencyKey: pending.cancelIdempotencyKey };
      activeChallengeOperation.current = operation;
      dispatch({type:"api_operation_pending",operationId:receipt.operation.operation_id,phase:"reply"});
      if (pending.cancelRequested) {
        try {
          const terminal = await cancelAcceptedPendingReceipt(apiClient, pending, receipt);
          if (terminal) { operation.operation = terminal; operation.lastEventId = terminal.last_event_id; }
          dispatch({ type: "api_cancel" });
        } catch (error) {
          dispatch({ type: "api_error", notice: apiErrorNotice(error, state.language) });
        }
        return;
      }
      if (asyncGeneration.current.isCurrent(receiptToken)) streamChallengeOperation(operation, receiptToken);
    };
    pending.recover = async (receiptToken) => acceptReply(await replayPendingReceipt(
      pending, (idempotencyKey) => apiClient.startChallengeReply(apiChallengeId, { message: text }, { idempotencyKey }),
    ), receiptToken);
    void pending.recover(token).catch((error) => { dispatch({ type: "api_error", notice: apiErrorNotice(error, state.language) }); }).finally(() => {
      replyInFlight.current = false;
    });
  }, [apiChallengeId, apiClient, state.language, state.viewpoint, state.confirmation, state.reviewConfirmation, streamChallengeOperation]);
  const addEvidence = useCallback((draft: EvidenceDraft) => {
    if (isReviewLocked(state)) return;
    if (Object.keys(validateSearchEvidence(draft)).length > 0) return;
    const searchProvider = draft.search_provider?.trim();
    const searchResultHash = draft.search_result_hash?.trim();
    if (Boolean(searchProvider) !== Boolean(searchResultHash) || (searchProvider && (searchProvider.length > 100 || !searchResultHash || !/^[a-f0-9]{64}$/.test(searchResultHash)))) return;
    const retrievedAt = new Date().toISOString();
    const sourceType: EvidenceView["source_type"] = draft.grade === "A" ? "primary" : draft.grade === "B" ? "reliable_secondary" : draft.grade === "C" ? "commentary" : draft.grade === "D" ? "user_private" : "model_inference";
    const evidenceId = `evidence-user-${state.evidence.length + 1}`;
    const challengeId = apiChallengeId ?? "challenge-web-demo";
    const url = draft.url.trim();
    const title = draft.title.trim();
    const publisher = draft.publisher.trim();
    const publishedAt = draft.published_at.trim();
    const excerpt = draft.excerpt.trim();
    const userNote = draft.user_note.trim();
    const independentlyReviewable = isEvidenceIndependentlyReviewable(draft.grade, sourceType, url);
    const candidateProvenance = searchProvider && searchResultHash ? { search_provider: searchProvider, search_result_hash: searchResultHash } : {};
    const evidence: EvidenceView = {
      content_hash: evidenceHash({ evidence_id: evidenceId, challenge_id: challengeId, source_type: sourceType, url, title, publisher, published_at: publishedAt, retrieved_at: retrievedAt, excerpt, user_note: userNote, grade: draft.grade, supports_or_challenges: draft.supports_or_challenges, independently_reviewable: independentlyReviewable, ...candidateProvenance }),
      evidence_id: evidenceId,
      challenge_id: challengeId,
      source_type: sourceType,
      url,
      title,
      publisher,
      published_at: publishedAt,
      retrieved_at: retrievedAt,
      excerpt,
      user_note: userNote,
      grade: draft.grade,
      supports_or_challenges: draft.supports_or_challenges,
      independently_reviewable: independentlyReviewable,
      sourceLabel: publisher,
      ...candidateProvenance,
    };
    if (!apiClient || !apiChallengeId) { dispatch({ type: "add_evidence", evidence }); return; }
    if (evidenceSubmissionInFlight.current) return;
    const request: Parameters<typeof apiClient.submitEvidence>[1] = {
      title,
      retrieved_at: retrievedAt,
      excerpt,
      user_note: userNote,
      content_hash: sourceMaterialHash({
        url: url || null,
        title,
        published_at: publishedAt || null,
        retrieved_at: retrievedAt,
        excerpt,
        user_note: userNote,
        search_provider: searchProvider || null,
        search_result_hash: searchResultHash || null,
      }),
      ...(url ? { url } : {}),
      ...(publisher ? { publisher } : {}),
      ...(publishedAt ? { published_at: publishedAt } : {}),
      ...candidateProvenance,
    };
    const controller = new AbortController();
    apiAbort.current?.abort();
    apiAbort.current = controller;
    evidenceSubmissionInFlight.current = true;
    setEvidenceSubmitting(true);
    void apiClient.submitEvidence(apiChallengeId, request, { signal: controller.signal }).then((response) => {
      dispatch({ type: "add_evidence", evidence });
      dispatch({type:"api_operation_finished",operationId:response.operation.operation_id});
      dispatch({ type: "api_challenge_update", ...challengeToUi(response.challenge, state.viewpoint, state.language, "evidence", "", challengeTurnsRef.current) });
    }).catch((error) => { if (!controller.signal.aborted) dispatch({ type: "api_error", notice: apiErrorNotice(error, state.language) }); }).finally(() => {
      if (apiAbort.current === controller) apiAbort.current = null;
      evidenceSubmissionInFlight.current = false;
      setEvidenceSubmitting(false);
    });
  }, [apiChallengeId, apiClient, state.evidence.length, state.language, state.viewpoint, state.confirmation, state.reviewConfirmation]);

  const computeFm = useCallback(() => {
    if (!canStartFm(state)) return;
    const token = invalidateAsync();
    const controller = new AbortController(); fmAbort.current = controller;
    dispatch({ type: "fm_start" });
    const request = { assetIds: ASSET_FIXTURES.map((asset) => asset.assetId), cutoff: marketConfig.mode === "mock" ? "2026-08-09" : new Date().toISOString(), timezone: "UTC" } as const;
    void (async () => {
      const bundle = await marketRuntime.refresh(request);
      if (controller.signal.aborted || !asyncGeneration.current.isCurrent(token)) return null;
      setMarketAssets(assetsForMarketBundle(bundle));
      return runFmInWorker(fmInput(bundle), { signal: controller.signal, onProgress: (progress, step) => { if (asyncGeneration.current.isCurrent(token)) dispatch({ type: "fm_progress", progress, step }); } });
    })().then((result) => { if (result && !controller.signal.aborted && asyncGeneration.current.isCurrent(token)) dispatch({ type: "fm_complete", result }); }).catch(() => { if (!controller.signal.aborted && asyncGeneration.current.isCurrent(token)) dispatch({ type: "fm_fail" }); });
  }, [invalidateAsync, marketConfig.mode, marketRuntime, state]);
  const cancelFm = useCallback(() => { invalidateAsync(); dispatch({ type: "fm_cancel" }); }, [invalidateAsync]);
  const studyBusy = studySwitching || worldlineSubmitting || evidenceSubmitting || state.fmStatus === "computing" || ["streaming","submitting_reply","recovering"].includes(state.challengeStatus);
  const archiveCurrentStudy = useCallback((): boolean => {
    if (studyBusy) return false;
    try {
      // If server re-verification failed, preserve the complete stored object,
      // not the reduced text-only fallback currently rendered in memory.
      const snapshot = restoreFailure ? window.localStorage.getItem(workspaceKey(researchScope)) : encodeWorkspace(researchScope,state,marketAssets);
      if (!snapshot) throw new Error("No restorable snapshot");
      const entries=saveStudy(window.localStorage,researchScope,snapshot);
      setStudyLibrary({scope:researchScope,entries}); return true;
    } catch {
      setStudyMessage({text:state.language === "en" ? "Research was not archived (storage full or invalid record). The current study was not replaced; export it first." : "研究归档失败（空间不足或记录异常）。当前研究未被替换，请先导出。",error:true}); return false;
    }
  }, [studyBusy,restoreFailure,researchScope,state,marketAssets]);
  const setViewpoint = useCallback((viewpoint: string) => {
    if (viewpoint !== state.viewpoint) {
      if (studyBusy || (hasResearchProgress(state) && !archiveCurrentStudy())) return;
      if (hasResearchProgress(state)) setStudyMessage({text:state.language === "en" ? "Previous research saved; this premise starts a new draft." : "原研究已保存；修改后的观点作为新草稿。",error:false});
      invalidateAsync();
    }
    dispatch({ type: "set_viewpoint", viewpoint });
  }, [invalidateAsync,state,studyBusy,archiveCurrentStudy]);
  const reset = useCallback(() => {
    if (!archiveCurrentStudy()) return;
    invalidateAsync(); restoredScope.current = researchScope; setRestoreFailure(false); setMarketAssets(ASSET_FIXTURES);
    dispatch({ type: "reset" }); setStudyMessage({text:state.language === "en" ? "New draft opened. Previous research remains in Saved research." : "已打开新草稿；原研究保留在“已存研究”中。",error:false});
  }, [invalidateAsync,researchScope,archiveCurrentStudy,state.language]);
  const openStudy = useCallback((id:string) => {
    if (studyBusy) return;
    let selected:StudyEntry|undefined;
    try { selected=readStudies(window.localStorage,researchScope).find(e=>e.id===id); } catch { /* handled below without replacing the current study */ }
    if (!selected) { setStudyMessage({text:state.language === "en" ? "Saved research is missing or invalid." : "所选研究缺失或记录异常。",error:true}); return; }
    if (!archiveCurrentStudy()) return;
    try { selected=readStudies(window.localStorage,researchScope).find(e=>e.id===id) ?? selected; }
    catch { setStudyMessage({text:state.language === "en" ? "Saved research changed; retry opening it." : "已存研究发生变化，请重新打开。",error:true}); return; }
    const token=invalidateAsync(); const controller=new AbortController(); studySwitchAbort.current=controller;
    setStudySwitching(true); setStudyMessage({text:"",error:false});
    void verifyWorkspace(selected.workspace,apiClient,controller.signal).then(restored=>{
      if (controller.signal.aborted || !asyncGeneration.current.isCurrent(token)) return;
      // Commit working-copy bytes BEFORE replacing the visible state. A quota
      // failure leaves both current UI and the immutable saved study available.
      window.localStorage.setItem(workspaceKey(researchScope),encodeWorkspace(researchScope,restored,selected!.workspace.assets));
      dispatch({type:"restore_workspace",state:restored}); setMarketAssets(selected!.workspace.assets);
      restoredScope.current=researchScope; setRestoreFailure(false); activeChallengeOperation.current=null;
      setStudyMessage({text:state.language === "en" ? "Research restored without regenerating results." : "已恢复研究，未重新生成结果。",error:false});
    }).catch(()=>{
      if (!controller.signal.aborted && asyncGeneration.current.isCurrent(token)) setStudyMessage({text:state.language === "en" ? "Restoration did not complete. Both studies are retained; no model request was submitted." : "研究恢复未完成。当前与已存研究均保留，未提交模型请求。",error:true});
    }).finally(()=>{ if (studySwitchAbort.current===controller) {studySwitchAbort.current=null;setStudySwitching(false);} });
  }, [studyBusy,researchScope,archiveCurrentStudy,invalidateAsync,apiClient,state.language]);
  const confirm = useCallback((confirmation: "confirmed" | "reserved") => {
    if (!apiClient || !apiChallengeId) { dispatch({ type: "confirm", confirmation }); return; }
    if (worldlineSubmission.current || !state.apiOperationId) return;
    worldlineSubmission.current = true;
    setWorldlineSubmitting(true);
    dispatch({type:"api_error",notice:""});
    const token = asyncGeneration.current.invalidate();
    const controller = new AbortController();
    apiAbort.current?.abort();
    apiAbort.current = controller;
    let acceptedConfirmation = confirmation;
    void completeWorldlineFlow(apiClient, apiChallengeId, {
      command: confirmation === "reserved" ? "CONFIRM_WITH_RESERVATIONS" : "CONFIRM",
      unresolved_issues_acknowledged: state.unresolvedIssuesAcknowledged,
      ...(confirmation === "reserved" ? { reservation_note: state.reservationNote } : {}),
    }, {operationId: state.apiOperationId, signal: controller.signal, storage: {getItem: (key) => window.localStorage.getItem(key), setItem: (key,value) => window.localStorage.setItem(key,value)}, onConfirmed: (receipt) => {
      if (receipt.challenge.status !== "CONFIRMED" || !["confirmed", "confirmed_with_reservations"].includes(receipt.challenge.user_confirmation)) throw new Error("Invalid confirmation receipt");
      acceptedConfirmation = receipt.challenge.user_confirmation === "confirmed_with_reservations" ? "reserved" : "confirmed";
      if (asyncGeneration.current.isCurrent(token)) {
        dispatch({type:"api_review_confirmed",confirmation:acceptedConfirmation});
        dispatch({type:"api_operation_finished",operationId:receipt.operation.operation_id});
      }
    }}).then((response) => {
      if (controller.signal.aborted || !asyncGeneration.current.isCurrent(token)) return;
      const branches = worldlineBundleToBranches(response.bundle, state.language);
      if (branches.length !== 3) throw new Error("API worldline data is unavailable.");
      dispatch({ type: "confirm", confirmation: acceptedConfirmation });
      dispatch({ type: "api_worldlines_ready", branches });
    }).catch((error) => {
      if (!controller.signal.aborted && asyncGeneration.current.isCurrent(token)) dispatch({ type: "api_error", notice: apiErrorNotice(error, state.language) });
    }).finally(() => { worldlineSubmission.current = false; setWorldlineSubmitting(false); });
  }, [apiChallengeId, apiClient, state.apiOperationId, state.language, state.reservationNote, state.unresolvedIssuesAcknowledged]);
  const navigateTo = useCallback((stage: Stage) => { if (!canEnterStage(state, stage)) { dispatch({ type: "set_stage", stage }); return; } if (stage !== state.stage) invalidateAsync(); dispatch({ type: "set_stage", stage }); }, [invalidateAsync, state]);

  const exportJson = useCallback(() => { downloadText("foresightnine-research.json", JSON.stringify(researchExport(state, viewpointBranches, rows, marketDataAsOf, navDraft.values), null, 2), "application/json;charset=utf-8"); dispatch({ type: "export_notice", notice: "JSON 已导出；当前观点、挑战回复和证据均保留。" }); }, [marketDataAsOf, rows, state, viewpointBranches, navDraft.values]);
  const exportCsv = useCallback(() => { downloadText("foresightnine-allocation.csv", csvExport(rows), "text/csv;charset=utf-8"); dispatch({ type: "export_notice", notice: "CSV 已导出；这是 allocation-only 快照，完整审计字段请使用 JSON。" }); }, [rows]);
  const exportPath = useCallback(() => { downloadText("foresightnine-worldline-nav.json", JSON.stringify(navExport(state, viewpointBranches, marketDataAsOf, rows, navDraft.values), null, 2), "application/json;charset=utf-8"); dispatch({ type: "export_notice", notice: "路径数据已导出；包含实际配置、用户假设、日期与计算方式。" }); }, [marketDataAsOf, state, viewpointBranches, rows, navDraft.values]);
  const methodsExport = useCallback(() => { downloadText(methodsGuideFilename(state.language), buildMethodsGuide(state.language), "text/plain;charset=utf-8"); dispatch({ type: "export_notice", notice: state.language === "en" ? "Complete user guide exported." : "完整使用说明已导出。" }); }, [state.language]);

  const workspaceReady = draftHydrated && (restoredScope.current === researchScope || restoreFailure);
  const visibleStage = safeStageFor(state);
  const content = renderStage(visibleStage, state, dispatch, { startChallenge, cancelChallenge, retryChallenge, recoverChallenge, addEvidence, evidenceSubmitting, worldlineSubmitting, submitReply, confirm, computeFm, cancelFm, exportJson, exportCsv, exportPath, methodsExport, rows, viewpointBranches, navData, navDraft, setViewpoint, reset }, oauthReadiness, { session: oauthSession, onStart: oauthReturnUri ? startOAuth : undefined, onLogout: logoutOAuth, onDelete: deleteOAuth, billingSummary, apiBaseUrl });
  const inspector = renderInspector(visibleStage, state, rows, () => dispatch({ type: "toggle_drawdown" }), state.language);
  return <LanguageProvider language={state.language} setLanguage={(language) => dispatch({ type: "set_language", language })}><AppShell stage={visibleStage} onStage={navigateTo} canNavigate={(stage) => workspaceReady && !studySwitching && canEnterStage(state, stage)} inspector={workspaceReady && !studySwitching ? inspector : undefined} oauthProviders={oauthReadiness.providers} oauthReadinessStatus={oauthReadiness.status} onOAuthStart={oauthReturnUri ? startOAuth : undefined} oauthSession={oauthSession} onOAuthLogout={logoutOAuth} onOAuthDelete={deleteOAuth} billingSummary={billingSummary}><div className="top-status"><span><StatusDot tone={apiMode === "api" ? "blue" : "sage"} /> {t(state.language, apiMode === "api" ? "API 模式 · 服务端挑战" : "离线/模拟模式 · 无远程模型")}</span>{oauthNotice ? <span className="export-notice" role="alert">{oauthNotice}</span> : null}{state.apiError ? <span className="export-notice" role="alert">{state.apiError}</span> : null}{state.exportNotice ? <span className="export-notice" role="status">{t(state.language, state.exportNotice)}</span> : null}</div>{workspaceReady ? <StudySwitcher entries={studyLibrary.scope === researchScope ? studyLibrary.entries : []} busy={studyBusy} onOpen={openStudy} onSave={() => {if (archiveCurrentStudy()) setStudyMessage({text:state.language === "en" ? "Research saved on this device." : "研究已保存在此设备。",error:false});}} onNew={reset} notice={studyMessage.text} error={studyMessage.error}/> : null}{!workspaceReady || studySwitching ? <p role="status">{state.language === "en" ? "Restoring research…" : "正在恢复研究…"}</p> : restoreFailure ? <section role="alert"><p>{state.language === "en" ? "The saved research could not be verified. Its local copy is retained; no model request was resubmitted." : "暂未验证已保存的研究。本地副本仍保留，未重新提交模型请求。"}</p><Button onClick={() => setRestoreRetry(n => n + 1)}>{state.language === "en" ? "Retry restoration" : "重新恢复研究"}</Button><Button onClick={reset}>{state.language === "en" ? "Start a new research" : "新建研究"}</Button></section> : content}{visibleStage === "thesis" ? <SocialLoginPanel className="mobile-social-login" providers={oauthReadiness.providers} readinessStatus={oauthReadiness.status} onStart={oauthReturnUri ? startOAuth : undefined} session={oauthSession} onLogout={logoutOAuth} onDelete={deleteOAuth} billingSummary={billingSummary} /> : null}</AppShell></LanguageProvider>;
}

interface Handlers { readonly navData: NavExportData; readonly navDraft: ReturnType<typeof useNavAssumptions>; readonly startChallenge: () => void; readonly cancelChallenge: () => void; readonly retryChallenge: () => void; readonly recoverChallenge: () => void; readonly addEvidence: (draft: EvidenceDraft) => void; readonly evidenceSubmitting: boolean; readonly worldlineSubmitting: boolean; readonly submitReply: (body: string) => void; readonly confirm: (confirmation: "confirmed" | "reserved") => void; readonly computeFm: () => void; readonly cancelFm: () => void; readonly exportJson: () => void; readonly exportCsv: () => void; readonly exportPath: () => void; readonly methodsExport: () => void; readonly rows: ReturnType<typeof allocationRows>; readonly viewpointBranches: ReturnType<typeof makeBranches>; readonly setViewpoint: (viewpoint: string) => void; readonly reset: () => void; }

interface OAuthHandlers { readonly session: OAuthSession | null; readonly onStart?: ((provider: "google" | "github") => void) | undefined; readonly onLogout: () => void; readonly onDelete: () => void; readonly billingSummary: AccountBillingSummary | null; readonly apiBaseUrl: string | null; }

function renderStage(stage: Stage, state: ReturnType<typeof reducer>, dispatch: React.Dispatch<Parameters<typeof reducer>[1]>, handlers: Handlers, oauthReadiness: OAuthReadiness, oauth: OAuthHandlers) {
  switch (stage) {
    case "thesis": return <ThesisInput hasExistingReview={hasResearchProgress(state)} willArchiveOnEdit={hasResearchProgress(state)} viewpoint={state.viewpoint} capital={state.capital} currency={state.currency} draftRestored={state.draftRestored} onViewpoint={handlers.setViewpoint} onCapital={(capital) => dispatch({ type: "set_capital", capital })} onCurrency={(currency) => dispatch({ type: "set_currency", currency: currency as Currency })} onStart={hasResearchProgress(state) ? () => dispatch({type:"set_stage",stage:"challenge"}) : handlers.startChallenge} onReset={handlers.reset} />;
    case "challenge": return <ChallengePanel confirmation={state.reviewConfirmation !== "pending" ? state.reviewConfirmation : state.confirmation} worldlinesReady={Boolean(state.serverBranches) || (!state.apiChallengeId && state.confirmation !== "pending")} onOpenWorldlines={() => dispatch({type:"set_stage",stage:"worldline"})} savedEvidenceDraft={state.evidenceDraft} onEvidenceDraftChange={(draft) => dispatch({type:"set_evidence_draft",draft})} savedSearchQuery={state.evidenceSearchQuery} onSearchQueryChange={(query) => dispatch({type:"set_evidence_query",query})} status={state.challengeStatus} turns={state.challengeTurns} replyDraft={state.replyDraft} evidence={state.evidence} rubric={state.rubric} evidenceSubmitting={handlers.evidenceSubmitting} worldlineSubmitting={handlers.worldlineSubmitting} unresolvedIssuesAcknowledged={state.unresolvedIssuesAcknowledged} reservationNote={state.reservationNote} onStart={handlers.startChallenge} onCancel={handlers.cancelChallenge} onRetry={handlers.retryChallenge} onRecover={handlers.recoverChallenge} onReplyDraft={(draft) => dispatch({ type: "set_reply_draft", draft })} onSubmitReply={handlers.submitReply} onEvidence={handlers.addEvidence} onUnresolvedIssuesAcknowledged={(acknowledged) => dispatch({ type: "set_unresolved_issues_acknowledged", acknowledged })} onReservationNote={(note) => dispatch({ type: "set_reservation_note", note })} onConfirm={handlers.confirm} />;
    case "worldline": return <WorldlinePanel branches={handlers.viewpointBranches} selected={state.selectedBranch} onSelect={(branch) => dispatch({ type: "select_branch", branch })} onCompute={state.fmStatus === "complete" && state.fmResult ? () => dispatch({type:"set_stage",stage:"fm"}) : handlers.computeFm} canCompute={canStartFm(state)} generating={handlers.worldlineSubmitting} onRegenerate={state.apiChallengeId ? () => handlers.confirm(state.confirmation === "reserved" ? "reserved" : "confirmed") : undefined} />;
    case "fm": return <FmPanel status={state.fmStatus} progress={state.fmProgress} step={state.fmStep} resultReady={Boolean(state.fmResult)} result={state.fmResult} onCompute={handlers.computeFm} onCancel={handlers.cancelFm} onPortfolio={() => dispatch({ type: "set_stage", stage: "portfolio" })} />;
    case "portfolio": return <PortfolioPanel navData={handlers.navData} mode={state.portfolioMode} currency={state.currency} rows={handlers.rows} capital={parseCapital(state.capital)} selectedBranch={state.selectedBranch} branches={handlers.viewpointBranches} stress={state.stressScenario} onMode={(mode) => dispatch({ type: "set_mode", mode })} onStress={(scenario) => dispatch({ type: "set_stress", scenario })} onCompute={handlers.computeFm} onNav={() => { dispatch({type:"select_branch",branch:state.stressScenario}); dispatch({ type: "set_stage", stage: "nav" }); }} onExport={(kind) => kind === "json" ? handlers.exportJson() : handlers.exportCsv()} fmReady={Boolean(state.fmResult)} canCompute={canStartFm(state)} canNavigate={canEnterStage(state, "nav")} />;
    case "nav": return <NavPanel data={handlers.navData} assumptions={handlers.navDraft.values} onAssumption={handlers.navDraft.setValue} saveError={handlers.navDraft.saveError} branches={handlers.viewpointBranches} selected={state.selectedBranch} onSelect={(branch) => dispatch({ type: "select_branch", branch })} stress={state.stressScenario} onExport={handlers.exportPath} drawdownVisible={state.drawdownVisible} onToggleDrawdown={() => dispatch({ type: "toggle_drawdown" })} />;
    case "console": return <ConsolePanel onExport={handlers.methodsExport} providers={oauthReadiness.providers} readinessStatus={oauthReadiness.status} onOAuthStart={oauth.onStart} oauthSession={oauth.session} onOAuthLogout={oauth.onLogout} onOAuthDelete={oauth.onDelete} billingSummary={oauth.billingSummary} apiBaseUrl={oauth.apiBaseUrl} />;
  }
}


function renderInspector(stage: Stage, state: ReturnType<typeof reducer>, rows: ReturnType<typeof allocationRows>, onToggleDrawdown: () => void, language: Language) {
  if (stage === "challenge") return <div className="inspector-content"><h2>{t(language, "证据工作台")}</h2><p>{t(language, "来源等级表示来源与可核验性，不代表结论正确。")}</p>{state.evidence.map((item) => <div className="inspector-row" key={item.evidence_id}><Tag tone={item.grade === "E" ? "warning" : "strong"}>{item.grade}</Tag><span>{item.title}</span></div>)}<Disclosure compact /></div>;
  if (stage === "worldline") return <WorldlineExplanation branch={state.serverBranches?.find(branch => branch.kind === state.selectedBranch)} />;
  if (stage === "fm") return <FmExplanation result={state.fmStatus === "complete" ? state.fmResult : null} />;
  if (stage === "portfolio") return <div className="inspector-content"><h2>{t(language, "配置说明")}</h2>{state.fmResult ? <><p>{language === "en" ? "FM weights use market factors; worldline probabilities do not reweight this allocation." : "FM 权重来自市场因子；世界线概率不会重新加权当前配置。"}</p><hr /><h3>{t(language, "世界线暴露")}</h3><p>{t(language, "当前选择：")}{t(language, state.selectedBranch === "main" ? "主情景 W₀" : state.selectedBranch === "weak" ? "弱情景 W−" : "强情景 W+")}</p><p>{t(language, "残余现金：")}{currencySymbol(state.currency)}{Math.max(0, parseCapital(state.capital) - rows.reduce((sum, row) => sum + row.amount, 0)).toLocaleString(language === "en" ? "en-US" : state.currency === "USD" ? "en-US" : state.currency === "EUR" ? "de-DE" : "zh-CN")}</p></> : <p role="status">{t(language, "FM 尚未计算；暂无配置权重、金额、单位或残余现金结果。")}</p>}<Disclosure /></div>;
  if (stage === "nav") return <div className="inspector-content"><h2>{t(language, "路径假设")}</h2><p>{language === "en" ? "Changing the scenario changes the assumptions shown, not your FM allocation." : "切换情景可填写不同假设，不会改变 FM 配置。"}</p><p>{language === "en" ? "Asset quantities and residual cash come from the current allocation." : "资产数量与剩余现金来自当前配置方案。"}</p><Disclosure compact /></div>;
  return <div className="inspector-content"><h2>{t(language, "研究档案")}</h2><p>{t(language, "公开研究模式保留从观点、证据到组合路径的审计线索。")}</p><div className="archive-status"><StatusDot tone="sage" /> {t(language, "本地保存")}</div><div className="archive-status"><StatusDot tone="blue" /> {state.evidence.length} {t(language, "条证据")}</div><Disclosure /></div>;
}
