import type { ComputeFmResult } from "@foresightnine/fm-engine";
import type { FreshnessDecision, ProviderFreshnessStatus } from "@foresightnine/domain";

export type Language = "zh" | "en";
export type Currency = "CNY" | "USD" | "EUR";
export type Stage = "thesis" | "challenge" | "evidence" | "worldline" | "portfolio" | "export";
export type ChallengeStatus = "idle" | "streaming" | "cancelled" | "recovering" | "failed" | "complete";
export type ChallengeFailure = "timeout" | "rate_limited" | "invalid_provider" | "no_search" | null;
export type BranchKind = "weak" | "main" | "strong";
export type PortfolioMode = "raw" | "public";
export type EvidenceGrade = "A" | "B" | "C" | "D" | "E";
export type AssetClass = "equity" | "defensive" | "commodity" | "cash";
export type FixtureState = "historical_reference_and_simulated";

export interface ProbabilityInterval {
  readonly low: number;
  readonly high: number;
}

export interface DraftState {
  readonly viewpoint: string;
  readonly capital: string;
  readonly currency: Currency;
  readonly language: Language;
  readonly updatedAt: string;
}

export interface EvidenceBundle {
  readonly evidence_id: string;
  readonly challenge_id: string;
  readonly source_type: "primary" | "reliable_secondary" | "commentary" | "user_private" | "model_inference";
  readonly url: string;
  readonly title: string;
  readonly publisher: string;
  readonly published_at: string | null;
  readonly retrieved_at: string;
  readonly excerpt: string;
  readonly user_note: string;
  readonly content_hash: string;
  readonly grade: EvidenceGrade;
  readonly supports_or_challenges: "supports" | "challenges" | "context";
  readonly independently_reviewable: boolean;
  readonly search_provider?: string;
  readonly search_result_hash?: string;
}

export interface RubricItem {
  readonly id: string;
  readonly label: string;
  readonly score: number;
  readonly critical: boolean;
  readonly rationale: string;
}

export interface TranscriptTurn {
  readonly id: string;
  readonly author: "user" | "challenger" | "system";
  readonly body: string;
}

export interface BranchNode {
  readonly id: string;
  readonly time: string;
  readonly event: string;
  readonly occurrenceDate: string;
  readonly revealDate: string;
  readonly dataAsOf: string;
  readonly observableIndicators: readonly string[];
  readonly causalParents: readonly string[];
  readonly assetAnchors: readonly string[];
  readonly sourceRefs: readonly string[];
  readonly stateChange: string;
  readonly invalidation?: string;
}

export interface Branch {
  readonly kind: BranchKind;
  readonly title: string;
  readonly baselineDate: string;
  readonly horizonEnd: string;
  readonly dataAsOf: string;
  readonly fixtureState: FixtureState;
  readonly premise: ProbabilityInterval;
  readonly premisePoint: number;
  readonly conditional: ProbabilityInterval;
  readonly conditionalPoint: number;
  readonly joint: ProbabilityInterval;
  readonly jointPoint: number;
  readonly description: string;
  readonly reveal: string;
  readonly nodes: readonly BranchNode[];
  readonly assetAnchors: readonly string[];
  readonly sourceRefs: readonly string[];
}

export interface AssetFixture {
  readonly id: string;
  readonly name: string;
  readonly role: string;
  readonly instrument: string;
  readonly quoteCurrency: Currency;
  readonly price: number;
  readonly lotSize: number;
  readonly fractional: boolean;
  readonly tradable: boolean;
  readonly proxy?: string;
  readonly liquidity: "high" | "medium" | "low";
  readonly volatility: "low" | "high";
  readonly assetClass: AssetClass;
  readonly asOf: string;
  readonly freshness: "current" | "historical_only" | "stale";
}

export interface FreshnessContext {
  readonly runtimeAsOf: string;
  readonly providerStatus: ProviderFreshnessStatus;
  /** FX is an independent public-data gate; absent means unavailable. */
  readonly fxProviderStatus?: ProviderFreshnessStatus;
  readonly fxAsOf?: string;
  readonly fxCnyPerUnit?: Readonly<Record<string, number>>;
}

export interface FreshnessOptions extends FreshnessContext {
  readonly policy?: { readonly maxAgeMs: number };
}

export interface AllocationRow {
  readonly asset: AssetFixture;
  readonly rawWeight: number;
  readonly publicWeight: number;
  readonly rawAmount: number;
  readonly publicAmount: number;
  readonly rawUnits: number;
  readonly publicUnits: number;
  readonly rawResidual: number;
  readonly publicResidual: number;
  readonly rawExecutable: boolean;
  readonly publicExecutable: boolean;
  readonly reasons: readonly string[];
  readonly freshness: FreshnessDecision["status"];
  readonly freshnessLabel: FreshnessDecision["label"];
  readonly publicExecutionAllowed: boolean;
}

export interface NavPoint {
  readonly date: string;
  readonly nav: number;
  readonly returnFromStart: number;
  readonly drawdown: number;
  readonly segment: "historical" | "simulated";
}

export interface ScenarioNav {
  readonly branch: BranchKind;
  readonly dataAsOf: string;
  readonly simulatedFrom: string;
  readonly fixtureState: FixtureState;
  readonly points: readonly NavPoint[];
  readonly scenarioReturn: number;
  readonly maxDrawdown: number;
}

