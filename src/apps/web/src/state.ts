import type { ComputeFmResult } from "@foresightnine/fm-engine";
import { ASSET_FIXTURES, MARKET_DATA_AS_OF, PUBLIC_CONSTRAINTS, fxToBase, isPublicEligible } from "./fixtures";
import type { AllocationRow, AppAction, AppState, AssetFixture, BranchView, Currency, EvidenceView, PortfolioMode, PublicConstraintReason, RubricView, TranscriptTurn } from "./types";

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

export const initialState: AppState = {
  stage: "thesis",
  viewpoint: "",
  capital: "100000",
  currency: "CNY",
  draftRestored: false,
  challengeStatus: "idle",
  challengeTurns: [],
  replyDraft: "",
  rubric: rubricDefaults,
  evidence: [],
  confirmation: "pending",
  selectedBranch: "main",
  fmStatus: "idle",
  fmProgress: 0,
  fmStep: "等待本地计算",
  fmResult: null,
  portfolioMode: "raw",
  stressScenario: "main",
  drawdownVisible: true,
  exportNotice: "",
};

function challengeTurnsFor(viewpoint: string): readonly [TranscriptTurn, TranscriptTurn] {
  const premise = viewpoint.trim() || "（尚未填写观点）";
  return [
    { id: "user-1", author: "user", time: "现在", body: premise },
    { id: "challenger-1", author: "challenger", time: "现在", body: `围绕「${premise}」，最强反方问题是：哪些可观察证据能证明这条因果链正在发生？如果关键触发条件没有出现，哪一项会使结论失效？` },
  ];
}

export function reducer(state: AppState, action: AppAction): AppState {
  switch (action.type) {
    case "set_stage": return { ...state, stage: action.stage };
    case "set_viewpoint": return { ...state, viewpoint: action.viewpoint, draftRestored: false };
    case "set_capital": return { ...state, capital: action.capital, draftRestored: false };
    case "set_currency": return { ...state, currency: action.currency, draftRestored: false };
    case "restore_draft": return { ...state, viewpoint: action.draft.viewpoint, capital: action.draft.capital, currency: action.draft.currency, draftRestored: true };
    case "start_challenge": return { ...state, stage: "challenge", challengeStatus: "streaming", challengeTurns: [...challengeTurnsFor(action.viewpoint ?? state.viewpoint)], replyDraft: "", exportNotice: "" };
    case "set_reply_draft": return { ...state, replyDraft: action.draft };
    case "submit_reply": return action.body.trim().length === 0 ? state : { ...state, replyDraft: "", rubric: updateRubric(state.rubric, { "用户确认": { score: 3, rationale: "用户已回答反方问题，仍需选择确认方式。" } }), challengeTurns: [...state.challengeTurns, { id: `user-reply-${state.challengeTurns.length + 1}`, author: "user", time: "现在", body: action.body.trim() }] };
    case "challenge_complete": return { ...state, challengeStatus: "complete", fmStep: "反方审查完成" };
    case "challenge_cancel": return { ...state, challengeStatus: "cancelled", challengeTurns: [...state.challengeTurns, { id: "cancelled-1", author: "system", time: "现在", body: "已取消生成；已接收内容和证据仍保留。" }] };
    case "challenge_recover": return { ...state, challengeStatus: "recovering" };
    case "challenge_fail": return { ...state, challengeStatus: "failed" };
    case "add_evidence": return state.evidence.some((item) => item.evidence_id === action.evidence.evidence_id) ? state : { ...state, rubric: updateRubric(state.rubric, { "证据质量": { score: 3, rationale: "已附上证据；来源等级与可复核状态仍保留。" }, "反事实与替代解释": { score: 3, rationale: "证据已用于检验替代解释，仍需持续复核。" }, "市场已定价程度": { score: 3, rationale: "证据已进入已定价程度审查，仍不是事实判断。" } }), evidence: [...state.evidence, action.evidence] };
    case "confirm": return action.confirmation === "confirmed" && !canConfirmNormally(state) ? state : { ...state, confirmation: action.confirmation, stage: "worldline" };
    case "select_branch": return { ...state, selectedBranch: action.branch };
    case "fm_start": return { ...state, stage: "fm", fmStatus: "computing", fmProgress: 8, fmStep: "正在验证签名数据快照…", exportNotice: "" };
    case "fm_progress": return { ...state, fmProgress: action.progress, fmStep: action.step };
    case "fm_complete": return { ...state, fmStatus: "complete", fmProgress: 100, fmStep: "本地计算完成，原始结果已保留", fmResult: action.result, stage: "fm" };
    case "fm_cancel": return { ...state, fmStatus: "cancelled", fmStep: "本地计算已取消；中间结果未上传。" };
    case "fm_fail": return { ...state, fmStatus: "failed", fmStep: "计算失败。研究状态仍已保存，可重试。" };
    case "set_mode": return { ...state, portfolioMode: action.mode };
    case "set_stress": return { ...state, stressScenario: action.scenario };
    case "toggle_drawdown": return { ...state, drawdownVisible: !state.drawdownVisible };
    case "export_notice": return { ...state, exportNotice: action.notice };
    case "reset": return initialState;
  }
  return state;
}

