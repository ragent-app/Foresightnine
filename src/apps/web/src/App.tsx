import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { AppShell, Disclosure } from "./components/AppShell";
import { LanguageProvider, t } from "./i18n";
import { Button, StatusDot, Tag } from "./components/Controls";
import { ThesisInput } from "./components/ThesisInput";
import { ChallengePanel } from "./components/ChallengePanel";
import { WorldlinePanel } from "./components/WorldlinePanel";
import { FmPanel } from "./components/FmPanel";
import { PortfolioPanel } from "./components/PortfolioPanel";
import { NavPanel } from "./components/NavPanel";
import { ConsolePanel } from "./components/MethodsPanel";
import { SocialLoginPanel, type OAuthProviderAvailability, type OAuthReadinessStatus } from "./components/SocialLoginPanel";
import { ASSET_FIXTURES, assetsForMarketBundle, evidenceHash, fmInput, isEvidenceIndependentlyReviewable, makeBranches, MARKET_DATA_AS_OF } from "./fixtures";
import { allocationRows, canEnterStage, canStartFm, initialState, reducer, safeStageFor } from "./state";
import { downloadText, csvExport, navExport, researchExport } from "./exports";
import { currencySymbol, parseCapital, validateSearchEvidence } from "./validation";
import { runFmInWorker } from "./fm-client";
import { apiErrorNotice, challengeToUi, configuredApiBaseUrl, createWebApiClient, webApiMode, worldlineBundleToBranches } from "./api-runtime";
import { configuredMarketDataRuntimeConfig } from "./runtime-config";
import { createMarketRuntimeController } from "@foresightnine/market-data";
import type { Currency, EvidenceDraft, EvidenceView, FreshnessContext, Language, Stage } from "./types";

const DRAFT_KEY = "foresightnine.web.draft.v1";
const OAUTH_PROVIDERS = ["google", "github"] as const;
const OAUTH_UNAVAILABLE: readonly OAuthProviderAvailability[] = OAUTH_PROVIDERS.map((provider) => ({ provider, enabled: false }));

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

/** A small testable token guard for timers, workers, and other async callbacks. */
export function createGenerationGuard(): GenerationGuard {
  let generation = 0;
  return { current: () => generation, invalidate: () => { generation += 1; return generation; }, isCurrent: (token) => token === generation };
}

