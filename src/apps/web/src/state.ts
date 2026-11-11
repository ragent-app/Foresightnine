import { legacyLiveScenarios } from "./scenario-status";
import type { ComputeFmResult } from "@foresightnine/fm-engine";
import { ASSET_FIXTURES, DEFAULT_RUNTIME_AS_OF, FRESHNESS_POLICY, MARKET_DATA_AS_OF, PROVIDER_FRESHNESS_STATUS, PUBLIC_CONSTRAINTS, RUNTIME_AS_OF, freshnessFor, fxToBase, hasCurrentFx, isPublicEligible } from "./fixtures";
import { isSafeEvidenceUrl, parseCapital } from "./validation";
import type { AllocationRow, AppAction, AppState, AssetFixture, BranchView, ChallengeStatus, Currency, EvidenceView, FreshnessOptions, PortfolioMode, PublicConstraintReason, RubricView, TranscriptTurn } from "./types";

export const rubricDefaults: readonly RubricView[] = [
  { label: "前提清晰度", score: 3, max: 4, rationale: "观点已提交，但仍需确认关键假设。" },
  { label: "时间边界", score: 4, max: 4, rationale: "基线与 24 个月窗口明确。" },
  { label: "触发可观察性", score: 3, max: 4, rationale: "已提出可观察的需求、投资或订单信号。" },
  { label: "因果链", score: 3, max: 4, rationale: "触发事件与资产影响已连接。" },
  { label: "反事实与替代解释", score: 2, max: 4, rationale: "需要补充融资条件恶化或需求不及预期的替代路径。" },
  { label: "证据质量", score: 1, max: 4, rationale: "尚未附上可独立复核的公开证据。" },
  { label: "资产传导", score: 3, max: 4, rationale: "资产方向已提出，但传导强度仍需证据支持。" },
  { label: "市场已定价程度", score: 2, max: 4, rationale: "尚未证明市场是否已经反映该观点。" },
  { label: "可证伪性", score: 3, max: 4, rationale: "已有可观察指标，公开证据仍需补强。" },
  { label: "用户确认", score: 1, max: 4, rationale: "等待用户回复反方问题并选择确认方式。" },
];

export const rubricDefaultsEn: readonly RubricView[] = [
  { label: "Premise clarity", score: 3, max: 4, rationale: "The thesis is submitted, but its key assumptions still need confirmation." },
  { label: "Time boundary", score: 4, max: 4, rationale: "The baseline and 24-month window are explicit." },
  { label: "Observable trigger", score: 3, max: 4, rationale: "Observable demand, investment, or order signals are proposed." },
  { label: "Causal chain", score: 3, max: 4, rationale: "The trigger and asset impact are connected." },
  { label: "Counterfactuals", score: 2, max: 4, rationale: "Add alternative paths such as tighter financing or weaker demand." },
  { label: "Evidence quality", score: 1, max: 4, rationale: "No independently reviewable public evidence has been attached yet." },
  { label: "Asset transmission", score: 3, max: 4, rationale: "The asset direction is proposed, but transmission strength still needs evidence." },
  { label: "Priced-in risk", score: 2, max: 4, rationale: "It is not yet established how much of the thesis is already priced in." },
  { label: "Falsifiability", score: 3, max: 4, rationale: "Observable indicators exist, but public evidence still needs strengthening." },
  { label: "User confirmation", score: 1, max: 4, rationale: "Waiting for the user's counterargument response and confirmation choice." },
];

export function rubricFor(language: AppState["language"]): readonly RubricView[] {
  return language === "en" ? rubricDefaultsEn : rubricDefaults;
}

function localizeRubric(rubric: readonly RubricView[], language: AppState["language"]): readonly RubricView[] {
  const defaults = rubricFor(language);
  return defaults.map((item, index) => ({ ...item, score: rubric[index]?.score ?? item.score }));
}

function rubricLabel(language: AppState["language"], zhLabel: string): string {
  const index = rubricDefaults.findIndex((item) => item.label === zhLabel);
  return rubricFor(language)[index]?.label ?? zhLabel;
}

