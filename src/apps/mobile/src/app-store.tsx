import { createContext, useCallback, useContext, useEffect, useMemo, useState, type PropsWithChildren } from "react";
import { computeFm, type ComputeFmResult } from "@foresightnine/fm-engine";
import { archiveExists, loadEncryptedArchive, saveEncryptedArchive, sharePrivacySafeDocument } from "./adapters/native-archive";
import { buildPrivacySafeDocument, type PrivacySafeDocument } from "./archive";
import { isDuplicateEvidence } from "./evidence";
import { MOCK_EVIDENCE, MOCK_TURNS, RUBRIC, fmInput, makeBranches } from "./fixtures";
import { createPortfolioPlan, type PortfolioPlan } from "./portfolio";
import { loadDraft, loadLanguage, saveDraft, saveLanguage } from "./storage";
import type { AppState, Branch, EvidenceBundle, Language } from "./types";
import { parseCapital } from "./validation";

const initialState: AppState = {
  stage: "thesis", language: "zh", viewpoint: "如果未来两年数据中心电力需求持续上升，并推动电网设备投资提速，那么电网基础设施将受益。", capital: "1000000", currency: "CNY", draftRestored: false,
  challengeStatus: "idle", challengeFailure: null, challengeTurns: [], replyDraft: "", rubric: RUBRIC, evidence: [], searchStatus: "idle", confirmation: "pending", selectedBranch: "main",
  fmStatus: "idle", fmProgress: 0, fmStep: "", fmResult: null, portfolioMode: "public", exportNotice: "",
};

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
  addEvidence(evidence: EvidenceBundle): "added" | "duplicate";
  setSearchStatus(status: AppState["searchStatus"]): void;
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
  const [state, setState] = useState<AppState>(initialState);
  const [archive, setArchive] = useState<PrivacySafeDocument | null>(null);
  const [archiveAvailable, setArchiveAvailable] = useState(false);
  useEffect(() => { void Promise.all([loadDraft(), loadLanguage(), archiveExists()]).then(([draft, language, exists]) => {
    setArchiveAvailable(exists);
    setState((current) => ({ ...current, ...(draft ? { viewpoint: draft.viewpoint, capital: draft.capital, currency: draft.currency, draftRestored: true } : {}), ...(language ? { language } : {}) }));
  }); }, []);
  const persist = useCallback((next: AppState) => { void saveDraft({ viewpoint: next.viewpoint, capital: next.capital, currency: next.currency, language: next.language, updatedAt: new Date().toISOString() }); }, []);
  const updateDraft = useCallback((patch: Partial<Pick<AppState, "viewpoint" | "capital" | "currency">>) => setState((current) => { const next = { ...current, ...patch }; persist(next); return next; }), [persist]);
  const setLanguage = useCallback((language: Language) => setState((current) => { const next = { ...current, language }; persist(next); void saveLanguage(language); return next; }), [persist]);
  const startChallenge = useCallback(() => setState((current) => ({ ...current, stage: "challenge", challengeStatus: "streaming", challengeFailure: null, challengeTurns: [...MOCK_TURNS] })), []);
  const cancelChallenge = useCallback(() => setState((current) => ({ ...current, challengeStatus: "cancelled" })), []);
  const retryChallenge = useCallback(() => setState((current) => ({ ...current, challengeStatus: "complete", challengeFailure: null, challengeTurns: [...MOCK_TURNS] })), []);
  const setChallengeFailure = useCallback((failure: NonNullable<AppState["challengeFailure"]>) => setState((current) => ({ ...current, challengeStatus: "failed", challengeFailure: failure })), []);
  const reply = useCallback((text: string) => setState((current) => ({ ...current, replyDraft: "", challengeStatus: "complete", challengeTurns: [...current.challengeTurns, { id: `user-${Date.now()}`, author: "user", body: text }] })), []);
  const addEvidence = useCallback((evidence: EvidenceBundle) => {
    let result: "added" | "duplicate" = "added";
    setState((current) => { if (isDuplicateEvidence(current.evidence, evidence)) { result = "duplicate"; return current; } return { ...current, evidence: [...current.evidence, evidence] }; });
    return result;
  }, []);
  const setSearchStatus = useCallback((searchStatus: AppState["searchStatus"]) => setState((current) => ({ ...current, searchStatus })), []);
  const confirm = useCallback((reserved: boolean) => setState((current) => ({ ...current, confirmation: reserved ? "reserved" : "confirmed", stage: "worldline", rubric: current.rubric.map((item) => item.id === "confirmation" ? { ...item, score: 4, rationale: reserved ? "用户带保留意见确认。" : "用户确认进入模拟。" } : item) })), []);
  const selectBranch = useCallback((selectedBranch: AppState["selectedBranch"]) => setState((current) => ({ ...current, selectedBranch })), []);
  const runFm = useCallback(() => setState((current) => { try { const result = computeFm(fmInput()); return { ...current, stage: "portfolio", fmStatus: "complete", fmProgress: 100, fmStep: "FM client computation complete", fmResult: result }; } catch { return { ...current, fmStatus: "failed", fmStep: "FM fixture rejected" }; } }), []);
  const setPortfolioMode = useCallback((portfolioMode: AppState["portfolioMode"]) => setState((current) => ({ ...current, portfolioMode })), []);
  const saveArchive = useCallback(async (includePrivate: boolean) => {
    const snapshot = state;
    const document = buildPrivacySafeDocument({ title: "ForesightNine research archive", viewpoint: snapshot.viewpoint, capital: parseCapital(snapshot.capital) ?? 0, currency: snapshot.currency, selectedBranch: snapshot.selectedBranch, evidence: snapshot.evidence.length ? snapshot.evidence : MOCK_EVIDENCE }, includePrivate);
    await saveEncryptedArchive(document); setArchive(document); setArchiveAvailable(true); setState((current) => ({ ...current, exportNotice: "Encrypted local archive saved. Private evidence is excluded unless explicitly selected." }));
  }, [state]);
  const restoreArchive = useCallback(async () => { const document = await loadEncryptedArchive(); setArchive(document); setState((current) => ({ ...current, exportNotice: document ? "Encrypted archive restored locally." : "No valid encrypted archive could be restored." })); }, []);
  const exportPublicDocument = useCallback(async () => {
    const snapshot = state;
    const document = buildPrivacySafeDocument({ title: "ForesightNine public research", viewpoint: snapshot.viewpoint, capital: parseCapital(snapshot.capital) ?? 0, currency: snapshot.currency, selectedBranch: snapshot.selectedBranch, evidence: snapshot.evidence.length ? snapshot.evidence : MOCK_EVIDENCE }, false);
    const shared = await sharePrivacySafeDocument(document);
    setState((current) => ({ ...current, exportNotice: shared ? "Privacy-safe research export opened." : "Sharing is unavailable on this device." }));
  }, [state]);
  const branches = useMemo(() => makeBranches(state.viewpoint), [state.viewpoint]);
  const portfolio = useMemo(() => state.fmResult && parseCapital(state.capital) ? createPortfolioPlan(state.fmResult, parseCapital(state.capital)!, state.currency) : null, [state.fmResult, state.capital, state.currency]);
  const value = useMemo<MobileStore>(() => ({ state, branches, portfolio, archive, archiveAvailable, updateDraft, setLanguage, startChallenge, cancelChallenge, retryChallenge, setChallengeFailure, reply, addEvidence, setSearchStatus, confirm, selectBranch, runFm, setPortfolioMode, saveArchive, restoreArchive, exportPublicDocument }), [state, branches, portfolio, archive, archiveAvailable, updateDraft, setLanguage, startChallenge, cancelChallenge, retryChallenge, setChallengeFailure, reply, addEvidence, setSearchStatus, confirm, selectBranch, runFm, setPortfolioMode, saveArchive, restoreArchive, exportPublicDocument]);
  return <Store.Provider value={value}>{children}</Store.Provider>;
}

export function useMobileStore(): MobileStore { const store = useContext(Store); if (!store) throw new Error("Mobile store is missing"); return store; }
