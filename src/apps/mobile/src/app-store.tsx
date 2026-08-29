import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type PropsWithChildren } from "react";
import { computeFm, type ComputeFmResult } from "@foresightnine/fm-engine";
import { archiveExists, loadEncryptedArchive, saveEncryptedArchive, sharePrivacySafeDocument } from "./adapters/native-archive";
import { buildPrivacySafeDocument, buildResearchSnapshot, canEnterPortfolio, canRestorePrivacySafeDocument, normalizePrivacySafeDocument, restoreAppState, type PrivacySafeDocument } from "./archive";
import { isDuplicateEvidence } from "./evidence";
import { ASSETS, FIXTURE_STATE, fmInput, makeBranches, rubricFor } from "./fixtures";
import { t } from "./i18n";
import { createPortfolioPlan, portfolioMetadata, type PortfolioPlan } from "./portfolio";
import { loadDraft, loadLanguage, saveDraft, saveLanguage } from "./storage";
import type { AppState, Branch, EvidenceBundle, FreshnessContext, Language } from "./types";
import { canConfirmNormally, canConfirmReserved, canEnterWorldline, parseCapital } from "./validation";
import { mobileInitialState, reduceChallengeAction, resetMobileWorkflowState } from "./lifecycle";
import { switchLanguageState } from "./language-state";
import { apiErrorNotice, challengeToMobileUi, createMobileApiClient, worldlineBundleToBranches } from "./api-runtime";

export { switchLanguageState } from "./language-state";

export interface MobileStore {
  readonly state: AppState;
  readonly branches: readonly Branch[];
  readonly portfolio: PortfolioPlan | null;
  readonly archive: PrivacySafeDocument | null;
  readonly archiveAvailable: boolean;
  updateDraft(patch: Partial<Pick<AppState, "viewpoint" | "capital" | "currency">>): void;
  setLanguage(language: Language): void;
  startChallenge(): void;
  cancelChallenge(): void;
  retryChallenge(): void;
  setChallengeFailure(failure: NonNullable<AppState["challengeFailure"]>): void;
  reply(text: string): void;
  addEvidence(evidence: EvidenceBundle): void;
  setSearchStatus(status: AppState["searchStatus"]): void;
  setUnresolvedIssuesAcknowledged(acknowledged: boolean): void;
  setReservationNote(note: string): void;
  confirm(reserved: boolean): void;
  selectBranch(branch: AppState["selectedBranch"]): void;
  runFm(): void;
  setPortfolioMode(mode: AppState["portfolioMode"]): void;
  setFreshness(freshness: FreshnessContext): void;
  saveArchive(includePrivate: boolean): Promise<void>;
  restoreArchive(): Promise<void>;
  exportPublicDocument(): Promise<void>;
}

const Store = createContext<MobileStore | null>(null);