export const initialState: AppState = {
  stage: "thesis",
  language: "zh",
  viewpoint: "",
  capital: "100000",
  currency: "CNY",
  draftRestored: false,
  challengeStatus: "idle",
  challengeTurns: [],
  replyDraft: "",
  evidenceDraft: {url:"", title:"", publisher:"", published_at:"", excerpt:"", user_note:"", supports_or_challenges:"supports", grade:"B"},
  evidenceSearchQuery: "",
  rubric: rubricDefaults,
  evidence: [],
  confirmation: "pending",
  reviewConfirmation: "pending",
  unresolvedIssuesAcknowledged: false,
  reservationNote: "",
  selectedBranch: "main",
  fmStatus: "idle",
  fmProgress: 0,
  fmStep: "等待本地计算",
  fmResult: null,
  portfolioMode: "raw",
  stressScenario: "main",
  drawdownVisible: true,
  exportNotice: "",
  freshness: { runtimeAsOf: DEFAULT_RUNTIME_AS_OF, providerStatus: PROVIDER_FRESHNESS_STATUS, fxProviderStatus: "unavailable" },
  apiSessionId: null,
  apiChallengeId: null,
  apiOperationId: null,
  apiCompletedOperationId: null,
  apiPendingPhase: null,
  apiError: "",
  serverBranches: null,
};

function challengeTurnsFor(viewpoint: string, language: AppState["language"]): readonly [TranscriptTurn, TranscriptTurn] {
  const premise = viewpoint.trim() || (language === "en" ? "(no thesis entered)" : "（尚未填写观点）");
  const challengerBody = language === "en"
    ? `Strongest counterargument around "${premise}": which observable evidence would show this causal chain is occurring? If the key trigger does not appear, what would invalidate the conclusion?`
    : `围绕「${premise}」，最强反方问题是：哪些可观察证据能证明这条因果链正在发生？如果关键触发条件没有出现，哪一项会使结论失效？`;
  return [
    { id: "user-1", author: "user", time: "现在", body: premise },
    { id: "challenger-1", author: "challenger", time: "现在", body: challengerBody },
  ];
}

/** Replies are valid only after a started challenge has a usable transcript. */
export function canSubmitReply(status: ChallengeStatus, turns: readonly TranscriptTurn[]): boolean {
  if (!["streaming", "recovering", "complete"].includes(status) || !hasStartedChallenge(turns)) return false;
  const challengerIndex = turns.reduce((latest, turn, index) => turn.author === "challenger" ? index : latest, -1);
  return challengerIndex >= 0 && !turns.slice(challengerIndex + 1).some((turn) => turn.author === "user");
}

function hasStartedChallenge(turns: readonly TranscriptTurn[]): boolean {
  if (!Array.isArray(turns) || turns.length < 2) return false;
  const ids = new Set<string>();
  for (const turn of turns) {
    if (!turn || typeof turn.id !== "string" || turn.id.trim().length === 0 || ids.has(turn.id) || !["user", "challenger", "system"].includes(turn.author) || typeof turn.body !== "string" || turn.body.trim().length === 0) return false;
    ids.add(turn.id);
  }
  return turns.some((turn) => turn.author === "challenger");
}

/** A completed challenge must contain a real challenger turn followed by a user reply. */
export function hasValidCompletedChallenge(status: ChallengeStatus, turns: readonly TranscriptTurn[]): boolean {
  if (status !== "complete" || !hasStartedChallenge(turns)) return false;
  const challengerIndex = turns.findIndex((turn) => turn.author === "challenger");
  return challengerIndex >= 0 && turns.slice(challengerIndex + 1).some((turn) => turn.author === "user");
}

function resetWorkflow(state: AppState, viewpoint: string): AppState {
  return { ...initialState, language: state.language, viewpoint, capital: state.capital, currency: state.currency, rubric: rubricFor(state.language), draftRestored: false };
}

export function isReviewLocked(state: Pick<AppState, "confirmation"> & Partial<Pick<AppState, "reviewConfirmation">>): boolean {
  return state.confirmation !== "pending" || (state.reviewConfirmation !== undefined && state.reviewConfirmation !== "pending");
}