export function App() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [draftHydrated, setDraftHydrated] = useState(false);
  const apiClient = useMemo(() => createWebApiClient(), []);
  const marketRuntime = useMemo(() => createMarketRuntimeController(), []);
  const marketConfig = useMemo(() => configuredMarketDataRuntimeConfig(), []);
  const [marketDataState, setMarketDataState] = useState(marketRuntime.getState());
  const [marketAssets, setMarketAssets] = useState(ASSET_FIXTURES);
  const marketDataAsOf = marketDataState.bundle?.cutoff ?? MARKET_DATA_AS_OF;
  const apiBaseUrl = configuredApiBaseUrl();
  const [oauthReadiness, setOAuthReadiness] = useState<OAuthReadiness>({ providers: OAUTH_UNAVAILABLE, status: apiBaseUrl ? "loading" : "offline" });
  const apiAbort = useRef<AbortController | null>(null);
  const apiMode = webApiMode();

  useEffect(() => {
    const unsubscribe = marketRuntime.subscribe(setMarketDataState);
    marketRuntime.configure(marketConfig);
    return unsubscribe;
  }, [marketConfig, marketRuntime]);

  useEffect(() => {
    const providerStatus: FreshnessContext["providerStatus"] = marketDataState.status === "ready" && marketDataState.mode !== "mock"
      ? "fresh"
      : marketDataState.status === "limited" ? "stale" : "unavailable";
    const runtimeAsOf = marketDataState.bundle?.cutoff ?? state.freshness.runtimeAsOf;
    if (state.freshness.providerStatus === providerStatus && state.freshness.runtimeAsOf === runtimeAsOf) return;
    dispatch({ type: "set_freshness", freshness: { runtimeAsOf, providerStatus } });
  }, [marketDataState, state.freshness.providerStatus, state.freshness.runtimeAsOf]);

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

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(DRAFT_KEY);
      if (saved) {
        const draft = JSON.parse(saved) as { viewpoint?: unknown; capital?: unknown; currency?: unknown; language?: unknown; updatedAt?: unknown };
        if (typeof draft.viewpoint === "string" && typeof draft.capital === "string" && (draft.currency === "CNY" || draft.currency === "USD" || draft.currency === "EUR")) {
          dispatch({ type: "restore_draft", draft: { viewpoint: draft.viewpoint, capital: draft.capital, currency: draft.currency as Currency, updatedAt: typeof draft.updatedAt === "string" ? draft.updatedAt : new Date().toISOString() } });
          if (draft.language === "zh" || draft.language === "en") dispatch({ type: "set_language", language: draft.language as Language });
        }
      }
    } catch { /* local draft is optional */ }
    finally { setDraftHydrated(true); }
  }, []);

  useEffect(() => {
    if (!draftHydrated) return;
    try { window.localStorage.setItem(DRAFT_KEY, JSON.stringify({ viewpoint: state.viewpoint, capital: state.capital, currency: state.currency, language: state.language, updatedAt: new Date().toISOString() })); } catch { /* offline storage is optional */ }
  }, [draftHydrated, state.viewpoint, state.capital, state.currency, state.language]);

  const invalidateAsync = useCallback((): number => {
    if (challengeTimer.current !== null) window.clearTimeout(challengeTimer.current);
    challengeTimer.current = null;
    fmAbort.current?.abort();
    fmAbort.current = null;
    apiAbort.current?.abort();
    apiAbort.current = null;
    return asyncGeneration.current.invalidate();
  }, []);

  useEffect(() => () => { invalidateAsync(); }, [invalidateAsync]);

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
      try {
        const session = await apiClient.createSession({ client_locale: state.language === "en" ? "en-US" : "zh-CN" }, { signal: controller.signal });
        if (!asyncGeneration.current.isCurrent(token)) return;
        dispatch({ type: "api_session_ready", sessionId: session.session.session_id });
        const challenge = await apiClient.createChallenge({ session_id: session.session.session_id, thesis: state.viewpoint.trim(), capital, base_currency: state.currency, user_private_evidence_claims: false }, { signal: controller.signal });
        if (!asyncGeneration.current.isCurrent(token)) return;
        const update = challengeToUi(challenge.challenge, state.viewpoint, state.language, "initial");
        dispatch({ type: "api_challenge_update", ...update });
      } catch (error) {
        if (asyncGeneration.current.isCurrent(token)) dispatch({ type: "api_error", notice: apiErrorNotice(error, state.language) });
      }
    })();
  }, [apiClient, capital, invalidateAsync, state.currency, state.language, state.viewpoint]);
  const cancelChallenge = useCallback(() => { invalidateAsync(); dispatch({ type: apiClient ? "api_cancel" : "challenge_cancel" }); }, [apiClient, invalidateAsync]);
  const retryChallenge = useCallback(() => { if (apiClient) { startChallenge(); return; } const token = invalidateAsync(); dispatch({ type: "challenge_recover" }); challengeTimer.current = window.setTimeout(() => { if (asyncGeneration.current.isCurrent(token)) dispatch({ type: "challenge_complete" }); }, 700); }, [apiClient, invalidateAsync, startChallenge]);
  const recoverChallenge = useCallback(() => { retryChallenge(); }, [retryChallenge]);
  const submitReply = useCallback((body: string) => {
    const text = body.trim();
    if (!apiClient || !apiChallengeId) { dispatch({ type: "submit_reply", body }); return; }
    const token = asyncGeneration.current.current();
    const controller = new AbortController();
    apiAbort.current?.abort();
    apiAbort.current = controller;
    void apiClient.submitChallengeReply(apiChallengeId, { message: text }, { signal: controller.signal }).then((response) => {
      if (!asyncGeneration.current.isCurrent(token)) return;
      const update = challengeToUi(response.challenge, state.viewpoint, state.language, "reply", text);
      dispatch({ type: "api_challenge_update", ...update });
    }).catch((error) => { if (asyncGeneration.current.isCurrent(token)) dispatch({ type: "api_error", notice: apiErrorNotice(error, state.language) }); });
  }, [apiChallengeId, apiClient, state.language, state.viewpoint]);
  const addEvidence = useCallback((draft: EvidenceDraft) => {
    if (Object.keys(validateSearchEvidence(draft)).length > 0) return;
    const searchProvider = draft.search_provider?.trim();
    const searchResultHash = draft.search_result_hash?.trim();
    if (Boolean(searchProvider) !== Boolean(searchResultHash) || (searchProvider && (searchProvider.length > 100 || !searchResultHash || !/^[a-f0-9]{64}$/.test(searchResultHash)))) return;
    const retrievedAt = new Date().toISOString();
    const sourceType: EvidenceView["source_type"] = draft.grade === "A" ? "primary" : draft.grade === "B" ? "reliable_secondary" : draft.grade === "C" ? "commentary" : draft.grade === "D" ? "user_private" : "model_inference";
    const evidenceId = `evidence-user-${state.evidence.length + 1}`;
    const challengeId = "challenge-web-demo";
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
    const request: Parameters<typeof apiClient.submitEvidence>[1] = { title, retrieved_at: retrievedAt, excerpt, user_note: userNote, content_hash: evidence.content_hash, ...(url ? { url } : {}), ...(publisher ? { publisher } : {}), ...(publishedAt ? { published_at: publishedAt } : {}), ...candidateProvenance };
    const controller = new AbortController();
    apiAbort.current?.abort();
    apiAbort.current = controller;
    void apiClient.submitEvidence(apiChallengeId, request, { signal: controller.signal }).then(() => dispatch({ type: "add_evidence", evidence })).catch((error) => dispatch({ type: "api_error", notice: apiErrorNotice(error, state.language) }));
  }, [apiChallengeId, apiClient, state.evidence.length, state.language]);

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
  const setViewpoint = useCallback((viewpoint: string) => { if (viewpoint !== state.viewpoint) invalidateAsync(); dispatch({ type: "set_viewpoint", viewpoint }); }, [invalidateAsync, state.viewpoint]);
  const reset = useCallback(() => { invalidateAsync(); dispatch({ type: "reset" }); }, [invalidateAsync]);
  const confirm = useCallback((confirmation: "confirmed" | "reserved") => {
    if (!apiClient || !apiChallengeId) { dispatch({ type: "confirm", confirmation }); return; }
    const controller = new AbortController();
    apiAbort.current?.abort();
    apiAbort.current = controller;
    void apiClient.confirmChallenge(apiChallengeId, { command: confirmation === "reserved" ? "CONFIRM_WITH_RESERVATIONS" : "CONFIRM", unresolved_issues_acknowledged: state.unresolvedIssuesAcknowledged, ...(confirmation === "reserved" ? { reservation_note: state.reservationNote } : {}) }, { signal: controller.signal }).then(() => apiClient.createWorldlines({ challenge_id: apiChallengeId, horizon_end: "2028-08-31" }, { signal: controller.signal })).then((response) => {
      const branches = worldlineBundleToBranches(response.bundle, state.language);
      if (branches.length !== 3) throw new Error("API worldline data is unavailable.");
      dispatch({ type: "confirm", confirmation });
      dispatch({ type: "api_worldlines_ready", branches });
    }).catch((error) => dispatch({ type: "api_error", notice: apiErrorNotice(error, state.language) }));
  }, [apiChallengeId, apiClient, state.language, state.reservationNote, state.unresolvedIssuesAcknowledged]);
  const navigateTo = useCallback((stage: Stage) => { if (!canEnterStage(state, stage)) { dispatch({ type: "set_stage", stage }); return; } if (stage !== state.stage) invalidateAsync(); dispatch({ type: "set_stage", stage }); }, [invalidateAsync, state]);

  const exportJson = useCallback(() => { downloadText("foresightnine-research.json", JSON.stringify(researchExport(state, viewpointBranches, rows, marketDataAsOf), null, 2), "application/json;charset=utf-8"); dispatch({ type: "export_notice", notice: "JSON 已导出；当前观点、挑战回复和证据均保留。" }); }, [marketDataAsOf, rows, state, viewpointBranches]);
  const exportCsv = useCallback(() => { downloadText("foresightnine-allocation.csv", csvExport(rows), "text/csv;charset=utf-8"); dispatch({ type: "export_notice", notice: "CSV 已导出；这是 allocation-only 快照，完整审计字段请使用 JSON。" }); }, [rows]);
  const exportPath = useCallback(() => { downloadText("foresightnine-worldline-nav.json", JSON.stringify(navExport(state, viewpointBranches, marketDataAsOf), null, 2), "application/json;charset=utf-8"); dispatch({ type: "export_notice", notice: "路径数据已导出，并标记了当前 premise、完整 NAV 数组与历史 / 模拟边界。" }); }, [marketDataAsOf, state, viewpointBranches]);
  const methodsExport = useCallback(() => { downloadText("foresightnine-methods.txt", state.language === "en" ? "ForesightNine methodology\n\nProbabilities are revisable judgments, not facts.\nScenario simulation is not a forecast or return promise.\n" : "ForesightNine 方法说明\n\n概率是可修订判断，不是事实。\n情景模拟，不是预测或收益承诺。\n", "text/plain;charset=utf-8"); dispatch({ type: "export_notice", notice: "方法说明已导出。" }); }, [state.language]);

  const visibleStage = safeStageFor(state);
  const content = renderStage(visibleStage, state, dispatch, { startChallenge, cancelChallenge, retryChallenge, recoverChallenge, addEvidence, submitReply, confirm, computeFm, cancelFm, exportJson, exportCsv, exportPath, methodsExport, rows, viewpointBranches, setViewpoint, reset }, oauthReadiness);
  const inspector = renderInspector(visibleStage, state, rows, () => dispatch({ type: "toggle_drawdown" }), state.language);
  const marketStatusLabel = state.language === "en" ? `Data: ${marketDataState.mode}/${marketDataState.status} · three anchors` : `数据：${marketDataState.mode}/${marketDataState.status} · 三个时点锚点`;
  return <LanguageProvider language={state.language} setLanguage={(language) => dispatch({ type: "set_language", language })}><AppShell stage={visibleStage} onStage={navigateTo} canNavigate={(stage) => canEnterStage(state, stage)} inspector={inspector} oauthProviders={oauthReadiness.providers} oauthReadinessStatus={oauthReadiness.status}><div className="top-status"><span><StatusDot tone={apiMode === "api" ? "blue" : "sage"} /> {t(state.language, apiMode === "api" ? "API 模式 · 服务端挑战" : "离线/模拟模式 · 无远程模型")}</span><span>{state.language === "en" ? `Data cutoff ${marketDataAsOf}` : `数据截止 ${marketDataAsOf}`}</span><span>{marketStatusLabel}</span>{state.apiError ? <span className="export-notice" role="alert">{state.apiError}</span> : null}{state.exportNotice ? <span className="export-notice" role="status">{t(state.language, state.exportNotice)}</span> : null}</div>{content}{visibleStage === "thesis" ? <SocialLoginPanel className="mobile-social-login" providers={oauthReadiness.providers} readinessStatus={oauthReadiness.status} /> : null}</AppShell></LanguageProvider>;
}