export function totalRubricScore(rubric: readonly RubricView[]): number { return rubric.reduce((sum, item) => sum + item.score, 0); }

export function updateRubric(rubric: readonly RubricView[], updates: Readonly<Record<string, { readonly score: number; readonly rationale: string }>>): readonly RubricView[] {
  return rubric.map((item) => updates[item.label] ? { ...item, ...updates[item.label], score: Math.max(0, Math.min(item.max, updates[item.label]!.score)) } : item);
}

export function canConfirmNormally(state: Pick<AppState, "challengeStatus" | "rubric" | "evidence">): boolean {
  if (state.challengeStatus !== "complete" || totalRubricScore(state.rubric) < 30) return false;
  const required = new Set(["前提清晰度", "时间边界", "因果链", "资产传导", "可证伪性"]);
  if ([...required].some((label) => (state.rubric.find((item) => item.label === label)?.score ?? 0) < 3)) return false;
  return state.evidence.some((item) => item.independently_reviewable && item.grade !== "D" && item.grade !== "E");
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

export function publicWeights(rawWeights: readonly number[], assets?: readonly AssetFixture[]): readonly number[] {
  const normalizedAssets = weightAssets(rawWeights, assets);
  const base = rawWeights.map((weight) => Math.max(0, Number.isFinite(weight) ? weight : 0));
  if (base.length === 0) return [];
  const eligible = normalizedAssets.map((asset, index) => isPublicEligible(asset) ? index : -1).filter((index) => index >= 0);
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

function eligibilityReason(asset: AssetFixture): PublicConstraintReason | undefined {
  if (!asset.tradable && !asset.proxyInstrument) return { code: "not_tradable", label: "不可直接交易", detail: `${asset.displayName} 没有可用 proxyInstrument。` };
  if (asset.freshness !== "current" || Date.parse(asset.asOf) > Date.parse(MARKET_DATA_AS_OF)) return { code: "stale_data", label: "过期/历史数据", detail: `Public Default 禁止使用 ${asset.freshness} 数据；data as-of ${asset.asOf}。` };
  if (asset.liquidity === "low" || asset.liquidity === "restricted") return { code: "low_liquidity", label: "流动性不足", detail: `liquidity=${asset.liquidity}，不进入 Public Default。` };
  return undefined;
}

export function allocationRows(result: ComputeFmResult | null, capital: number, baseCurrency: Currency = "CNY", mode: PortfolioMode = "raw"): readonly AllocationRow[] {
  if (!result) return [];
  const raw = result.weights.weights;
  const weights = ASSET_FIXTURES.map((_asset, index) => Math.max(0, Number.isFinite(raw[index] ?? 0) ? raw[index] ?? 0 : 0));
  const publicized = publicWeights(weights, ASSET_FIXTURES);
  const highVolTotal = publicized.reduce((sum, weight, index) => sum + (ASSET_FIXTURES[index]!.volatility === "high" ? weight : 0), 0);
  const roles: Readonly<Record<string, string>> = { GRID: "核心受益", CHIP: "次级受益", UTIL: "防守", GOLD: "对冲", WORLD: "分散", CASH: "流动性" };
  return ASSET_FIXTURES.map((asset, index) => {
    const weight = weights[index] ?? 0;
    const publicWeight = publicized[index] ?? 0;
    const fx = fxToBase(asset.quoteCurrency, baseCurrency);
    const rawDesired = capital * weight / (asset.price * fx);
    const publicDesired = capital * publicWeight / (asset.price * fx);
    const rawUnits = asset.fractionalSupported ? rawDesired : Math.floor(rawDesired / asset.lotSize) * asset.lotSize;
    const publicUnits = asset.fractionalSupported ? publicDesired : Math.floor(publicDesired / asset.lotSize) * asset.lotSize;
    const rawAmount = rawUnits * asset.price * fx;
    const publicAmount = publicUnits * asset.price * fx;
    const rawTarget = capital * weight;
    const publicTarget = capital * publicWeight;
    const rawResidualReason = rawUnits === 0 && rawTarget > 0 ? "Raw 金额过小，无法满足最小交易单位。" : rawAmount + 1e-9 < rawTarget ? `Raw lot rounding residual ${formatResidual(rawTarget - rawAmount, baseCurrency)}。` : undefined;
    const publicResidualReason = publicUnits === 0 && publicTarget > 0 ? "Public Default 金额过小，无法满足最小交易单位。" : publicAmount + 1e-9 < publicTarget ? `Public lot rounding residual ${formatResidual(publicTarget - publicAmount, baseCurrency)}。` : undefined;
    const constraintReasons: PublicConstraintReason[] = [];
    const eligibility = eligibilityReason(asset);
    if (eligibility) constraintReasons.push(eligibility);
    if (!asset.tradable && asset.proxyInstrument) constraintReasons.push({ code: "proxy_mapping", label: "代理映射", detail: `${asset.assetId} 非直接交易资产，使用 ${asset.proxyInstrument} 代理。` });
    if (asset.volatility === "high" && highVolTotal >= PUBLIC_CONSTRAINTS.highVolatilityClassMax - 1e-9 && publicWeight < weight - 1e-9) constraintReasons.push({ code: "high_volatility_cap", label: "高波动类别上限", detail: `高波动资产类别合计上限 ${PUBLIC_CONSTRAINTS.highVolatilityClassMax * 100}%。` });
    if (asset.assetClass !== "cash" && publicWeight < weight - 1e-9 && !constraintReasons.some((reason) => reason.code === "high_volatility_cap")) constraintReasons.push({ code: "single_security_cap", label: "单证券上限", detail: `单证券上限 ${PUBLIC_CONSTRAINTS.singleSecurityMax * 100}%。释放权重按可用资产原始权重比例确定性再分配。` });
    if (publicWeight > weight + 1e-9) constraintReasons.push({ code: "redistributed_weight", label: "释放权重再分配", detail: "其他不合资格或受限资产释放的权重按可用资产原始权重比例再分配。" });
    const selectedAmount = mode === "public" ? publicAmount : rawAmount;
    const selectedUnits = mode === "public" ? publicUnits : rawUnits;
    const row: AllocationRow = { id: asset.assetId, role: roles[asset.assetId] ?? asset.displayName, weight, publicWeight, amount: selectedAmount, units: selectedUnits, rawAmount, rawUnits, publicAmount, publicUnits, currency: baseCurrency, price: asset.price, quoteCurrency: asset.quoteCurrency, lotSize: asset.lotSize, constraintReasons, dataAsOf: asset.asOf, instrumentId: asset.instrumentId, tradable: asset.tradable, liquidity: asset.liquidity, volatility: asset.volatility };
    return { ...row, ...(rawResidualReason ? { rawResidualReason } : {}), ...(publicResidualReason ? { publicResidualReason } : {}), ...(mode === "raw" && rawResidualReason ? { residualReason: rawResidualReason } : {}), ...(mode === "public" && publicResidualReason ? { residualReason: publicResidualReason } : {}), ...(asset.proxyInstrument ? { proxyInstrument: asset.proxyInstrument } : {}) };
  });
}

function formatResidual(value: number, currency: Currency): string { return `${currency} ${value.toFixed(2)}`; }

export function branchByKind(branches: readonly BranchView[], kind: BranchView["kind"]): BranchView { return branches.find((branch) => branch.kind === kind) ?? branches[1]!; }