export function reducer(state: AppState, action: AppAction): AppState {
  if (isReviewLocked(state) && ["submit_reply", "begin_reply_submit", "add_evidence", "set_reservation_note", "set_unresolved_issues_acknowledged", "set_reply_draft", "set_evidence_draft", "set_evidence_query"].includes(action.type)) return state;
  switch (action.type) {
    case "set_evidence_draft": return {...state,evidenceDraft:action.draft};
    case "set_evidence_query": return {...state,evidenceSearchQuery:action.query};
    case "restore_workspace": return { ...action.state, stage: safeStageFor(action.state), draftRestored: true };
    case "api_operation_pending": return { ...state, apiOperationId: action.operationId, apiPendingPhase: action.phase };
    case "api_operation_finished": return { ...state, apiOperationId: action.operationId, apiCompletedOperationId: action.operationId, apiPendingPhase: null };
    case "set_stage": return canEnterStage(state, action.stage) ? { ...state, stage: action.stage } : { ...state, stage: safeStageFor(state) };
    case "set_language": return { ...state, language: action.language, rubric: localizeRubric(state.rubric, action.language) };
    case "set_viewpoint": {
      if (action.viewpoint === state.viewpoint) return { ...state, draftRestored: false };
      // A viewpoint is the root identity of every downstream artifact.  Keep
      // only the user inputs and locale when it changes; no challenge,
      // evidence, worldline, FM, or portfolio state may cross that boundary.
      return resetWorkflow(state, action.viewpoint);
    }
    case "set_capital": return { ...state, capital: action.capital, draftRestored: false };
    case "set_currency": return { ...state, currency: action.currency, draftRestored: false };
    case "restore_draft": return { ...state, viewpoint: action.draft.viewpoint, capital: action.draft.capital, currency: action.draft.currency, draftRestored: true };
    case "start_challenge": {
      const viewpoint = action.viewpoint ?? state.viewpoint;
      if (viewpoint.trim().length === 0 || parseCapital(state.capital) <= 0) return { ...state, stage: safeStageFor(state) };
      const fresh = resetWorkflow(state, viewpoint);
      return { ...fresh, stage: "challenge", challengeStatus: "streaming", challengeTurns: action.server ? [{ id: "api-user-pending", author: "user", time: "现在", body: viewpoint }] : [...challengeTurnsFor(viewpoint, state.language)] };
    }
    case "set_reply_draft": return { ...state, replyDraft: action.draft };
    case "begin_reply_submit": return action.body.trim().length === 0 || state.stage !== "challenge" || !canSubmitReply(state.challengeStatus, state.challengeTurns) ? state : { ...state, challengeStatus: "submitting_reply" };
    case "submit_reply": return action.body.trim().length === 0 || state.stage !== "challenge" || !canSubmitReply(state.challengeStatus, state.challengeTurns) ? state : { ...state, replyDraft: "", challengeStatus: "complete", fmStep: "反方审查完成", rubric: updateRubric(state.rubric, { [rubricLabel(state.language, "用户确认")]: { score: 3, rationale: state.language === "en" ? "The user answered the counterargument; a confirmation choice is still required." : "用户已回答反方问题，仍需选择确认方式。" } }), challengeTurns: [...state.challengeTurns, { id: `user-reply-${state.challengeTurns.length + 1}`, author: "user", time: "现在", body: action.body.trim() }] };
    case "challenge_complete": return state.stage === "challenge" && (state.challengeStatus === "streaming" || state.challengeStatus === "recovering") && hasValidCompletedChallenge("complete", state.challengeTurns) ? { ...state, challengeStatus: "complete", fmStep: "反方审查完成" } : state;
    case "challenge_cancel": return state.stage === "challenge" && (state.challengeStatus === "streaming" || state.challengeStatus === "recovering" || state.challengeStatus === "submitting_reply") && hasStartedChallenge(state.challengeTurns) ? { ...state, challengeStatus: "cancelled", challengeTurns: [...state.challengeTurns, { id: `cancelled-${state.challengeTurns.length + 1}`, author: "system", time: "现在", body: "已取消生成；已接收内容和证据仍保留。" }] } : state;
    case "challenge_recover": return ["cancelled", "failed"].includes(state.challengeStatus) && hasStartedChallenge(state.challengeTurns) ? { ...state, challengeStatus: "recovering" } : state;
    case "challenge_fail": return state.stage === "challenge" && (state.challengeStatus === "streaming" || state.challengeStatus === "recovering") && hasStartedChallenge(state.challengeTurns) ? { ...state, challengeStatus: "failed" } : state;
    case "add_evidence": return state.evidence.some((item) => item.evidence_id === action.evidence.evidence_id) ? state : { ...state, rubric: updateRubric(state.rubric, { [rubricLabel(state.language, "证据质量")]: { score: 3, rationale: state.language === "en" ? "Evidence is attached; source grade and reviewability remain explicit." : "已附上证据；来源等级与可复核状态仍保留。" }, [rubricLabel(state.language, "反事实与替代解释")]: { score: 3, rationale: state.language === "en" ? "Evidence is being used to test alternatives; continued review is required." : "证据已用于检验替代解释，仍需持续复核。" }, [rubricLabel(state.language, "市场已定价程度")]: { score: 3, rationale: state.language === "en" ? "Evidence is included in the priced-in review; it is not a fact judgment." : "证据已进入已定价程度审查，仍不是事实判断。" } }), evidence: [...state.evidence, action.evidence] };
    case "set_unresolved_issues_acknowledged": return { ...state, unresolvedIssuesAcknowledged: action.acknowledged };
    case "set_reservation_note": return { ...state, reservationNote: action.note };
    case "confirm": {
      const allowed = action.confirmation === "confirmed" ? canConfirmNormally(state) : canConfirmReserved(state);
      return !allowed ? state : { ...state, confirmation: action.confirmation, reviewConfirmation: action.confirmation, stage: "worldline" };
    }
    case "api_review_confirmed": return { ...state, reviewConfirmation: action.confirmation };
    case "select_branch": return { ...state, selectedBranch: action.branch };
    case "fm_start": return canStartFm(state) ? { ...state, stage: "fm", fmStatus: "computing", fmProgress: 8, fmStep: "正在获取金融数据…", exportNotice: "" } : state;
    case "fm_progress": return state.fmStatus === "computing" && state.stage === "fm" ? { ...state, fmProgress: action.progress, fmStep: action.step } : state;
    case "fm_complete": return state.fmStatus === "computing" && state.stage === "fm" ? { ...state, fmStatus: "complete", fmProgress: 100, fmStep: "本地计算完成，原始结果已保留", fmResult: action.result, stage: "fm" } : state;
    case "fm_cancel": return state.stage === "fm" && state.fmStatus === "computing" ? { ...state, fmStatus: "cancelled", fmStep: "本地计算已取消；中间结果未上传。" } : state;
    case "fm_fail": return state.stage === "fm" && state.fmStatus === "computing" ? { ...state, fmStatus: "failed", fmStep: "计算失败。研究状态仍已保存，可重试。" } : state;
    case "set_mode": return { ...state, portfolioMode: action.mode };
    case "set_stress": return { ...state, stressScenario: action.scenario };
    case "toggle_drawdown": return { ...state, drawdownVisible: !state.drawdownVisible };
    case "export_notice": return { ...state, exportNotice: action.notice };
    case "set_freshness": return { ...state, freshness: action.freshness };
    case "api_session_ready": return { ...state, apiSessionId: action.sessionId, apiError: "" };
    case "api_challenge_update": return { ...state, apiChallengeId: action.challengeId, challengeTurns: action.turns, rubric: action.rubric, challengeStatus: action.status, apiError: "", fmStep: action.status === "complete" ? "反方审查完成" : state.fmStep };
    case "api_worldlines_ready": return { ...state, serverBranches: action.branches, apiError: "",
      ...(state.reviewConfirmation !== "pending" ? {confirmation:state.reviewConfirmation,stage:"worldline" as const} : {}),
      ...(state.serverBranches?.[0]?.worldline.bundle_id !== action.branches[0]?.worldline.bundle_id ? {fmResult:null,fmStatus:"idle" as const,fmProgress:0} : {}) };
    case "api_error": return { ...state, apiError: action.notice, ...(state.stage === "challenge" && (state.challengeStatus === "streaming" || state.challengeStatus === "submitting_reply" || state.challengeStatus === "recovering") ? { challengeStatus: "failed" as const } : {}) };
    case "api_cancel": return { ...state, challengeStatus: "cancelled", apiError: "" };
    case "reset": return { ...initialState, language: state.language, rubric: rubricFor(state.language) };
  }
  return state;
}