/** JSON-safe FM output retained by the offline archive. Typed arrays/maps are
 * intentionally reduced to the fields required to recreate the portfolio. */
export interface ArchivedFmResult {
  readonly cutoff: string;
  readonly selectedFactorIds: readonly number[];
  readonly z: readonly number[];
  readonly weights: readonly number[];
  readonly fallback: boolean;
  readonly forecasts: readonly number[];
  readonly forecastScale: { readonly crossSectionalVolatility: readonly number[]; readonly returnScale: number };
  readonly metrics: { readonly icSeries: readonly number[]; readonly icMean: number; readonly icir: number } | null;
  readonly rebalance: ComputeFmResult["rebalance"];
  readonly deadband: ComputeFmResult["deadband"];
  readonly reasons: ComputeFmResult["reasons"];
  readonly rawClientResult: { readonly targetWeights: readonly number[]; readonly forecastReturns: readonly number[]; readonly transactionCost: number; readonly reasons: readonly string[] };
}

export interface ResearchMethodData {
  readonly methodVersion: string;
  readonly dataAsOf: string;
  readonly navLabel: string;
  readonly fixtureState: FixtureState;
  readonly assetIds: readonly string[];
}

export interface ResearchSnapshot {
  readonly schema: "f9-research-snapshot-v1";
  readonly stage: Stage;
  readonly language: Language;
  readonly viewpoint: string;
  readonly capital: string;
  readonly currency: Currency;
  readonly challengeStatus: ChallengeStatus;
  readonly challengeFailure: ChallengeFailure;
  readonly challengeTurns: readonly TranscriptTurn[];
  readonly replyDraft: string;
  readonly rubric: readonly RubricItem[];
  readonly evidence: readonly EvidenceBundle[];
  readonly searchStatus: AppState["searchStatus"];
  readonly confirmation: AppState["confirmation"];
  /** Optional for backwards-compatible restore of v1 archives. */
  readonly unresolvedIssuesAcknowledged?: boolean;
  /** Optional for backwards-compatible restore of v1 archives. */
  readonly reservationNote?: string;
  readonly selectedBranch: BranchKind;
  readonly branches: readonly Branch[];
  readonly fmStatus: AppState["fmStatus"];
  readonly fmProgress: number;
  readonly fmStep: string;
  readonly fmResult: ArchivedFmResult | null;
  readonly portfolioMode: PortfolioMode;
  readonly methodData: ResearchMethodData;
}

export interface AppState {
  readonly stage: Stage;
  readonly language: Language;
  readonly viewpoint: string;
  readonly capital: string;
  readonly currency: Currency;
  readonly draftRestored: boolean;
  readonly challengeStatus: ChallengeStatus;
  readonly challengeFailure: ChallengeFailure;
  readonly challengeTurns: readonly TranscriptTurn[];
  readonly replyDraft: string;
  readonly rubric: readonly RubricItem[];
  readonly evidence: readonly EvidenceBundle[];
  readonly branchSnapshot: readonly Branch[] | null;
  readonly searchStatus: "idle" | "mock_ready" | "loading" | "available" | "api_unavailable" | "api_failed";
  readonly confirmation: "pending" | "confirmed" | "reserved";
  readonly unresolvedIssuesAcknowledged: boolean;
  readonly reservationNote: string;
  readonly selectedBranch: BranchKind;
  readonly fmStatus: "idle" | "computing" | "complete" | "cancelled" | "failed";
  readonly fmProgress: number;
  readonly fmStep: string;
  readonly fmResult: ComputeFmResult | null;
  readonly portfolioMode: PortfolioMode;
  readonly exportNotice: string;
  readonly freshness: FreshnessContext;
}

export type AppAction =
  | { readonly type: "set_language"; readonly language: Language }
  | { readonly type: "set_viewpoint"; readonly viewpoint: string }
  | { readonly type: "set_capital"; readonly capital: string }
  | { readonly type: "set_currency"; readonly currency: Currency }
  | { readonly type: "restore_draft"; readonly draft: DraftState }
  | { readonly type: "start_challenge" }
  | { readonly type: "set_reply_draft"; readonly draft: string }
  | { readonly type: "submit_reply"; readonly body: string }
  | { readonly type: "challenge_cancel" }
  | { readonly type: "challenge_retry" }
  | { readonly type: "challenge_fail"; readonly failure: Exclude<ChallengeFailure, null> }
  | { readonly type: "challenge_complete" }
  | { readonly type: "add_evidence"; readonly evidence: EvidenceBundle }
  | { readonly type: "set_search_status"; readonly status: AppState["searchStatus"] }
  | { readonly type: "set_unresolved_issues_acknowledged"; readonly acknowledged: boolean }
  | { readonly type: "set_reservation_note"; readonly note: string }
  | { readonly type: "confirm"; readonly confirmation: "confirmed" | "reserved" }
  | { readonly type: "select_branch"; readonly branch: BranchKind }
  | { readonly type: "fm_start" }
  | { readonly type: "fm_progress"; readonly progress: number; readonly step: string }
  | { readonly type: "fm_complete"; readonly result: ComputeFmResult }
  | { readonly type: "fm_cancel" }
  | { readonly type: "set_mode"; readonly mode: PortfolioMode }
  | { readonly type: "export_notice"; readonly notice: string }
  | { readonly type: "set_freshness"; readonly freshness: FreshnessContext }
  | { readonly type: "reset" };