interface Handlers { readonly startChallenge: () => void; readonly cancelChallenge: () => void; readonly retryChallenge: () => void; readonly recoverChallenge: () => void; readonly addEvidence: (draft: EvidenceDraft) => void; readonly submitReply: (body: string) => void; readonly confirm: (confirmation: "confirmed" | "reserved") => void; readonly computeFm: () => void; readonly cancelFm: () => void; readonly exportJson: () => void; readonly exportCsv: () => void; readonly exportPath: () => void; readonly methodsExport: () => void; readonly rows: ReturnType<typeof allocationRows>; readonly viewpointBranches: ReturnType<typeof makeBranches>; readonly setViewpoint: (viewpoint: string) => void; readonly reset: () => void; }

function renderStage(stage: Stage, state: ReturnType<typeof reducer>, dispatch: React.Dispatch<Parameters<typeof reducer>[1]>, handlers: Handlers, oauthReadiness: OAuthReadiness) {
  switch (stage) {
    case "thesis": return <ThesisInput viewpoint={state.viewpoint} capital={state.capital} currency={state.currency} draftRestored={state.draftRestored} onViewpoint={handlers.setViewpoint} onCapital={(capital) => dispatch({ type: "set_capital", capital })} onCurrency={(currency) => dispatch({ type: "set_currency", currency: currency as Currency })} onStart={handlers.startChallenge} onReset={handlers.reset} />;
    case "challenge": return <ChallengePanel status={state.challengeStatus} turns={state.challengeTurns} replyDraft={state.replyDraft} evidence={state.evidence} rubric={state.rubric} unresolvedIssuesAcknowledged={state.unresolvedIssuesAcknowledged} reservationNote={state.reservationNote} onStart={handlers.startChallenge} onCancel={handlers.cancelChallenge} onRetry={handlers.retryChallenge} onRecover={handlers.recoverChallenge} onReplyDraft={(draft) => dispatch({ type: "set_reply_draft", draft })} onSubmitReply={handlers.submitReply} onEvidence={handlers.addEvidence} onUnresolvedIssuesAcknowledged={(acknowledged) => dispatch({ type: "set_unresolved_issues_acknowledged", acknowledged })} onReservationNote={(note) => dispatch({ type: "set_reservation_note", note })} onConfirm={handlers.confirm} />;
    case "worldline": return <WorldlinePanel branches={handlers.viewpointBranches} selected={state.selectedBranch} onSelect={(branch) => dispatch({ type: "select_branch", branch })} onCompute={handlers.computeFm} canCompute={canStartFm(state)} />;
    case "fm": return <FmPanel status={state.fmStatus} progress={state.fmProgress} step={state.fmStep} resultReady={Boolean(state.fmResult)} onCompute={handlers.computeFm} onCancel={handlers.cancelFm} onPortfolio={() => dispatch({ type: "set_stage", stage: "portfolio" })} />;
    case "portfolio": return <PortfolioPanel mode={state.portfolioMode} currency={state.currency} rows={handlers.rows} capital={parseCapital(state.capital)} selectedBranch={state.selectedBranch} branches={handlers.viewpointBranches} stress={state.stressScenario} onMode={(mode) => dispatch({ type: "set_mode", mode })} onStress={(scenario) => dispatch({ type: "set_stress", scenario })} onCompute={handlers.computeFm} onNav={() => dispatch({ type: "set_stage", stage: "nav" })} onExport={(kind) => kind === "json" ? handlers.exportJson() : handlers.exportCsv()} fmReady={Boolean(state.fmResult)} canCompute={canStartFm(state)} canNavigate={canEnterStage(state, "nav")} />;
    case "nav": return <NavPanel branches={handlers.viewpointBranches} selected={state.selectedBranch} onSelect={(branch) => dispatch({ type: "select_branch", branch })} stress={state.stressScenario} onExport={handlers.exportPath} drawdownVisible={state.drawdownVisible} onToggleDrawdown={() => dispatch({ type: "toggle_drawdown" })} />;
    case "console": return <ConsolePanel onExport={handlers.methodsExport} providers={oauthReadiness.providers} readinessStatus={oauthReadiness.status} />;
  }
}