export function totalRubricScore(rubric: readonly RubricView[]): number { return rubric.reduce((sum, item) => sum + item.score, 0); }

export function updateRubric(rubric: readonly RubricView[], updates: Readonly<Record<string, { readonly score: number; readonly rationale: string }>>): readonly RubricView[] {
  return rubric.map((item) => updates[item.label] ? { ...item, ...updates[item.label], score: Math.max(0, Math.min(item.max, updates[item.label]!.score)) } : item);
}

const PUBLIC_EVIDENCE_SOURCE_GRADE: Readonly<Record<EvidenceView["source_type"], EvidenceView["grade"]>> = {
  primary: "A",
  reliable_secondary: "B",
  commentary: "C",
  user_private: "D",
  model_inference: "E",
};

function isSemanticallyValidEvidence(item: EvidenceView): boolean {
  const expectedGrade = PUBLIC_EVIDENCE_SOURCE_GRADE[item.source_type];
  return expectedGrade === item.grade && (item.grade === "D" || item.grade === "E" || isSafeEvidenceUrl(item.url.trim()));
}

function isDerivedPublicEvidence(item: EvidenceView): boolean {
  return (item.grade === "A" || item.grade === "B" || item.grade === "C")
    && PUBLIC_EVIDENCE_SOURCE_GRADE[item.source_type] === item.grade
    && isSafeEvidenceUrl(item.url.trim());
}

