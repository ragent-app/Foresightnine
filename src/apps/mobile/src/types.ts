import type { ComputeFmResult } from "@foresightnine/fm-engine";

export type Language = "zh" | "en";
export type Currency = "CNY" | "USD" | "EUR";
export type Stage = "thesis" | "challenge" | "evidence" | "worldline" | "portfolio" | "export";
export type ChallengeStatus = "idle" | "streaming" | "cancelled" | "recovering" | "failed" | "complete";
export type ChallengeFailure = "timeout" | "rate_limited" | "invalid_provider" | "no_search" | null;
export type BranchKind = "weak" | "main" | "strong";
export type PortfolioMode = "raw" | "public";
export type EvidenceGrade = "A" | "B" | "C" | "D" | "E";

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
  readonly invalidation?: string;
}

export interface Branch {
  readonly kind: BranchKind;
  readonly title: string;
  readonly interval: { readonly low: number; readonly high: number };
  readonly conditional: { readonly low: number; readonly high: number };
  readonly joint: { readonly low: number; readonly high: number };
  readonly description: string;
  readonly reveal: string;
  readonly nodes: readonly BranchNode[];
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
  readonly asOf: string;
  readonly freshness: "current" | "stale";
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
  readonly reasons: readonly string[];
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
  readonly searchStatus: "idle" | "mock_ready" | "api_unavailable";
  readonly confirmation: "pending" | "confirmed" | "reserved";
  readonly selectedBranch: BranchKind;
  readonly fmStatus: "idle" | "computing" | "complete" | "cancelled" | "failed";
  readonly fmProgress: number;
  readonly fmStep: string;
  readonly fmResult: ComputeFmResult | null;
  readonly portfolioMode: PortfolioMode;
  readonly exportNotice: string;
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
  | { readonly type: "confirm"; readonly confirmation: "confirmed" | "reserved" }
  | { readonly type: "select_branch"; readonly branch: BranchKind }
  | { readonly type: "fm_start" }
  | { readonly type: "fm_progress"; readonly progress: number; readonly step: string }
  | { readonly type: "fm_complete"; readonly result: ComputeFmResult }
  | { readonly type: "fm_cancel" }
  | { readonly type: "set_mode"; readonly mode: PortfolioMode }
  | { readonly type: "export_notice"; readonly notice: string }
  | { readonly type: "reset" };