function renderInspector(stage: Stage, state: ReturnType<typeof reducer>, rows: ReturnType<typeof allocationRows>, onToggleDrawdown: () => void, language: Language) {
  if (stage === "challenge") return <div className="inspector-content"><h2>{t(language, "证据工作台")}</h2><p>{t(language, "来源等级表示来源与可核验性，不代表结论正确。")}</p>{state.evidence.map((item) => <div className="inspector-row" key={item.evidence_id}><Tag tone={item.grade === "E" ? "warning" : "strong"}>{item.grade}</Tag><span>{item.title}</span></div>)}<Disclosure compact /></div>;
  if (stage === "worldline") return <div className="inspector-content"><h2>{t(language, "概率依据")}</h2>{["基准率 · 中性起点", "支持证据 · 向上", "反对证据 · 向下", "专家约束 · 排除不可能路径", "模型不确定性 · 区间加宽"].map((item) => <p className="inspector-line" key={item}>{t(language, item)}</p>)}<div className="inspector-callout">{t(language, "方向表示判断影响，不是可加总百分点。")}</div></div>;
  if (stage === "fm") return <div className="inspector-content"><h2>{t(language, "可复现性")}</h2>{["FM-v1", "决策时点 2026-08-09", "Python golden parity", "无未来数据", "Web Worker"].map((item) => <p className="check-line" key={item}><StatusDot tone="sage" />{t(language, item)}</p>)}<hr /><h3>{t(language, "数据快照")}</h3><p>{t(language, "签名哈希由本地计算生成，原始市场分片不会上传。")}</p></div>;
  if (stage === "portfolio") return <div className="inspector-content"><h2>{t(language, "配置说明")}</h2>{state.fmResult ? <><p>{t(language, "当前组合基于 FM 方法论，在世界线框架下平衡收益贡献与风险暴露。")}</p><hr /><h3>{t(language, "世界线暴露")}</h3><p>{t(language, "当前选择：")}{t(language, state.selectedBranch === "main" ? "主情景 W₀" : state.selectedBranch === "weak" ? "弱情景 W−" : "强情景 W+")}</p><p>{t(language, "残余现金：")}{currencySymbol(state.currency)}{Math.max(0, parseCapital(state.capital) - rows.reduce((sum, row) => sum + row.amount, 0)).toLocaleString(language === "en" ? "en-US" : state.currency === "USD" ? "en-US" : state.currency === "EUR" ? "de-DE" : "zh-CN")}</p></> : <p role="status">{t(language, "FM 尚未计算；暂无配置权重、金额、单位或残余现金结果。")}</p>}<Disclosure /></div>;
  if (stage === "nav") return <div className="inspector-content"><h2>{t(language, "路径假设")}</h2>{["世界线版本 WL-v1", "波动率窗口 252 日", "噪声种子 已固定", "价格口径 复权收盘", "数据截止 2026-08-09"].map((item) => <p className="inspector-line" key={item}>{t(language, item)}</p>)}<Button variant="secondary" onClick={onToggleDrawdown}>{t(language, state.drawdownVisible ? "隐藏回撤" : "显示回撤")}</Button><Disclosure compact /></div>;
  return <div className="inspector-content"><h2>{t(language, "研究档案")}</h2><p>{t(language, "公开研究模式保留从观点、证据到组合路径的审计线索。")}</p><div className="archive-status"><StatusDot tone="sage" /> {t(language, "本地保存")}</div><div className="archive-status"><StatusDot tone="blue" /> {state.evidence.length} {t(language, "条证据")}</div><Disclosure /></div>;
}