function isCanonicalRubric(rubric: readonly RubricView[], language: AppState["language"]): boolean {
  const canonical = rubricFor(language);
  return rubric.length === canonical.length && rubric.every((item, index) => {
    const expected = canonical[index];
    return expected !== undefined && item.label === expected.label && item.max === expected.max && Number.isSafeInteger(item.score) && item.score >= 0 && item.score <= item.max && typeof item.rationale === "string";
  });
}

type ConfirmationState = Pick<AppState, "challengeStatus" | "challengeTurns" | "rubric" | "evidence" | "language" | "unresolvedIssuesAcknowledged" | "reservationNote">;

export function canConfirmNormally(state: ConfirmationState): boolean {
  if (!hasValidCompletedChallenge(state.challengeStatus, state.challengeTurns) || !isCanonicalRubric(state.rubric, state.language) || totalRubricScore(state.rubric) < 30) return false;
  const requiredIndices = [0, 1, 3, 6, 8];
  if (requiredIndices.some((index) => (state.rubric[index]?.score ?? 0) < 3)) return false;
  return state.evidence.every(isSemanticallyValidEvidence) && state.evidence.some(isDerivedPublicEvidence);
}

export function canConfirmReserved(state: ConfirmationState): boolean {
  return hasValidCompletedChallenge(state.challengeStatus, state.challengeTurns)
    && isCanonicalRubric(state.rubric, state.language)
    && state.evidence.every(isSemanticallyValidEvidence)
    && state.unresolvedIssuesAcknowledged
    && state.reservationNote.trim().length > 0;
}

export function canEnterWorldline(state: Pick<AppState, "challengeStatus" | "challengeTurns" | "rubric" | "evidence" | "language" | "confirmation" | "unresolvedIssuesAcknowledged" | "reservationNote"> & Partial<Pick<AppState,"apiChallengeId"|"reviewConfirmation"|"serverBranches">>): boolean {
  // Server confirmation is a durable decision, not a fresh local rubric vote.
  // It only unlocks navigation once the owner's actual bundle is present.
  if (state.apiChallengeId && state.reviewConfirmation && state.reviewConfirmation !== "pending") return state.serverBranches?.length === 3;
  return state.confirmation === "confirmed" ? canConfirmNormally(state) : state.confirmation === "reserved" ? canConfirmReserved(state) : false;
}

export function canStartFm(state: Pick<AppState, "stage" | "challengeStatus" | "challengeTurns" | "rubric" | "evidence" | "language" | "confirmation" | "unresolvedIssuesAcknowledged" | "reservationNote"> & Partial<Pick<AppState,"serverBranches">>): boolean {
  return !legacyLiveScenarios(state.serverBranches) && (state.stage === "worldline" || state.stage === "fm") && canEnterWorldline(state);
}

