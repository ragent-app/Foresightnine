import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type PropsWithChildren } from "react";
import { AppState as NativeAppState } from "react-native";
import { configuredApiBaseUrl } from "./runtime-config";
import { buildWorkspaceDocument, normalizeWorkspaceDocument, restoreWorkspaceState, scenarioWorkspaceScope, workspaceStorageScope, type WorkspaceDocument } from "./workspace-state";
import { createWorkspaceAutosave, type WorkspaceSaveStatus } from "./workspace-autosave";
import { computeFm, type ComputeFmResult } from "@foresightnine/fm-engine";
import { archiveExists, loadEncryptedArchive, saveEncryptedArchive, loadEncryptedWorkspace, saveEncryptedWorkspace, sharePrivacySafeDocument } from "./adapters/native-archive";
import { buildPrivacySafeDocument, buildResearchSnapshot, canEnterPortfolio, canRestorePrivacySafeDocument, normalizePrivacySafeDocument, restoreAppState, type PrivacySafeDocument } from "./archive";
import { isDuplicateEvidence } from "./evidence";
import { ASSETS, assetsForMarketBundle, FIXTURE_STATE, fmInput, makeBranches, rubricFor } from "./fixtures";
import { t } from "./i18n";
import { createPortfolioPlan, portfolioMetadata, type PortfolioPlan } from "./portfolio";
import { loadDraft, loadLanguage, saveDraft, saveLanguage } from "./storage";
import type { AppState, Branch, EvidenceBundle, FreshnessContext, Language } from "./types";
import { canConfirmNormally, canConfirmReserved, canEnterWorldline, parseCapital } from "./validation";
import { mobileInitialState, reduceChallengeAction, resetMobileWorkflowState } from "./lifecycle";
import { switchLanguageState } from "./language-state";
import { apiErrorNotice, challengeToMobileUi, createMobileApiClient } from "./api-runtime";
import { configuredMarketDataRuntimeConfig } from "./runtime-config";
import { createMarketRuntimeController, type MarketRuntimeState } from "@foresightnine/market-data";
import { researchOwnerScope } from "./research-owner";
import { useOAuthSession } from "./oauth-session";
import { EMPTY_EVIDENCE_WORKSPACE, patchEvidenceWorkspace, type EvidenceWorkspace } from "./evidence-workspace";
import { ConfirmationInputChanged, createConfirmationFlow } from "./confirmation-flow";
import { applyAcceptedEvidence, createEvidenceSubmission, EvidenceInputChanged, type EvidenceAddResult } from "./evidence-submission";

import { scenarioDraftFor, updateScenarioDraft, type ScenarioDrafts, type ScopedScenarioDraft } from "./scenario-inputs";

export { switchLanguageState } from "./language-state";

function fxRatesFingerprint(rates: Readonly<Record<string, number>> | undefined): string {
  return JSON.stringify(Object.entries(rates ?? {}).sort(([left], [right]) => left.localeCompare(right)));
}

export interface MobileStore {
  readonly state: AppState;
  readonly workspaceReady: boolean;
  readonly workspaceStatus: WorkspaceSaveStatus;
  retryWorkspace(): void;
  readonly branches: readonly Branch[];
  readonly portfolio: PortfolioPlan | null;
  readonly scenarioDrafts: ScenarioDrafts;
  setScenarioReturn(assetId: string, value: string): void;
  readonly archive: PrivacySafeDocument | null;
  readonly archiveAvailable: boolean;
  readonly marketDataState: MarketRuntimeState;
  readonly evidenceWorkspace: EvidenceWorkspace;
  updateEvidenceWorkspace(patch: Partial<EvidenceWorkspace>): void;
  updateDraft(patch: Partial<Pick<AppState, "viewpoint" | "capital" | "currency">>): void;
  setLanguage(language: Language): void;
  startChallenge(): void;
  cancelChallenge(): void;
  retryChallenge(): void;
  setChallengeFailure(failure: NonNullable<AppState["challengeFailure"]>): void;
  reply(text: string): void;
  setReplyDraft(text: string): void;
  readonly evidenceBusy: boolean;
  addEvidence(evidence: EvidenceBundle): Promise<EvidenceAddResult>;
  setSearchStatus(status: AppState["searchStatus"]): void;
  setUnresolvedIssuesAcknowledged(acknowledged: boolean): void;
  setReservationNote(note: string): void;
  readonly confirmationBusy: boolean;
  confirm(reserved: boolean): Promise<boolean>;
  selectBranch(branch: AppState["selectedBranch"]): void;
  runFm(): void;
  setPortfolioMode(mode: AppState["portfolioMode"]): void;
  setFreshness(freshness: FreshnessContext): void;
  saveArchive(includePrivate: boolean): Promise<void>;
  restoreArchive(): Promise<void>;
  exportPublicDocument(): Promise<void>;
}

