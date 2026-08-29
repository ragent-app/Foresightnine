import type { EvidenceItem, ProbabilityAssessment, ThesisDocument, Worldline } from "@foresightnine/contracts";
import type { FreshnessDecision, ProviderFreshnessStatus } from "@foresightnine/domain";
import type { ComputeFmResult } from "@foresightnine/fm-engine";

export type Stage = "thesis" | "challenge" | "worldline" | "fm" | "portfolio" | "nav" | "console";
export type ChallengeStatus = "idle" | "streaming" | "cancelled" | "recovering" | "failed" | "complete";
export type FmStatus = "idle" | "computing" | "complete" | "cancelled" | "failed";
export type PortfolioMode = "raw" | "public";
export type StressScenario = "weak" | "main" | "strong";
export type Currency = "CNY" | "USD" | "EUR";
export type Language = "zh" | "en";
export type EvidenceGrade = "A" | "B" | "C" | "D" | "E";

export interface SearchEvidenceBundle {
  readonly evidence_id: string;
  readonly challenge_id: string;
  readonly source_type: "primary" | "reliable_secondary" | "commentary" | "user_private" | "model_inference";
  readonly url: string;
  readonly title: string;
  readonly publisher: string;
  readonly published_at: string;
  readonly retrieved_at: string;
  readonly excerpt: string;
  readonly user_note: string;
  readonly content_hash: string;
  readonly grade: EvidenceGrade;
  readonly supports_or_challenges: "supports" | "challenges" | "context";
  readonly independently_reviewable: boolean;
  readonly search_provider?: string;
  readonly search_result_hash?: string;
  readonly fixture?: boolean;
}

export interface EvidenceDraft {
  readonly url: string;
  readonly title: string;
  readonly publisher: string;
  readonly published_at: string;
  readonly excerpt: string;
  readonly user_note: string;
  readonly supports_or_challenges: SearchEvidenceBundle["supports_or_challenges"];
  readonly grade: EvidenceGrade;
  readonly search_provider?: string;
  readonly search_result_hash?: string;
}

export interface SearchCandidate {
  readonly search_result_hash: string;
  readonly search_provider: string;
  readonly title: string;
  readonly url: string;
  readonly excerpt: string;
  readonly publisher?: string;
  readonly published_at?: string;
  readonly retrieved_at: string;
  readonly independently_reviewable: boolean;
}

export interface SearchCandidatesResult {
  readonly provider: string;
  readonly project_search_available: boolean;
  readonly fallback: "client_bundle";
  readonly candidates: readonly SearchCandidate[];
}

export interface EvidenceErrors {
  url?: string;
  title?: string;
  publisher?: string;
  published_at?: string;
  excerpt?: string;
  user_note?: string;
}

export interface DraftState {
  readonly viewpoint: string;
  readonly capital: string;
  readonly currency: Currency;
  readonly updatedAt: string;
}

export interface EvidenceView extends Pick<EvidenceItem, "evidence_id" | "challenge_id" | "title" | "source_type" | "grade" | "excerpt" | "supports_or_challenges" | "independently_reviewable" | "retrieved_at" | "content_hash"> {
  readonly sourceLabel: string;
  readonly url: string;
  readonly publisher: string;
  readonly published_at: string;
  readonly user_note: string;
  /** Hash/provenance of an explicitly selected project-search candidate. */
  readonly search_provider?: string;
  readonly search_result_hash?: string;
  readonly fixture?: boolean;
}

export interface RubricView {
  readonly label: string;
  readonly score: number;
  readonly max: number;
  readonly rationale: string;
}

export interface TranscriptTurn {
  readonly id: string;
  readonly author: "user" | "challenger" | "system";
  readonly time: string;
  readonly body: string;
}

export interface AssetFixture {
  readonly assetId: string;
  readonly displayName: string;
  readonly economicExposureId: string;
  readonly instrumentId: string;
  readonly assetClass: string;
  readonly quoteCurrency: Currency;
  readonly price: number;
  readonly lotSize: number;
  readonly fractionalSupported: boolean;
  readonly tradable: boolean;
  readonly proxyInstrument?: string;
  readonly liquidity: "high" | "medium" | "low" | "restricted";
  readonly volatility: "low" | "high";
  readonly asOf: string;
  readonly freshness: "current" | "historical_only" | "stale";
}

export interface FreshnessContext {
  readonly runtimeAsOf: string;
  readonly providerStatus: ProviderFreshnessStatus;
}

export interface FreshnessOptions extends FreshnessContext {
  readonly policy?: { readonly maxAgeMs: number };
}

export interface PublicConstraintReason {
  readonly code: string;
  readonly label: string;
  readonly detail: string;
}