export function canEnterStage(state: AppState, stage: AppState["stage"]): boolean {
  if (["fm","portfolio","nav"].includes(stage) && legacyLiveScenarios(state.serverBranches)) return false;
  switch (stage) {
    case "thesis":
    case "console":
      return true;
    case "challenge":
      return state.viewpoint.trim().length > 0 && parseCapital(state.capital) > 0;
    case "worldline":
      return canEnterWorldline(state);
    case "fm": {
      if (!canEnterWorldline(state)) return false;
      if (state.fmStatus === "complete") return state.fmResult !== null;
      if (state.stage === "worldline") return true;
      return state.stage === "fm" && (state.fmStatus === "computing" || state.fmStatus === "failed" || state.fmStatus === "cancelled");
    }
    case "portfolio":
    case "nav":
      return state.fmStatus === "complete" && state.fmResult !== null && canEnterWorldline(state);
  }
}

export function safeStageFor(state: AppState): AppState["stage"] {
  if (legacyLiveScenarios(state.serverBranches) && canEnterWorldline(state) && ["fm","portfolio","nav"].includes(state.stage)) return "worldline";
  if (canEnterStage(state, state.stage)) return state.stage;
  if (state.stage === "thesis" || state.stage === "challenge") return canEnterStage(state, "challenge") ? "challenge" : "thesis";
  if (!canEnterWorldline(state)) return canEnterStage(state, "challenge") ? "challenge" : "thesis";
  if (state.stage === "portfolio" || state.stage === "nav") return state.fmStatus === "complete" && state.fmResult !== null ? "fm" : "worldline";
  return "worldline";
}

const fallbackCash: AssetFixture = { assetId: "CASH", displayName: "现金等价物", economicExposureId: "cash", instrumentId: "CASH", assetClass: "cash", quoteCurrency: "CNY", price: 1, lotSize: 1, fractionalSupported: true, tradable: true, liquidity: "high", volatility: "low", asOf: MARKET_DATA_AS_OF, freshness: "current" };

function boundedProRata(indices: readonly number[], budget: number, base: readonly number[], assets: readonly AssetFixture[]): readonly number[] {
  const result = new Array<number>(base.length).fill(0);
  const open = new Set(indices);
  let remaining = budget;
  while (open.size > 0 && remaining > 1e-12) {
    const openIndices = [...open];
    const totalBase = openIndices.reduce((sum, index) => sum + Math.max(base[index] ?? 0, 0), 0);
    const share = totalBase > 0 ? (index: number) => remaining * Math.max(base[index] ?? 0, 0) / totalBase : () => remaining / openIndices.length;
    let fixed = false;
    for (const index of openIndices) {
      const cap = assets[index]?.assetClass === "cash" ? Number.POSITIVE_INFINITY : PUBLIC_CONSTRAINTS.singleSecurityMax;
      const proposed = share(index);
      if (proposed > cap + 1e-12) {
        result[index] = cap;
        remaining -= cap;
        open.delete(index);
        fixed = true;
      }
    }
    if (!fixed) {
      for (const index of openIndices) result[index] = share(index);
      remaining = 0;
    }
  }
  if (remaining > 1e-12) {
    const cashIndex = indices.find((index) => assets[index]?.assetClass === "cash");
    if (cashIndex !== undefined) result[cashIndex] = (result[cashIndex] ?? 0) + remaining;
  }
  return result;
}

function weightAssets(rawWeights: readonly number[], assets?: readonly AssetFixture[]): readonly AssetFixture[] {
  return rawWeights.map((_weight, index) => assets?.[index] ?? (index === rawWeights.length - 1 ? fallbackCash : ASSET_FIXTURES[index] ?? fallbackCash));
}