const Store = createContext<MobileStore | null>(null);

export function AppProvider({ children, storageScope: providedStorageScope }: PropsWithChildren<{ readonly storageScope?: string | null }> ): React.JSX.Element {
  const [state, setState] = useState<AppState>(mobileInitialState);
  const latestState = useRef(state);
  latestState.current = state;
  const [evidenceBusy, setEvidenceBusy] = useState(false);
  const [evidenceSubmission] = useState(createEvidenceSubmission);
  useEffect(() => () => evidenceSubmission.cancel(), [evidenceSubmission]);
  const [confirmationBusy, setConfirmationBusy] = useState(false);
  const [confirmationFlow] = useState(createConfirmationFlow);
  useEffect(() => () => confirmationFlow.cancel(), [confirmationFlow]);
  const [evidenceWorkspace, setEvidenceWorkspace] = useState<EvidenceWorkspace>(EMPTY_EVIDENCE_WORKSPACE);
  const updateEvidenceWorkspace = useCallback((patch: Partial<EvidenceWorkspace>) => setEvidenceWorkspace((current) => patchEvidenceWorkspace(current, patch)), []);
  const { getSession, invalidateSession, session: oauthSession, status: oauthStatus } = useOAuthSession();
  const storageScope = providedStorageScope === undefined ? researchOwnerScope(oauthSession, oauthStatus) : providedStorageScope;
  const draftTouched = useRef(false);
  const languageTouched = useRef(false);
  const workspaceScope = storageScope === null ? null : workspaceStorageScope(storageScope, configuredApiBaseUrl());
  const [workspaceReady, setWorkspaceReady] = useState(false);
  const [workspaceStatus, setWorkspaceStatus] = useState<WorkspaceSaveStatus>("loading");
  const [restoreAttempt, setRestoreAttempt] = useState(0);
  const [autosave] = useState(() => createWorkspaceAutosave<WorkspaceDocument>(document => workspaceScope === null ? Promise.reject(new Error("workspace_owner_pending")) : saveEncryptedWorkspace(document, workspaceScope), setWorkspaceStatus));
  const retryWorkspace = useCallback(() => { if (workspaceReady) void autosave.retry(); else setRestoreAttempt(value => value + 1); }, [autosave, workspaceReady]);
  useEffect(() => {
    autosave.resume();
    const listener = NativeAppState.addEventListener("change", next => { if (next !== "active") void autosave.flush(); });
    return () => { listener.remove(); autosave.close(); };
  }, [autosave]);
  const apiClient = useMemo(() => createMobileApiClient({ getSession, invalidateSession }), [getSession, invalidateSession]);
  const marketRuntime = useMemo(() => createMarketRuntimeController(), []);
  const marketConfig = useMemo(() => configuredMarketDataRuntimeConfig(), []);
  const [marketDataState, setMarketDataState] = useState<MarketRuntimeState>(marketRuntime.getState());
  const [marketAssets, setMarketAssets] = useState(ASSETS);
  const apiChallengeId = useRef<string | null>(null);
  const apiOperationId = useRef<string | null>(null);
  const apiAbort = useRef<AbortController | null>(null);
  const workflowGeneration = useRef(0);
  const fmRequest = useRef(0);
  const fmInFlight = useRef(false);
  const invalidateWorkflow = useCallback(() => {
    workflowGeneration.current += 1;
    fmRequest.current += 1;
    fmInFlight.current = false;
    apiAbort.current?.abort();
    apiAbort.current = null;
  }, []);
  // AbortSignal alone is insufficient: a transport can still deliver a late response.
  const captureWorkflow = useCallback(() => {
    const generation = workflowGeneration.current;
    const owner = getSession();
    return () => {
      try { return workflowGeneration.current === generation && getSession() === owner; }
      catch { return false; }
    };
  }, [getSession]);
  useEffect(() => () => invalidateWorkflow(), [invalidateWorkflow]);
  const [scenarioSaved, setScenarioSaved] = useState<ScopedScenarioDraft>({ scope: "", values: {} });
  const scenarioScope = scenarioWorkspaceScope(oauthSession?.account.account_id ?? "anonymous", apiChallengeId.current, state);
  const scenarioDrafts = scenarioDraftFor(scenarioSaved, scenarioScope);
  const setScenarioReturn = useCallback((assetId: string, value: string) => {
    if (!marketAssets.some(asset => asset.id === assetId && asset.assetClass !== "cash")) return;
    setScenarioSaved(current => updateScenarioDraft(current, scenarioScope, state.selectedBranch, assetId, value));
  }, [marketAssets, scenarioScope, state.selectedBranch]);
  useEffect(() => { const unsubscribe = marketRuntime.subscribe(setMarketDataState); marketRuntime.configure(marketConfig); return unsubscribe; }, [marketConfig, marketRuntime]);
  useEffect(() => {
    const providerStatus = marketDataState.status === "ready" && marketDataState.mode !== "mock"
      ? "fresh" as const
      : marketDataState.status === "limited" ? "stale" as const : "unavailable" as const;
    const fxProviderStatus = providerStatus === "fresh" && marketDataState.bundle?.fx ? "fresh" as const : "unavailable" as const;
    const runtimeAsOf = marketDataState.bundle?.cutoff ?? state.freshness.runtimeAsOf;
    const fxAsOf = marketDataState.bundle?.fx?.asOf;
    const fxCnyPerUnit = marketDataState.bundle?.fx?.cnyPerUnit;
    if (state.freshness.providerStatus === providerStatus && state.freshness.fxProviderStatus === fxProviderStatus && state.freshness.runtimeAsOf === runtimeAsOf && state.freshness.fxAsOf === fxAsOf && fxRatesFingerprint(state.freshness.fxCnyPerUnit) === fxRatesFingerprint(fxCnyPerUnit)) return;
    setState((current) => ({ ...current, freshness: { runtimeAsOf, providerStatus, fxProviderStatus, ...(fxAsOf === undefined ? {} : { fxAsOf }), ...(fxCnyPerUnit === undefined ? {} : { fxCnyPerUnit }) } }));
  }, [marketDataState, state.freshness.fxAsOf, state.freshness.fxProviderStatus, state.freshness.providerStatus, state.freshness.runtimeAsOf]);
  const [archive, setArchive] = useState<PrivacySafeDocument | null>(null);
  const [archiveAvailable, setArchiveAvailable] = useState(false);
  useEffect(() => {
    if (storageScope === null || workspaceScope === null) return;
    let active = true;
    setWorkspaceStatus("loading");
    void Promise.all([loadDraft(storageScope), loadLanguage(), archiveExists(storageScope), loadEncryptedWorkspace(workspaceScope)]).then(([draft, language, exists, saved]) => {
      if (!active) return;
      setArchiveAvailable(exists);
      if (saved.status === "unreadable") { setWorkspaceStatus("error"); return; }
      const document = saved.status === "loaded" ? normalizeWorkspaceDocument(saved.document) : null;
      if (saved.status === "loaded" && !document) { setWorkspaceStatus("error"); return; }
      if (document && !draftTouched.current) {
        const restored = restoreWorkspaceState(latestState.current, document);
        apiChallengeId.current = document.apiChallengeId; apiOperationId.current = document.apiOperationId ?? null;
        setEvidenceWorkspace(document.evidenceWorkspace);
        setMarketAssets(document.assets);
        setScenarioSaved({ scope: scenarioWorkspaceScope(oauthSession?.account.account_id ?? "anonymous", document.apiChallengeId, restored), values: document.scenarioDrafts });
        setState(languageTouched.current ? switchLanguageState(restored, latestState.current.language) : restored);
      } else {
        setState(current => ({ ...current,
          ...(draft && !draftTouched.current ? { viewpoint: draft.viewpoint, capital: draft.capital, currency: draft.currency, draftRestored: true } : {}),
          ...(language && !languageTouched.current ? { language, rubric: rubricFor(language) } : {}),
        }));
      }
      setWorkspaceReady(true); setWorkspaceStatus("saved");
    }).catch(() => { if (active) { setWorkspaceStatus("error"); setState(current => ({ ...current, exportNotice: current.language === "en" ? "Local draft could not be read." : "本地草稿读取失败。" })); } });
    return () => { active = false; };
  }, [storageScope, workspaceScope, restoreAttempt]);
  const persist = useCallback((next: AppState) => {
    if (storageScope === null) return;
    let isCurrent: () => boolean;
    try { isCurrent = captureWorkflow(); } catch { return; }
    void saveDraft({ viewpoint: next.viewpoint, capital: next.capital, currency: next.currency, language: next.language, updatedAt: new Date().toISOString() }, storageScope).catch(() => {
      if (isCurrent()) setState(current => ({ ...current, exportNotice: current.language === "en" ? "Local draft was not saved. Check device storage." : "本地草稿未保存，请检查设备存储。" }));
    });
  }, [captureWorkflow, storageScope]);
  const updateDraft = useCallback((patch: Partial<Pick<AppState, "viewpoint" | "capital" | "currency">>) => {
    draftTouched.current = true;
    // Invalidate before React schedules the state updater, not as a side effect inside it.
    if (patch.viewpoint !== undefined && patch.viewpoint !== latestState.current.viewpoint) {
      invalidateWorkflow(); confirmationFlow.cancel(); evidenceSubmission.cancel(); apiChallengeId.current = null; apiOperationId.current = null;
    }
    setState((current) => {
      const viewpointChanged = patch.viewpoint !== undefined && patch.viewpoint !== current.viewpoint;
      const next: AppState = viewpointChanged
        ? { ...resetMobileWorkflowState(current, patch.viewpoint), ...patch }
        : { ...current, ...patch, branchSnapshot: null };
      persist(next);
      return next;
    });
  }, [confirmationFlow, evidenceSubmission, invalidateWorkflow, persist]);
  const setLanguage = useCallback((language: Language) => { languageTouched.current = true; setState((current) => { const next = switchLanguageState(current, language); persist(next); void saveLanguage(language).catch(() => undefined); return next; }); }, [persist]);
  const startChallenge = useCallback(() => {
    draftTouched.current = true; languageTouched.current = true;
    if (apiAbort.current && !apiAbort.current.signal.aborted || evidenceSubmission.isBusy() || confirmationFlow.isBusy()) return;
    invalidateWorkflow();
    setScenarioSaved({ scope: "", values: {} });
    evidenceSubmission.cancel();
    apiChallengeId.current = null; apiOperationId.current = null;
    confirmationFlow.cancel();
    if (!apiClient) { setState((current) => reduceChallengeAction(current, { type: "start_challenge" })); return; }
    const snapshot = latestState.current;
    let isWorkflowCurrent: () => boolean;
    try { isWorkflowCurrent = captureWorkflow(); }
    catch (error) { setState(current => ({ ...current, challengeStatus: "failed", exportNotice: apiErrorNotice(error, current.language) })); return; }
    const controller = new AbortController();
    apiAbort.current = controller;
    const isCurrent = () => isWorkflowCurrent() && apiAbort.current === controller && !controller.signal.aborted;
    setState((current) => ({ ...resetMobileWorkflowState(current), stage: "challenge", challengeStatus: "streaming", challengeTurns: [{ id: "api-user-pending", author: "user", body: snapshot.viewpoint }], exportNotice: "" }));
    void (async () => {
      try {
        const session = await apiClient.createSession({ client_locale: snapshot.language === "en" ? "en-US" : "zh-CN" }, { signal: controller.signal });
        if (!isCurrent()) return;
        const challenge = await apiClient.createChallenge({ session_id: session.session.session_id, thesis: snapshot.viewpoint.trim(), capital: parseCapital(snapshot.capital) ?? 0, base_currency: snapshot.currency, user_private_evidence_claims: false }, { signal: controller.signal });
        if (!isCurrent()) return;
        apiChallengeId.current = challenge.challenge.challenge_id; apiOperationId.current = challenge.operation?.operation_id ?? null;
        const update = challengeToMobileUi(challenge.challenge, snapshot.viewpoint, snapshot.language);
        setState((current) => isWorkflowCurrent() ? ({ ...current, challengeTurns: update.turns, rubric: update.rubric, challengeStatus: update.status, exportNotice: "" }) : current);
      } catch (error) {
        if (isCurrent()) setState((current) => isWorkflowCurrent() ? ({ ...current, challengeStatus: "failed", exportNotice: apiErrorNotice(error, current.language) }) : current);
      } finally { if (apiAbort.current === controller) apiAbort.current = null; }
    })();
  }, [apiClient, captureWorkflow, confirmationFlow, evidenceSubmission, invalidateWorkflow]);
  const cancelChallenge = useCallback(() => { invalidateWorkflow(); evidenceSubmission.cancel(); confirmationFlow.cancel(); setState((current) => apiClient ? { ...current, challengeStatus: "cancelled", exportNotice: current.language === "en" ? "Request cancelled on this device. Server processing may already have completed." : "本机请求已取消；服务端可能已完成处理。" } : reduceChallengeAction(current, { type: "challenge_cancel" })); }, [apiClient, confirmationFlow, evidenceSubmission, invalidateWorkflow]);
  const retryChallenge = useCallback(() => {
    if (!apiClient) { setState(current => reduceChallengeAction(current, { type: "challenge_retry" })); return; }
    const challengeId = apiChallengeId.current;
    if (!challengeId) {
      if (latestState.current.draftRestored) { setState(current => ({ ...current, exportNotice: current.language === "en" ? "The interrupted request returned no research ID. It has not been resent. Edit the thesis to explicitly start a new study." : "上次请求未返回研究标识，尚未重发。若要新建研究，请先修改观点。" })); return; }
      startChallenge(); return;
    }
    if (apiAbort.current && !apiAbort.current.signal.aborted) return;
    let stillCurrent: () => boolean; try { stillCurrent = captureWorkflow(); } catch { return; }
    const controller = new AbortController(); apiAbort.current = controller;
    const operationId = apiOperationId.current;
    void apiClient.getChallenge(challengeId, { signal: controller.signal, ...(operationId ? { operationId } : {}) }).then(response => {
      if (!stillCurrent() || controller.signal.aborted) return;
      if (response.challenge.challenge_id !== challengeId || operationId && response.operation.operation_id !== operationId) throw new Error("Recovered research receipt mismatch");
      if (response.operation.status !== "completed") { setState(current => ({ ...current, exportNotice: current.language === "en" ? "The previous server operation is not complete. No new model request was made." : "服务端上次操作尚未完成，未重新调用模型。" })); return; }
      apiOperationId.current = response.operation.operation_id;
      const snapshot = latestState.current; const update = challengeToMobileUi(response.challenge, snapshot.viewpoint, snapshot.language);
      setState(current => ({ ...current, challengeTurns: update.turns, rubric: update.rubric, challengeStatus: update.status, challengeFailure: null, exportNotice: current.language === "en" ? "Recovered the server result. No new model request was made." : "已读取服务端结果，未重新调用模型。" }));
    }).catch(error => { if (stillCurrent() && !controller.signal.aborted) setState(current => ({ ...current, exportNotice: apiErrorNotice(error, current.language) })); }).finally(() => { if (apiAbort.current === controller) apiAbort.current = null; });
  }, [apiClient, captureWorkflow, startChallenge]);
  const setChallengeFailure = useCallback((failure: NonNullable<AppState["challengeFailure"]>) => setState((current) => reduceChallengeAction(current, { type: "challenge_fail", failure })), []);
  const setReplyDraft = useCallback((replyDraft: string) => setState((current) => ({ ...current, replyDraft })), []);
  const reply = useCallback((text: string) => {
    if (!text.trim() || evidenceSubmission.isBusy() || confirmationFlow.isBusy() || apiAbort.current && !apiAbort.current.signal.aborted) return;
    const challengeId = apiChallengeId.current;
    if (!apiClient) { setState((current) => reduceChallengeAction(current, { type: "submit_reply", body: text })); return; }
    if (!challengeId) { setState(current => ({ ...current, exportNotice: current.language === "en" ? "Start a review before replying." : "请先开始审查，再提交回答。" })); return; }
    let isWorkflowCurrent: () => boolean;
    try { isWorkflowCurrent = captureWorkflow(); }
    catch (error) { setState(current => ({ ...current, exportNotice: apiErrorNotice(error, current.language) })); return; }
    const controller = new AbortController();
    apiAbort.current = controller;
    const isCurrent = () => isWorkflowCurrent() && apiChallengeId.current === challengeId && apiAbort.current === controller && !controller.signal.aborted;
    setState((current) => ({ ...current, challengeStatus: "streaming" }));
    apiOperationId.current = null;
    void apiClient.submitChallengeReply(challengeId, { message: text }, { signal: controller.signal }).then((response) => {
      if (!isCurrent()) return;
      apiOperationId.current = response.operation?.operation_id ?? null;
      setState((current) => {
        if (!isWorkflowCurrent() || apiChallengeId.current !== challengeId) return current;
        const update = challengeToMobileUi(response.challenge, current.viewpoint, current.language, text, current.challengeTurns);
        return { ...current, challengeTurns: update.turns, rubric: update.rubric, challengeStatus: update.status, replyDraft: current.replyDraft.trim() === text.trim() ? "" : current.replyDraft, exportNotice: "" };
      });
    }).catch((error) => {
      if (isCurrent()) setState((current) => isWorkflowCurrent() ? ({ ...current, challengeStatus: "failed", exportNotice: apiErrorNotice(error, current.language) }) : current);
    }).finally(() => { if (apiAbort.current === controller) apiAbort.current = null; });
  }, [apiClient, captureWorkflow, confirmationFlow, evidenceSubmission]);
  const addEvidence = useCallback(async (evidence: EvidenceBundle): Promise<EvidenceAddResult> => {
    if (evidenceSubmission.isBusy() || confirmationFlow.isBusy() || apiAbort.current && !apiAbort.current.signal.aborted) return "busy";
    const snapshot = latestState.current;
    if (isDuplicateEvidence(snapshot.evidence, evidence)) return "duplicate";
    if (snapshot.confirmation !== "pending" || snapshot.challengeStatus === "streaming") return "failed";
    if (!apiClient) {
      setState((current) => isDuplicateEvidence(current.evidence, evidence) ? current : { ...current, evidence: [...current.evidence, evidence] });
      return "added";
    }
    const challengeId = apiChallengeId.current;
    if (!challengeId) { setState((current) => ({ ...current, exportNotice: current.language === "en" ? "Start a review before submitting evidence." : "请先开始审查，再提交证据。" })); return "failed"; }
    apiOperationId.current = null;
    setEvidenceBusy(true);
    try {
      const owner = getSession();
      const record = await evidenceSubmission.submit({ api: apiClient, ownerScope: owner ? `account:${owner.account.account_id}` : "anonymous", challengeId, evidence, isCurrent: () => {
        try { return apiChallengeId.current === challengeId && latestState.current.viewpoint === snapshot.viewpoint && getSession() === owner; } catch { return false; }
      } });
      if (!record) return "busy";
      setState((current) => {
        if (current.viewpoint !== snapshot.viewpoint) return current;
        return applyAcceptedEvidence(current, evidence, record);
      });
      return "added";
    } catch (error) {
      if (!(error instanceof Error && error.name === "AbortError")) setState((current) => ({ ...current, exportNotice: error instanceof EvidenceInputChanged ? (current.language === "en" ? "Retry the original evidence before changing its content." : "请先重试原证据，不要更改尚未确认提交的内容。") : apiErrorNotice(error, current.language) }));
      return "failed";
    } finally { setEvidenceBusy(false); }
  }, [apiClient, confirmationFlow, evidenceSubmission, getSession]);
  const setSearchStatus = useCallback((searchStatus: AppState["searchStatus"]) => setState((current) => ({ ...current, searchStatus })), []);
  const setUnresolvedIssuesAcknowledged = useCallback((acknowledged: boolean) => setState((current) => ({ ...current, unresolvedIssuesAcknowledged: acknowledged })), []);
  const setReservationNote = useCallback((reservationNote: string) => setState((current) => ({ ...current, reservationNote })), []);
  const confirm = useCallback(async (reserved: boolean): Promise<boolean> => {
    if (confirmationFlow.isBusy() || evidenceSubmission.isBusy()) return false;
    const snapshot = latestState.current;
    const allowed = reserved ? canConfirmReserved(snapshot) : canConfirmNormally(snapshot);
    if (!allowed) return false;
    if (snapshot.confirmation !== "pending" && snapshot.branchSnapshot?.length === 3) return true;
    if (!apiClient) {
      setState((current) => ({ ...current, confirmation: reserved ? "reserved" : "confirmed", stage: "worldline", rubric: current.rubric.map((item) => item.id === "confirmation" ? { ...item, score: 4, rationale: reserved ? t(current.language, "reserved") : t(current.language, "confirm") } : item) }));
      return true;
    }
    const challengeId = apiChallengeId.current;
    if (!challengeId) { setState((current) => ({ ...current, exportNotice: current.language === "en" ? "The research connection is missing. Start a new review before confirming." : "研究连接缺失，请先重新开始审查。" })); return false; }
    apiOperationId.current = null;
    setConfirmationBusy(true);
    try {
      const owner = getSession();
      const result = await confirmationFlow.run({ api: apiClient, ownerScope: owner ? `account:${owner.account.account_id}` : "anonymous", challengeId, reserved, acknowledged: snapshot.unresolvedIssuesAcknowledged, note: snapshot.reservationNote, language: snapshot.language, isCurrent: () => {
        try { return apiChallengeId.current === challengeId && latestState.current.viewpoint === snapshot.viewpoint && getSession() === owner; } catch { return false; }
      } });
      if (!result) return false;
      const update = challengeToMobileUi(result.challenge, snapshot.viewpoint, snapshot.language);
      setState((current) => current.viewpoint !== snapshot.viewpoint ? current : ({ ...current, confirmation: reserved ? "reserved" : "confirmed", unresolvedIssuesAcknowledged: snapshot.unresolvedIssuesAcknowledged, reservationNote: snapshot.reservationNote, rubric: update.rubric, stage: "worldline", branchSnapshot: result.branches, exportNotice: "" }));
      return true;
    } catch (error) {
      if (!(error instanceof Error && error.name === "AbortError")) setState((current) => ({ ...current, exportNotice: error instanceof ConfirmationInputChanged ? (current.language === "en" ? "Use the original confirmation choices to recover this request; do not submit a different confirmation." : "请使用原确认选项恢复请求，不要重复提交不同的确认内容。") : apiErrorNotice(error, current.language) }));
      return false;
    } finally { setConfirmationBusy(false); }
  }, [apiClient, confirmationFlow, evidenceSubmission, getSession]);
  const selectBranch = useCallback((selectedBranch: AppState["selectedBranch"]) => setState((current) => ({ ...current, selectedBranch })), []);
  const runFm = useCallback(() => {
    if (!canEnterWorldline(latestState.current) || fmInFlight.current) return;
    let isWorkflowCurrent: () => boolean;
    try { isWorkflowCurrent = captureWorkflow(); }
    catch (error) { setState(current => ({ ...current, fmStatus: "failed", exportNotice: apiErrorNotice(error, current.language) })); return; }
    const requestId = ++fmRequest.current;
    fmInFlight.current = true;
    const isCurrent = () => isWorkflowCurrent() && fmRequest.current === requestId;
    setState((current) => ({ ...current, stage: "portfolio", fmStatus: "computing", fmProgress: 20, fmStep: t(current.language, "runFm") }));
    const request = { assetIds: ASSETS.map((asset) => asset.id), cutoff: marketConfig.mode === "mock" ? "2026-08-09" : new Date().toISOString(), timezone: "UTC" } as const;
    void marketRuntime.refresh(request).then((bundle) => {
      if (!isCurrent()) return;
      const nextAssets = assetsForMarketBundle(bundle);
      const result = computeFm(fmInput(bundle));
      setMarketAssets(nextAssets);
      setState((current) => isCurrent() ? ({ ...current, stage: "portfolio", fmStatus: "complete", fmProgress: 100, fmStep: `${t(current.language, "operationComplete")} · ${bundle.provider.mode} · ${current.language === "en" ? "three point-in-time anchors" : "三个时点锚点"}`, fmResult: result }) : current);
    }).catch(() => { if (isCurrent()) setState((current) => isCurrent() ? ({ ...current, fmStatus: "failed", fmProgress: 0, fmStep: t(current.language, "fixtureRejected") }) : current); }).finally(() => { if (fmRequest.current === requestId) fmInFlight.current = false; });
  }, [captureWorkflow, marketConfig.mode, marketRuntime]);
  const setPortfolioMode = useCallback((portfolioMode: AppState["portfolioMode"]) => setState((current) => ({ ...current, portfolioMode })), []);
  const setFreshness = useCallback((freshness: FreshnessContext) => setState((current) => ({ ...current, freshness })), []);
  const branches = useMemo(() => {
    if (state.branchSnapshot) return state.branchSnapshot;
    return apiClient ? [] : makeBranches(state.viewpoint, state.language);
  }, [apiClient, state.branchSnapshot, state.viewpoint, state.language]);
  useEffect(() => {
    if (!workspaceReady || workspaceScope === null) return;
    try {
      autosave.schedule(buildWorkspaceDocument({ state, branches, apiChallengeId: apiChallengeId.current, apiOperationId: apiOperationId.current, evidenceWorkspace, scenarioDrafts, assets: marketAssets,
        methodData: { methodVersion: portfolioMetadata.method, dataAsOf: portfolioMetadata.dataAsOf, navLabel: portfolioMetadata.navLabel, fixtureState: FIXTURE_STATE, assetIds: marketAssets.map(asset => asset.id) } }));
    } catch { autosave.reject(); }
  }, [autosave, workspaceReady, workspaceScope, state, branches, evidenceWorkspace, scenarioDrafts, marketAssets]);
  const saveArchive = useCallback(async (includePrivate: boolean) => {
    if (storageScope === null) return;
    let isCurrent: () => boolean;
    try { isCurrent = captureWorkflow(); } catch { return; }
    try {
      const snapshot = state;
      const researchState = buildResearchSnapshot({ state: snapshot, branches, methodData: { methodVersion: portfolioMetadata.method, dataAsOf: portfolioMetadata.dataAsOf, navLabel: portfolioMetadata.navLabel, fixtureState: FIXTURE_STATE, assetIds: ASSETS.map((asset) => asset.id) } }, includePrivate);
      const document = buildPrivacySafeDocument({ title: `ForesightNine ${t(snapshot.language, "localArchive")}`, viewpoint: snapshot.viewpoint, capital: parseCapital(snapshot.capital) ?? 0, currency: snapshot.currency, selectedBranch: snapshot.selectedBranch, evidence: snapshot.evidence, language: snapshot.language, researchState }, includePrivate);
      await saveEncryptedArchive(document, storageScope);
      if (!isCurrent()) return;
      setArchive(document); setArchiveAvailable(true); setState((current) => ({ ...current, exportNotice: t(current.language, "archiveSaved") }));
    } catch {
      if (isCurrent()) setState(current => ({ ...current, exportNotice: current.language === "en" ? "Archive was not saved. The study may be too large or incomplete, or device storage may be unavailable." : "存档未保存：研究可能过大、不完整，或设备存储暂不可用。" }));
    }
  }, [branches, captureWorkflow, state, storageScope]);
  const restoreArchive = useCallback(async () => {
    if (storageScope === null) return;
    draftTouched.current = true; languageTouched.current = true;
    invalidateWorkflow();
    let isCurrent: () => boolean;
    try { isCurrent = captureWorkflow(); }
    catch (error) { setState(current => ({ ...current, exportNotice: apiErrorNotice(error, current.language) })); return; }
    setScenarioSaved({ scope: "", values: {} });
    evidenceSubmission.cancel();
    apiChallengeId.current = null; apiOperationId.current = null;
    confirmationFlow.cancel();
    const document = await loadEncryptedArchive(storageScope).catch(() => null);
    if (!isCurrent()) return;
    const normalized = document && canRestorePrivacySafeDocument(document) ? normalizePrivacySafeDocument(document) : null;
    if (normalized) {
      setArchive(normalized);
      setState((current) => ({ ...restoreAppState(current, normalized), exportNotice: t(normalized.researchState?.language ?? current.language, "archiveRestored") }));
    } else {
      setArchive(null);
      setState((current) => ({ ...current, exportNotice: t(current.language, "noArchiveRestored") }));
    }
  }, [captureWorkflow, confirmationFlow, evidenceSubmission, invalidateWorkflow, storageScope]);
  const exportPublicDocument = useCallback(async () => {
    const snapshot = state;
    const document = buildPrivacySafeDocument({ title: `ForesightNine ${t(snapshot.language, "export")}`, viewpoint: snapshot.viewpoint, capital: parseCapital(snapshot.capital) ?? 0, currency: snapshot.currency, selectedBranch: snapshot.selectedBranch, evidence: snapshot.evidence, language: snapshot.language, reservationNote: snapshot.reservationNote, unresolvedIssuesAcknowledged: snapshot.unresolvedIssuesAcknowledged }, false);
    const shared = await sharePrivacySafeDocument(document);
    setState((current) => ({ ...current, exportNotice: shared ? t(current.language, "exportOpened") : t(current.language, "sharingUnavailable") }));
  }, [state]);
  const portfolio = useMemo(() => {
    if (!canEnterPortfolio(state) || !state.fmResult || !parseCapital(state.capital)) return null;
    const branch = branches.find(item => item.kind === state.selectedBranch);
    return createPortfolioPlan(state.fmResult, parseCapital(state.capital)!, state.currency, marketAssets, state.language, state.selectedBranch, state.freshness, branch ? { mode: state.portfolioMode, baselineDate: branch.baselineDate, horizonEnd: branch.horizonEnd, returns: scenarioDrafts[state.selectedBranch] ?? {} } : undefined);
  }, [marketAssets, state, branches, scenarioDrafts]);
  const value = useMemo<MobileStore>(() => ({ state, workspaceReady, workspaceStatus, retryWorkspace, branches, portfolio, scenarioDrafts, setScenarioReturn, archive, archiveAvailable, marketDataState, evidenceWorkspace, updateEvidenceWorkspace, updateDraft, setLanguage, startChallenge, cancelChallenge, retryChallenge, setChallengeFailure, reply, setReplyDraft, evidenceBusy, addEvidence, setSearchStatus, setUnresolvedIssuesAcknowledged, setReservationNote, confirmationBusy, confirm, selectBranch, runFm, setPortfolioMode, setFreshness, saveArchive, restoreArchive, exportPublicDocument }), [state, workspaceReady, workspaceStatus, retryWorkspace, branches, portfolio, scenarioDrafts, setScenarioReturn, archive, archiveAvailable, marketDataState, evidenceWorkspace, updateEvidenceWorkspace, updateDraft, setLanguage, startChallenge, cancelChallenge, retryChallenge, setChallengeFailure, reply, setReplyDraft, evidenceBusy, addEvidence, setSearchStatus, setUnresolvedIssuesAcknowledged, setReservationNote, confirmationBusy, confirm, selectBranch, runFm, setPortfolioMode, setFreshness, saveArchive, restoreArchive, exportPublicDocument]);
  return <Store.Provider value={value}>{children}</Store.Provider>;
}

export function useMobileStore(): MobileStore { const store = useContext(Store); if (!store) throw new Error("Mobile store is missing"); return store; }