export function AppProvider({ children }: PropsWithChildren): React.JSX.Element {
  const [state, setState] = useState<AppState>(mobileInitialState);
  const apiClient = useMemo(() => createMobileApiClient(), []);
  const apiChallengeId = useRef<string | null>(null);
  const apiAbort = useRef<AbortController | null>(null);
  const [archive, setArchive] = useState<PrivacySafeDocument | null>(null);
  const [archiveAvailable, setArchiveAvailable] = useState(false);
  useEffect(() => { void Promise.all([loadDraft(), loadLanguage(), archiveExists()]).then(([draft, language, exists]) => {
    setArchiveAvailable(exists);
    setState((current) => ({ ...current, ...(draft ? { viewpoint: draft.viewpoint, capital: draft.capital, currency: draft.currency, draftRestored: true } : {}), ...(language ? { language, rubric: rubricFor(language) } : {}) }));
  }); }, []);
  const persist = useCallback((next: AppState) => { void saveDraft({ viewpoint: next.viewpoint, capital: next.capital, currency: next.currency, language: next.language, updatedAt: new Date().toISOString() }); }, []);
  const updateDraft = useCallback((patch: Partial<Pick<AppState, "viewpoint" | "capital" | "currency">>) => setState((current) => {
    const viewpointChanged = patch.viewpoint !== undefined && patch.viewpoint !== current.viewpoint;
    const next: AppState = viewpointChanged
      ? { ...resetMobileWorkflowState(current, patch.viewpoint), ...patch }
      : { ...current, ...patch, branchSnapshot: null };
    persist(next);
    return next;
  }), [persist]);
  const setLanguage = useCallback((language: Language) => setState((current) => { const next = switchLanguageState(current, language); persist(next); void saveLanguage(language); return next; }), [persist]);
  const startChallenge = useCallback(() => {
    if (!apiClient) { setState((current) => reduceChallengeAction(current, { type: "start_challenge" })); return; }
    apiAbort.current?.abort();
    const controller = new AbortController();
    apiAbort.current = controller;
    setState((current) => ({ ...resetMobileWorkflowState(current), stage: "challenge", challengeStatus: "streaming", challengeTurns: [{ id: "api-user-pending", author: "user", body: current.viewpoint }], exportNotice: "" }));
    void (async () => {
      try {
        const session = await apiClient.createSession({ client_locale: state.language === "en" ? "en-US" : "zh-CN" }, { signal: controller.signal });
        const challenge = await apiClient.createChallenge({ session_id: session.session.session_id, thesis: state.viewpoint.trim(), capital: parseCapital(state.capital) ?? 0, base_currency: state.currency, user_private_evidence_claims: false }, { signal: controller.signal });
        apiChallengeId.current = challenge.challenge.challenge_id;
        const update = challengeToMobileUi(challenge.challenge, state.viewpoint, state.language);
        setState((current) => ({ ...current, challengeTurns: update.turns, rubric: update.rubric, challengeStatus: update.status, exportNotice: "" }));
      } catch (error) { setState((current) => ({ ...current, challengeStatus: "failed", exportNotice: apiErrorNotice(error, current.language) })); }
    })();
  }, [apiClient, state.capital, state.currency, state.language, state.viewpoint]);
  const cancelChallenge = useCallback(() => { apiAbort.current?.abort(); setState((current) => apiClient ? { ...current, challengeStatus: "cancelled", exportNotice: current.language === "en" ? "API request cancelled locally." : "API 请求已在本机取消。" } : reduceChallengeAction(current, { type: "challenge_cancel" })); }, [apiClient]);
  const retryChallenge = useCallback(() => { if (apiClient) startChallenge(); else setState((current) => reduceChallengeAction(current, { type: "challenge_retry" })); }, [apiClient, startChallenge]);
  const setChallengeFailure = useCallback((failure: NonNullable<AppState["challengeFailure"]>) => setState((current) => reduceChallengeAction(current, { type: "challenge_fail", failure })), []);
  const reply = useCallback((text: string) => {
    const challengeId = apiChallengeId.current;
    if (!apiClient || !challengeId) { setState((current) => reduceChallengeAction(current, { type: "submit_reply", body: text })); return; }
    const controller = new AbortController();
    apiAbort.current?.abort();
    apiAbort.current = controller;
    void apiClient.submitChallengeReply(challengeId, { message: text }, { signal: controller.signal }).then((response) => {
      setState((current) => { const update = challengeToMobileUi(response.challenge, current.viewpoint, current.language, text); return { ...current, challengeTurns: update.turns, rubric: update.rubric, challengeStatus: update.status, exportNotice: "" }; });
    }).catch((error) => setState((current) => ({ ...current, challengeStatus: "failed", exportNotice: apiErrorNotice(error, current.language) })));
  }, [apiClient]);
  const addEvidence = useCallback((evidence: EvidenceBundle) => {
    const challengeId = apiChallengeId.current;
    if (!apiClient || !challengeId) { setState((current) => isDuplicateEvidence(current.evidence, evidence) ? current : { ...current, evidence: [...current.evidence, evidence] }); return; }
    const request: Parameters<typeof apiClient.submitEvidence>[1] = { title: evidence.title, retrieved_at: evidence.retrieved_at, excerpt: evidence.excerpt, user_note: evidence.user_note, content_hash: evidence.content_hash, ...(evidence.url ? { url: evidence.url } : {}), ...(evidence.publisher ? { publisher: evidence.publisher } : {}), ...(evidence.published_at ? { published_at: evidence.published_at } : {}), ...(evidence.search_provider && evidence.search_result_hash ? { search_provider: evidence.search_provider, search_result_hash: evidence.search_result_hash } : {}) };
    const controller = new AbortController();
    apiAbort.current?.abort();
    apiAbort.current = controller;
    void apiClient.submitEvidence(challengeId, request, { signal: controller.signal }).then(() => setState((current) => isDuplicateEvidence(current.evidence, evidence) ? current : { ...current, evidence: [...current.evidence, evidence], exportNotice: "" })).catch((error) => setState((current) => ({ ...current, exportNotice: apiErrorNotice(error, current.language) })));
  }, [apiClient]);
  const setSearchStatus = useCallback((searchStatus: AppState["searchStatus"]) => setState((current) => ({ ...current, searchStatus })), []);
  const setUnresolvedIssuesAcknowledged = useCallback((acknowledged: boolean) => setState((current) => ({ ...current, unresolvedIssuesAcknowledged: acknowledged })), []);
  const setReservationNote = useCallback((reservationNote: string) => setState((current) => ({ ...current, reservationNote })), []);
  const confirm = useCallback((reserved: boolean) => {
    const challengeId = apiChallengeId.current;
    if (!apiClient || !challengeId) { setState((current) => { const allowed = reserved ? canConfirmReserved(current) : canConfirmNormally(current); return !allowed ? current : { ...current, confirmation: reserved ? "reserved" : "confirmed", stage: "worldline", rubric: current.rubric.map((item) => item.id === "confirmation" ? { ...item, score: 4, rationale: reserved ? t(current.language, "reserved") : t(current.language, "confirm") } : item) }; }); return; }
    const controller = new AbortController();
    apiAbort.current?.abort();
    apiAbort.current = controller;
    void apiClient.confirmChallenge(challengeId, { command: reserved ? "CONFIRM_WITH_RESERVATIONS" : "CONFIRM", unresolved_issues_acknowledged: state.unresolvedIssuesAcknowledged, ...(reserved ? { reservation_note: state.reservationNote } : {}) }, { signal: controller.signal }).then(() => apiClient.createWorldlines({ challenge_id: challengeId, horizon_end: "2028-08-31" }, { signal: controller.signal })).then((response) => {
      const branches = worldlineBundleToBranches(response.bundle, state.language);
      if (branches.length !== 3) throw new Error("API worldline data is unavailable.");
      // The local gate was evaluated before issuing the request. Once the
      // server has accepted the same confirmation and returned a validated
      // three-branch bundle, the server result is the authoritative lifecycle
      // transition. Re-checking a mutable note/transcript here could leave the
      // UI stranded on the worldline route after a successful API response.
      setState((current) => ({ ...current, confirmation: reserved ? "reserved" : "confirmed", stage: "worldline", branchSnapshot: branches, exportNotice: "" }));
    }).catch((error) => setState((current) => ({ ...current, exportNotice: apiErrorNotice(error, current.language) })));
  }, [apiClient, state.reservationNote, state.unresolvedIssuesAcknowledged]);
  const selectBranch = useCallback((selectedBranch: AppState["selectedBranch"]) => setState((current) => ({ ...current, selectedBranch })), []);
  const runFm = useCallback(() => setState((current) => {
    if (!canEnterWorldline(current)) return current;
    try { const result = computeFm(fmInput()); return { ...current, stage: "portfolio", fmStatus: "complete", fmProgress: 100, fmStep: t(current.language, "operationComplete"), fmResult: result }; } catch { return { ...current, fmStatus: "failed", fmStep: t(current.language, "fixtureRejected") }; }
  }), []);
  const setPortfolioMode = useCallback((portfolioMode: AppState["portfolioMode"]) => setState((current) => ({ ...current, portfolioMode })), []);
  const setFreshness = useCallback((freshness: FreshnessContext) => setState((current) => ({ ...current, freshness })), []);
  const branches = useMemo(() => {
    if (state.branchSnapshot) return state.branchSnapshot;
    return apiClient ? [] : makeBranches(state.viewpoint, state.language);
  }, [apiClient, state.branchSnapshot, state.viewpoint, state.language]);
  const saveArchive = useCallback(async (includePrivate: boolean) => {
    const snapshot = state;
    const researchState = buildResearchSnapshot({ state: snapshot, branches, methodData: { methodVersion: portfolioMetadata.method, dataAsOf: portfolioMetadata.dataAsOf, navLabel: portfolioMetadata.navLabel, fixtureState: FIXTURE_STATE, assetIds: ASSETS.map((asset) => asset.id) } }, includePrivate);
    const document = buildPrivacySafeDocument({ title: `ForesightNine ${t(snapshot.language, "localArchive")}`, viewpoint: snapshot.viewpoint, capital: parseCapital(snapshot.capital) ?? 0, currency: snapshot.currency, selectedBranch: snapshot.selectedBranch, evidence: snapshot.evidence, language: snapshot.language, researchState }, includePrivate);
    await saveEncryptedArchive(document); setArchive(document); setArchiveAvailable(true); setState((current) => ({ ...current, exportNotice: t(current.language, "archiveSaved") }));
  }, [branches, state]);
  const restoreArchive = useCallback(async () => {
    const document = await loadEncryptedArchive();
    const normalized = document && canRestorePrivacySafeDocument(document) ? normalizePrivacySafeDocument(document) : null;
    if (normalized) {
      setArchive(normalized);
      setState((current) => ({ ...restoreAppState(current, normalized), exportNotice: t(normalized.researchState?.language ?? current.language, "archiveRestored") }));
    } else {
      setArchive(null);
      setState((current) => ({ ...current, exportNotice: t(current.language, "noArchiveRestored") }));
    }
  }, []);
  const exportPublicDocument = useCallback(async () => {
    const snapshot = state;
    const document = buildPrivacySafeDocument({ title: `ForesightNine ${t(snapshot.language, "export")}`, viewpoint: snapshot.viewpoint, capital: parseCapital(snapshot.capital) ?? 0, currency: snapshot.currency, selectedBranch: snapshot.selectedBranch, evidence: snapshot.evidence, language: snapshot.language, reservationNote: snapshot.reservationNote, unresolvedIssuesAcknowledged: snapshot.unresolvedIssuesAcknowledged }, false);
    const shared = await sharePrivacySafeDocument(document);
    setState((current) => ({ ...current, exportNotice: shared ? t(current.language, "exportOpened") : t(current.language, "sharingUnavailable") }));
  }, [state]);
  const portfolio = useMemo(() => canEnterPortfolio(state) && state.fmResult && parseCapital(state.capital) ? createPortfolioPlan(state.fmResult, parseCapital(state.capital)!, state.currency, undefined, state.language, state.selectedBranch, state.freshness) : null, [state, state.fmResult, state.capital, state.currency, state.language, state.selectedBranch, state.freshness]);
  const value = useMemo<MobileStore>(() => ({ state, branches, portfolio, archive, archiveAvailable, updateDraft, setLanguage, startChallenge, cancelChallenge, retryChallenge, setChallengeFailure, reply, addEvidence, setSearchStatus, setUnresolvedIssuesAcknowledged, setReservationNote, confirm, selectBranch, runFm, setPortfolioMode, setFreshness, saveArchive, restoreArchive, exportPublicDocument }), [state, branches, portfolio, archive, archiveAvailable, updateDraft, setLanguage, startChallenge, cancelChallenge, retryChallenge, setChallengeFailure, reply, addEvidence, setSearchStatus, setUnresolvedIssuesAcknowledged, setReservationNote, confirm, selectBranch, runFm, setPortfolioMode, setFreshness, saveArchive, restoreArchive, exportPublicDocument]);
  return <Store.Provider value={value}>{children}</Store.Provider>;
}

export function useMobileStore(): MobileStore { const store = useContext(Store); if (!store) throw new Error("Mobile store is missing"); return store; }