export function publicWeights(rawWeights: readonly number[], assets?: readonly AssetFixture[], freshnessOptions: FreshnessOptions = { runtimeAsOf: RUNTIME_AS_OF, providerStatus: PROVIDER_FRESHNESS_STATUS, policy: FRESHNESS_POLICY }): readonly number[] {
  const normalizedAssets = weightAssets(rawWeights, assets);
  const base = rawWeights.map((weight) => Math.max(0, Number.isFinite(weight) ? weight : 0));
  if (base.length === 0) return [];
  const eligible = normalizedAssets.map((asset, index) => isPublicEligible(asset, freshnessOptions) ? index : -1).filter((index) => index >= 0);
  if (eligible.length === 0) return new Array<number>(base.length).fill(0);
  const eligibleBaseTotal = eligible.reduce((sum, index) => sum + base[index]!, 0);
  const workingBase = base.map((value, index) => eligible.includes(index) ? (eligibleBaseTotal > 0 ? value / eligibleBaseTotal : 1 / eligible.length) : 0);
  const highVol = eligible.filter((index) => normalizedAssets[index]!.volatility === "high");
  const highVolShare = highVol.reduce((sum, index) => sum + workingBase[index]!, 0);
  const highBudget = Math.min(PUBLIC_CONSTRAINTS.highVolatilityClassMax, highVolShare);
  const lowVol = eligible.filter((index) => !highVol.includes(index));
  const resultHigh = boundedProRata(highVol, highBudget, workingBase, normalizedAssets);
  const resultLow = boundedProRata(lowVol, 1 - highBudget, workingBase, normalizedAssets);
  const result = resultHigh.map((value, index) => value + (resultLow[index] ?? 0));
  const residual = 1 - result.reduce((sum, value) => sum + value, 0);
  const cashIndex = eligible.find((index) => normalizedAssets[index]!.assetClass === "cash");
  if (cashIndex !== undefined && Math.abs(residual) > 1e-12) result[cashIndex] = (result[cashIndex] ?? 0) + residual;
  return result;
}

function eligibilityReason(asset: AssetFixture, freshnessOptions: FreshnessOptions): PublicConstraintReason | undefined {
  if (!hasCurrentFx(freshnessOptions, ["CNY", asset.quoteCurrency])) return { code: "fx_unavailable", label: "汇率数据不可用", detail: "Public Default 需要同一数据时点的受支持货币汇率；当前仅保留 FM Raw/历史展示。" };
  if (!asset.tradable && !asset.proxyInstrument) return { code: "not_tradable", label: "不可直接交易", detail: `${asset.displayName} 没有可用 proxyInstrument。` };
  const freshness = freshnessFor(asset, freshnessOptions);
  if (!freshness.allowCurrentPublicDefault) return { code: "stale_data", label: "过期/历史数据", detail: `Public Default 禁止使用 ${freshness.status} 数据；data as-of ${asset.asOf}。` };
  if (asset.liquidity === "low" || asset.liquidity === "restricted") return { code: "low_liquidity", label: "流动性不足", detail: `liquidity=${asset.liquidity}，不进入 Public Default。` };
  return undefined;
}

