import { createContext, useCallback, useContext, useEffect, useMemo, useState, type PropsWithChildren } from "react";
import { computeFm, type ComputeFmResult } from "@foresightnine/fm-engine";
import { archiveExists, loadEncryptedArchive, saveEncryptedArchive, sharePrivacySafeDocument } from "./adapters/native-archive";
import { buildPrivacySafeDocument, buildResearchSnapshot, canEnterPortfolio, canRestorePrivacySafeDocument, normalizePrivacySafeDocument, restoreAppState, type PrivacySafeDocument } from "./archive";
import { isDuplicateEvidence } from "./evidence";
import { ASSETS, FIXTURE_STATE, fmInput, makeBranches, rubricFor } from "./fixtures";
import { t } from "./i18n";
import { createPortfolioPlan, portfolioMetadata, type PortfolioPlan } from "./portfolio";
import { loadDraft, loadLanguage, saveDraft, saveLanguage } from "./storage";
import type { AppState, Branch, EvidenceBundle, Language } from "./types";
import { canConfirmNormally, canConfirmReserved, canEnterWorldline, parseCapital } from "./validation";
import { mobileInitialState, reduceChallengeAction, resetMobileWorkflowState } from "./lifecycle";
import { switchLanguageState } from "./language-state";

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
  saveArchive(includePrivate: boolean): Promise<void>;
  restoreArchive(): Promise<void>;
  exportPublicDocument(): Promise<void>;
}

const Store = createContext<MobileStore | null>(null);

export function AppProvider({ children }: PropsWithChildren): React.JSX.Element {
  const [state, setState] = useState<AppState>(mobileInitialState);
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
  const startChallenge = useCallback(() => setState((current) => reduceChallengeAction(current, { type: "start_challenge" })), []);
  const cancelChallenge = useCallback(() => setState((current) => reduceChallengeAction(current, { type: "challenge_cancel" })), []);
  const retryChallenge = useCallback(() => setState((current) => reduceChallengeAction(current, { type: "challenge_retry" })), []);
  const setChallengeFailure = useCallback((failure: NonNullable<AppState["challengeFailure"]>) => setState((current) => reduceChallengeAction(current, { type: "challenge_fail", failure })), []);
  const reply = useCallback((text: string) => setState((current) => reduceChallengeAction(current, { type: "submit_reply", body: text })), []);
  const addEvidence = useCallback((evidence: EvidenceBundle) => {
    setState((current) => isDuplicateEvidence(current.evidence, evidence) ? current : { ...current, evidence: [...current.evidence, evidence] });
  }, []);
  const setSearchStatus = useCallback((searchStatus: AppState["searchStatus"]) => setState((current) => ({ ...current, searchStatus })), []);
  const setUnresolvedIssuesAcknowledged = useCallback((acknowledged: boolean) => setState((current) => ({ ...current, unresolvedIssuesAcknowledged: acknowledged })), []);
  const setReservationNote = useCallback((reservationNote: string) => setState((current) => ({ ...current, reservationNote })), []);
  const confirm = useCallback((reserved: boolean) => setState((current) => {
    const allowed = reserved ? canConfirmReserved(current) : canConfirmNormally(current);
    return !allowed ? current : { ...current, confirmation: reserved ? "reserved" : "confirmed", stage: "worldline", rubric: current.rubric.map((item) => item.id === "confirmation" ? { ...item, score: 4, rationale: reserved ? t(current.language, "reserved") : t(current.language, "confirm") } : item) };
  }), []);
  const selectBranch = useCallback((selectedBranch: AppState["selectedBranch"]) => setState((current) => ({ ...current, selectedBranch })), []);
  const runFm = useCallback(() => setState((current) => {
    if (!canEnterWorldline(current)) return current;
    try { const result = computeFm(fmInput()); return { ...current, stage: "portfolio", fmStatus: "complete", fmProgress: 100, fmStep: t(current.language, "operationComplete"), fmResult: result }; } catch { return { ...current, fmStatus: "failed", fmStep: t(current.language, "fixtureRejected") }; }
  }), []);
  const setPortfolioMode = useCallback((portfolioMode: AppState["portfolioMode"]) => setState((current) => ({ ...current, portfolioMode })), []);
  const branches = useMemo(() => state.branchSnapshot ?? makeBranches(state.viewpoint, state.language), [state.branchSnapshot, state.viewpoint, state.language]);
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
  const portfolio = useMemo(() => canEnterPortfolio(state) && state.fmResult && parseCapital(state.capital) ? createPortfolioPlan(state.fmResult, parseCapital(state.capital)!, state.currency, undefined, state.language, state.selectedBranch) : null, [state, state.fmResult, state.capital, state.currency, state.language, state.selectedBranch]);
  const value = useMemo<MobileStore>(() => ({ state, branches, portfolio, archive, archiveAvailable, updateDraft, setLanguage, startChallenge, cancelChallenge, retryChallenge, setChallengeFailure, reply, addEvidence, setSearchStatus, setUnresolvedIssuesAcknowledged, setReservationNote, confirm, selectBranch, runFm, setPortfolioMode, saveArchive, restoreArchive, exportPublicDocument }), [state, branches, portfolio, archive, archiveAvailable, updateDraft, setLanguage, startChallenge, cancelChallenge, retryChallenge, setChallengeFailure, reply, addEvidence, setSearchStatus, setUnresolvedIssuesAcknowledged, setReservationNote, confirm, selectBranch, runFm, setPortfolioMode, saveArchive, restoreArchive, exportPublicDocument]);
  return <Store.Provider value={value}>{children}</Store.Provider>;
}

export function useMobileStore(): MobileStore { const store = useContext(Store); if (!store) throw new Error("Mobile store is missing"); return store; }