export interface BranchView {
  readonly kind: "weak" | "main" | "strong";
  readonly title: string;
  readonly range: string;
  readonly color: string;
  readonly description: string;
  readonly reveal: string;
  readonly invalidation: string;
  readonly nodes: readonly string[];
  readonly assessment: ProbabilityAssessment;
  readonly worldline: Worldline;
}

export interface AllocationRow {
  readonly id: string;
  readonly role: string;
  readonly weight: number;
  readonly publicWeight: number;
  readonly amount: number;
  readonly units: number;
  readonly rawAmount: number;
  readonly rawUnits: number;
  readonly publicAmount: number;
  readonly publicUnits: number;
  readonly currency: Currency;
  readonly price: number;
  readonly quoteCurrency: Currency;
  readonly lotSize: number;
  readonly residualReason?: string;
  readonly rawResidualReason?: string;
  readonly publicResidualReason?: string;
  readonly constraintReasons: readonly PublicConstraintReason[];
  readonly dataAsOf: string;
  readonly instrumentId: string;
  readonly proxyInstrument?: string;
  readonly tradable: boolean;
  readonly liquidity: AssetFixture["liquidity"];
  readonly volatility: AssetFixture["volatility"];
  readonly freshness: FreshnessDecision["status"];
  readonly freshnessLabel: FreshnessDecision["label"];
  readonly publicExecutionAllowed: boolean;
}

export interface AppState {
  readonly stage: Stage;
  readonly language: Language;
  readonly viewpoint: string;
  readonly capital: string;
  readonly currency: Currency;
  readonly draftRestored: boolean;
  readonly challengeStatus: ChallengeStatus;
  readonly challengeTurns: readonly TranscriptTurn[];
  readonly replyDraft: string;
  readonly rubric: readonly RubricView[];
  readonly evidence: readonly EvidenceView[];
  readonly confirmation: "pending" | "confirmed" | "reserved";
  readonly unresolvedIssuesAcknowledged: boolean;
  readonly reservationNote: string;
  readonly selectedBranch: BranchView["kind"];
  readonly fmStatus: FmStatus;
  readonly fmProgress: number;
  readonly fmStep: string;
  readonly fmResult: ComputeFmResult | null;
  readonly portfolioMode: PortfolioMode;
  readonly stressScenario: StressScenario;
  readonly drawdownVisible: boolean;
  readonly exportNotice: string;
  readonly freshness: FreshnessContext;
  readonly apiSessionId: string | null;
  readonly apiChallengeId: string | null;
  readonly apiError: string;
  readonly serverBranches: readonly BranchView[] | null;
}

export type AppAction =
  | { readonly type: "set_stage"; readonly stage: Stage }
  | { readonly type: "set_language"; readonly language: Language }
  | { readonly type: "set_viewpoint"; readonly viewpoint: string }
  | { readonly type: "set_capital"; readonly capital: string }
  | { readonly type: "set_currency"; readonly currency: Currency }
  | { readonly type: "restore_draft"; readonly draft: DraftState }
  | { readonly type: "start_challenge"; readonly viewpoint?: string; readonly server?: boolean }
  | { readonly type: "set_reply_draft"; readonly draft: string }
  | { readonly type: "submit_reply"; readonly body: string }
  | { readonly type: "challenge_complete" }
  | { readonly type: "challenge_cancel" }
  | { readonly type: "challenge_recover" }
  | { readonly type: "challenge_fail" }
  | { readonly type: "add_evidence"; readonly evidence: EvidenceView }
  | { readonly type: "set_unresolved_issues_acknowledged"; readonly acknowledged: boolean }
  | { readonly type: "set_reservation_note"; readonly note: string }
  | { readonly type: "confirm"; readonly confirmation: "confirmed" | "reserved" }
  | { readonly type: "select_branch"; readonly branch: BranchView["kind"] }
  | { readonly type: "fm_start" }
  | { readonly type: "fm_progress"; readonly progress: number; readonly step: string }
  | { readonly type: "fm_complete"; readonly result: ComputeFmResult }
  | { readonly type: "fm_cancel" }
  | { readonly type: "fm_fail" }
  | { readonly type: "set_mode"; readonly mode: PortfolioMode }
  | { readonly type: "set_stress"; readonly scenario: StressScenario }
  | { readonly type: "toggle_drawdown" }
  | { readonly type: "export_notice"; readonly notice: string }
  | { readonly type: "set_freshness"; readonly freshness: FreshnessContext }
  | { readonly type: "api_challenge_update"; readonly challengeId: string; readonly turns: readonly TranscriptTurn[]; readonly rubric: readonly RubricView[]; readonly status: ChallengeStatus; readonly unresolvedIssues: readonly string[] }
  | { readonly type: "api_session_ready"; readonly sessionId: string }
  | { readonly type: "api_worldlines_ready"; readonly branches: readonly BranchView[] }
  | { readonly type: "api_error"; readonly notice: string }
  | { readonly type: "api_cancel" }
  | { readonly type: "reset" };