export function allocationRows(result: ComputeFmResult | null, capital: number, baseCurrency: Currency = "CNY", mode: PortfolioMode = "raw", freshnessOptions: FreshnessOptions = { runtimeAsOf: RUNTIME_AS_OF, providerStatus: PROVIDER_FRESHNESS_STATUS, policy: FRESHNESS_POLICY }, assets: readonly AssetFixture[] = ASSET_FIXTURES): readonly AllocationRow[] {
  if (!result) return [];
  const raw = result.weights.weights;
  const weights = assets.map((_asset, index) => Math.max(0, Number.isFinite(raw[index] ?? 0) ? raw[index] ?? 0 : 0));
  const publicized = publicWeights(weights, assets, freshnessOptions);
  const highVolTotal = publicized.reduce((sum, weight, index) => sum + (assets[index]!.volatility === "high" ? weight : 0), 0);
  const roles: Readonly<Record<string, string>> = { GRID: "核心受益", CHIP: "次级受益", UTIL: "防守", GOLD: "对冲", WORLD: "分散", CASH: "流动性" };
  return assets.map((asset, index) => {
    const weight = weights[index] ?? 0;
    const publicWeight = publicized[index] ?? 0;
    const fx = fxToBase(asset.quoteCurrency, baseCurrency, freshnessOptions.fxCnyPerUnit);
    const conversion = Number.isFinite(fx) && fx > 0 ? fx : 1;
    const rawDesired = capital * weight / (asset.price * conversion);
    const publicDesired = capital * publicWeight / (asset.price * conversion);
    const rawUnits = asset.fractionalSupported ? rawDesired : Math.floor(rawDesired / asset.lotSize) * asset.lotSize;
    const publicUnits = asset.fractionalSupported ? publicDesired : Math.floor(publicDesired / asset.lotSize) * asset.lotSize;
    const rawAmount = rawUnits * asset.price * conversion;
    const publicAmount = publicUnits * asset.price * conversion;
    const rawTarget = capital * weight;
    const publicTarget = capital * publicWeight;
    const rawResidualReason = rawUnits === 0 && rawTarget > 0 ? "Raw 金额过小，无法满足最小交易单位。" : rawAmount + 1e-9 < rawTarget ? `Raw lot rounding residual ${formatResidual(rawTarget - rawAmount, baseCurrency)}。` : undefined;
    const publicResidualReason = publicUnits === 0 && publicTarget > 0 ? "Public Default 金额过小，无法满足最小交易单位。" : publicAmount + 1e-9 < publicTarget ? `Public lot rounding residual ${formatResidual(publicTarget - publicAmount, baseCurrency)}。` : undefined;
    const constraintReasons: PublicConstraintReason[] = [];
    const freshness = freshnessFor(asset, freshnessOptions);
    const eligibility = eligibilityReason(asset, freshnessOptions);
    if (eligibility) constraintReasons.push(eligibility);
    if (!asset.tradable && asset.proxyInstrument) constraintReasons.push({ code: "proxy_mapping", label: "代理映射", detail: `${asset.assetId} 非直接交易资产，使用 ${asset.proxyInstrument} 代理。` });
    if (asset.volatility === "high" && highVolTotal >= PUBLIC_CONSTRAINTS.highVolatilityClassMax - 1e-9 && publicWeight < weight - 1e-9) constraintReasons.push({ code: "high_volatility_cap", label: "高波动类别上限", detail: `高波动资产类别合计上限 ${PUBLIC_CONSTRAINTS.highVolatilityClassMax * 100}%。` });
    if (asset.assetClass !== "cash" && publicWeight < weight - 1e-9 && !constraintReasons.some((reason) => reason.code === "high_volatility_cap")) constraintReasons.push({ code: "single_security_cap", label: "单证券上限", detail: `单证券上限 ${PUBLIC_CONSTRAINTS.singleSecurityMax * 100}%。释放权重按可用资产原始权重比例确定性再分配。` });
    if (publicWeight > weight + 1e-9) constraintReasons.push({ code: "redistributed_weight", label: "释放权重再分配", detail: "其他不合资格或受限资产释放的权重按可用资产原始权重比例再分配。" });
    const selectedAmount = mode === "public" ? publicAmount : rawAmount;
    const selectedUnits = mode === "public" ? publicUnits : rawUnits;
    const row: AllocationRow = { id: asset.assetId, role: roles[asset.assetId] ?? asset.displayName, weight, publicWeight, amount: selectedAmount, units: selectedUnits, rawAmount, rawUnits, publicAmount, publicUnits, currency: baseCurrency, price: asset.price, quoteCurrency: asset.quoteCurrency, lotSize: asset.lotSize, constraintReasons, dataAsOf: asset.asOf, instrumentId: asset.instrumentId, tradable: asset.tradable, liquidity: asset.liquidity, volatility: asset.volatility, freshness: freshness.status, freshnessLabel: freshness.label, publicExecutionAllowed: isPublicEligible(asset, freshnessOptions) };
    return { ...row, ...(rawResidualReason ? { rawResidualReason } : {}), ...(publicResidualReason ? { publicResidualReason } : {}), ...(mode === "raw" && rawResidualReason ? { residualReason: rawResidualReason } : {}), ...(mode === "public" && publicResidualReason ? { residualReason: publicResidualReason } : {}), ...(asset.proxyInstrument ? { proxyInstrument: asset.proxyInstrument } : {}) };
  });
}

function formatResidual(value: number, currency: Currency): string { return `${currency} ${value.toFixed(2)}`; }

export function branchByKind(branches: readonly BranchView[], kind: BranchView["kind"]): BranchView { return branches.find((branch) => branch.kind === kind) ?? branches[1]!; }
