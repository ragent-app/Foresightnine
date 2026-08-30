import { sha256Hex, type ComputeFmInput, type FactorExpression } from "@foresightnine/fm-engine";
import { DEFAULT_FRESHNESS_POLICY, evaluateFreshness, type FreshnessDecision, type FreshnessPolicy, type ProviderFreshnessStatus } from "@foresightnine/domain";
import { applyLatestCompletedSessionPrices, createMockMarketSnapshotBundle, marketDataStatus, marketSnapshotBundleToFmInput, type MarketSnapshotBundle } from "@foresightnine/market-data";
import type { AssetFixture, Branch, BranchKind, EvidenceBundle, FreshnessOptions, Language, ProbabilityInterval, RubricItem, TranscriptTurn } from "./types";

export const MARKET_DATA_AS_OF = "2026-08-09";
export const RUNTIME_AS_OF = "2026-08-09T00:00:00Z";
/** Runtime clock for the live app; tests should pass an explicit deterministic value. */
export const DEFAULT_RUNTIME_AS_OF = new Date().toISOString();
/** Offline fixtures never establish current-market freshness. */
export const PROVIDER_FRESHNESS_STATUS: ProviderFreshnessStatus = "unavailable";
export const FRESHNESS_POLICY: FreshnessPolicy = DEFAULT_FRESHNESS_POLICY;
export const WORLDLINE_BASELINE_DATE = "2026-08-10";
export const WORLDLINE_HORIZON_END = "2028-08-31";
export const FIXTURE_STATE = "historical_reference_and_simulated" as const;
export const FX_SNAPSHOT = { asOf: MARKET_DATA_AS_OF, cnyPerUnit: { CNY: 1, USD: 7.18, EUR: 7.82 } } as const;

export const ASSETS: readonly AssetFixture[] = [
  { id: "GRID", name: "电网设备 ETF", role: "观点受益 · 代理映射", instrument: "GRID-ETF", quoteCurrency: "USD", price: 120, lotSize: 1, fractional: false, tradable: false, proxy: "GRID-ETF", liquidity: "high", volatility: "low", assetClass: "equity", asOf: MARKET_DATA_AS_OF, freshness: "current" },
  { id: "CHIP", name: "全球半导体 ETF", role: "设备与芯片传导", instrument: "CHIP", quoteCurrency: "USD", price: 80, lotSize: 1, fractional: false, tradable: true, liquidity: "high", volatility: "high", assetClass: "equity", asOf: MARKET_DATA_AS_OF, freshness: "current" },
  { id: "UTIL", name: "公用事业 ETF", role: "防御与现金流", instrument: "UTIL", quoteCurrency: "CNY", price: 36, lotSize: 1, fractional: true, tradable: true, liquidity: "medium", volatility: "low", assetClass: "defensive", asOf: MARKET_DATA_AS_OF, freshness: "current" },
  { id: "GOLD", name: "黄金 ETF", role: "宏观对冲", instrument: "GOLD-ETF", quoteCurrency: "EUR", price: 12, lotSize: 1, fractional: false, tradable: true, liquidity: "medium", volatility: "high", assetClass: "commodity", asOf: MARKET_DATA_AS_OF, freshness: "current" },
  { id: "WORLD", name: "全球股票 ETF", role: "广泛分散", instrument: "WORLD", quoteCurrency: "USD", price: 90, lotSize: 1, fractional: true, tradable: true, liquidity: "low", volatility: "low", assetClass: "equity", asOf: MARKET_DATA_AS_OF, freshness: "stale" },
  { id: "CASH", name: "现金等价物", role: "舍入余量", instrument: "CASH", quoteCurrency: "CNY", price: 1, lotSize: 1, fractional: true, tradable: true, liquidity: "high", volatility: "low", assetClass: "cash", asOf: MARKET_DATA_AS_OF, freshness: "current" },
];

export function freshnessFor(asset: AssetFixture, options: FreshnessOptions = { runtimeAsOf: RUNTIME_AS_OF, providerStatus: PROVIDER_FRESHNESS_STATUS }): FreshnessDecision {
  return evaluateFreshness({ dataAsOf: asset.asOf, runtimeAsOf: options.runtimeAsOf, providerStatus: options.providerStatus, declaredFreshness: asset.freshness }, options.policy ?? FRESHNESS_POLICY);
}

export const RUBRIC: readonly RubricItem[] = [
  { id: "premise", label: "前提清晰度", score: 4, critical: true, rationale: "观点包含方向与受益对象。" },
  { id: "time", label: "时间边界", score: 3, critical: true, rationale: "两年窗口可用于后续验证。" },
  { id: "trigger", label: "触发事件可观察性", score: 3, critical: false, rationale: "订单、指引和现金流可作为观察锚点。" },
  { id: "causal", label: "因果链完整性", score: 3, critical: true, rationale: "需求 → 投资 → 订单 → 现金流，仍需反例。" },
  { id: "counterfactual", label: "反事实与替代解释", score: 2, critical: false, rationale: "效率提升可能抵消部分电力需求。" },
  { id: "evidence", label: "证据质量", score: 3, critical: false, rationale: "客户端示例证据为 B/C/D/E 混合等级。" },
  { id: "transmission", label: "资产传导合理性", score: 3, critical: true, rationale: "代理工具与经济暴露已分开标注。" },
  { id: "priced", label: "市场已定价程度", score: 2, critical: false, rationale: "部分投资预期可能已经进入估值。" },
  { id: "falsifiable", label: "可证伪性", score: 3, critical: true, rationale: "连续下修订单或指引会使路径失效。" },
  { id: "confirmation", label: "用户确认程度", score: 0, critical: true, rationale: "需要用户确认，或带保留意见确认。" },
];

const RUBRIC_EN: readonly RubricItem[] = [
  { id: "premise", label: "Premise clarity", score: 4, critical: true, rationale: "The thesis identifies a direction and beneficiary." },
  { id: "time", label: "Time boundary", score: 3, critical: true, rationale: "The two-year window can be tested later." },
  { id: "trigger", label: "Observable trigger", score: 3, critical: false, rationale: "Orders, guidance and cash flow are observable anchors." },
  { id: "causal", label: "Causal chain", score: 3, critical: true, rationale: "Demand → investment → orders → cash flow; counterexamples remain." },
  { id: "counterfactual", label: "Counterfactuals", score: 2, critical: false, rationale: "Efficiency gains could offset some power demand." },
  { id: "evidence", label: "Evidence quality", score: 3, critical: false, rationale: "The client fixture mixes B/C/D/E grades." },
  { id: "transmission", label: "Asset transmission", score: 3, critical: true, rationale: "Proxy instruments and economic exposure are labelled separately." },
  { id: "priced", label: "Priced-in risk", score: 2, critical: false, rationale: "Some investment expectations may already be in valuations." },
  { id: "falsifiable", label: "Falsifiability", score: 3, critical: true, rationale: "Repeated downward revisions to orders or guidance invalidate the path." },
  { id: "confirmation", label: "User confirmation", score: 0, critical: true, rationale: "The user must confirm, with or without reservations." },
];

export function rubricFor(language: Language): readonly RubricItem[] { return language === "zh" ? RUBRIC : RUBRIC_EN; }

export const MOCK_TURNS: readonly TranscriptTurn[] = [
  { id: "system-1", author: "system", body: "Mock provider · 本地确定性挑战，不代表真实模型输出。" },
  { id: "challenger-1", author: "challenger", body: "最强反例：数据中心能效提升、需求延迟或资本开支已被市场定价，可能削弱电网设备的传导。请补充可观察证据，并说明什么条件会让结论失效。" },
];

const MOCK_TURNS_EN: readonly TranscriptTurn[] = [
  { id: "system-1", author: "system", body: "Mock provider · deterministic local challenge; not real model output." },
  { id: "challenger-1", author: "challenger", body: "Strongest counterexample: data-center efficiency, delayed demand, or capital expenditure already priced in could weaken transmission to grid equipment. Add observable evidence and state what would invalidate the conclusion." },
];

export function mockTurnsFor(language: Language): readonly TranscriptTurn[] { return language === "zh" ? MOCK_TURNS : MOCK_TURNS_EN; }

export const MOCK_EVIDENCE: readonly EvidenceBundle[] = [
  { evidence_id: "fixture-b", challenge_id: "mobile-challenge", source_type: "reliable_secondary", url: "https://www.iea.org/", title: "IEA 数据中心电力需求与电网投资摘要", publisher: "IEA · deterministic offline fixture", published_at: "2024-10-01", retrieved_at: "2026-08-10T00:00:00Z", excerpt: "公开摘要用于验证需求增长与电网投资的方向；具体数字仍需回到原始报告核验。", user_note: "客户端 illustrative mock；没有远程抓取。", content_hash: "fixture-evidence-b", grade: "B", supports_or_challenges: "supports", independently_reviewable: true },
  { evidence_id: "fixture-d", challenge_id: "mobile-challenge", source_type: "user_private", url: "", title: "行业专家访谈摘要", publisher: "用户私有声明 · fixture", published_at: "2026-08-09", retrieved_at: "2026-08-10T00:00:00Z", excerpt: "专家认为订单可见度改善，但访谈不能替代公开证据。", user_note: "D 级：不可独立复核，不作为公开事实。", content_hash: "fixture-evidence-d", grade: "D", supports_or_challenges: "context", independently_reviewable: false },
  { evidence_id: "fixture-e", challenge_id: "mobile-challenge", source_type: "model_inference", url: "", title: "电网设备订单弹性", publisher: "本地模型推断 · fixture", published_at: null, retrieved_at: "2026-08-10T00:00:00Z", excerpt: "模型推断订单可能对需求增长更敏感。", user_note: "E 级：不可作为公开事实。", content_hash: "fixture-evidence-e", grade: "E", supports_or_challenges: "context", independently_reviewable: false },
];

const MOCK_EVIDENCE_EN: readonly EvidenceBundle[] = [
  { evidence_id: "fixture-b", challenge_id: "mobile-challenge", source_type: "reliable_secondary", url: "https://www.iea.org/", title: "IEA data-center electricity demand and grid-investment summary", publisher: "IEA · deterministic offline fixture", published_at: "2024-10-01", retrieved_at: "2026-08-10T00:00:00Z", excerpt: "The public summary checks the direction of demand growth and grid investment; specific figures still require the original report.", user_note: "Client illustrative mock; no remote fetch.", content_hash: "fixture-evidence-b", grade: "B", supports_or_challenges: "supports", independently_reviewable: true },
  { evidence_id: "fixture-d", challenge_id: "mobile-challenge", source_type: "user_private", url: "", title: "Industry expert interview summary", publisher: "User-private claim · fixture", published_at: "2026-08-09", retrieved_at: "2026-08-10T00:00:00Z", excerpt: "The expert expects order visibility to improve, but the interview cannot replace public evidence.", user_note: "Grade D: not independently reviewable and not a public fact.", content_hash: "fixture-evidence-d", grade: "D", supports_or_challenges: "context", independently_reviewable: false },
  { evidence_id: "fixture-e", challenge_id: "mobile-challenge", source_type: "model_inference", url: "", title: "Grid-equipment order elasticity", publisher: "Local model inference · fixture", published_at: null, retrieved_at: "2026-08-10T00:00:00Z", excerpt: "The model infers that orders may be more sensitive to demand growth.", user_note: "Grade E: cannot become a public fact.", content_hash: "fixture-evidence-e", grade: "E", supports_or_challenges: "context", independently_reviewable: false },
];

export function mockEvidenceFor(language: Language): readonly EvidenceBundle[] { return language === "zh" ? MOCK_EVIDENCE : MOCK_EVIDENCE_EN; }

function range(low: number, high: number): ProbabilityInterval { return { low, high }; }

const PREMISE = { point: 0.6, range: range(0.55, 0.65) } as const;
const CONDITIONAL_POLICY = {
  weak: { point: 0.2, range: range(0.15, 0.25) },
  main: { point: 0.55, range: range(0.5, 0.6) },
  strong: { point: 0.25, range: range(0.2, 0.3) },
} as const satisfies Readonly<Record<BranchKind, { readonly point: number; readonly range: ProbabilityInterval }>>;

function productRange(left: ProbabilityInterval, right: ProbabilityInterval): ProbabilityInterval {
  return range(left.low * right.low, left.high * right.high);
}

/**
 * Conditional ranges are marginal bounds of one constrained probability
 * simplex.  They are not independent intervals: the displayed point policy
 * (weak/main/strong = 20%/55%/25%) sums to exactly one and each point must
 * remain within its public range.
 */
export function validateProbabilityLedger(branches: readonly Branch[]): boolean {
  if (branches.length !== 3) return false;
  const epsilon = 1e-12;
  const [first] = branches;
  if (!first || first.premise.low < 0 || first.premise.high > 1 || first.premise.low > first.premise.high || first.premisePoint < first.premise.low || first.premisePoint > first.premise.high) return false;
  const conditionalPoint = branches.reduce((sum, branch) => sum + branch.conditionalPoint, 0);
  const conditionalLower = branches.reduce((sum, branch) => sum + branch.conditional.low, 0);
  const conditionalUpper = branches.reduce((sum, branch) => sum + branch.conditional.high, 0);
  if (Math.abs(conditionalPoint - 1) > epsilon || conditionalLower > 1 + epsilon || conditionalUpper < 1 - epsilon) return false;
  return branches.every((branch) => {
    const expected = productRange(branch.premise, branch.conditional);
    return branch.premise.low === first.premise.low && branch.premise.high === first.premise.high
      && branch.premisePoint === first.premisePoint
      && branch.conditional.low >= 0 && branch.conditional.high <= 1 && branch.conditional.low <= branch.conditional.high
      && branch.conditionalPoint >= branch.conditional.low && branch.conditionalPoint <= branch.conditional.high
      && branch.joint.low === expected.low && branch.joint.high === expected.high
      && Math.abs(branch.jointPoint - branch.premisePoint * branch.conditionalPoint) <= epsilon;
  });
}

/** Protects fixture timelines from accidentally using a future datum. */
export function validateBranchTimeline(branch: Branch): boolean {
  if (branch.dataAsOf > branch.baselineDate || branch.baselineDate > branch.horizonEnd || branch.fixtureState !== FIXTURE_STATE || branch.sourceRefs.length === 0) return false;
  let previousOccurrence = branch.baselineDate;
  return branch.nodes.every((node) => {
    const valid = node.dataAsOf === branch.dataAsOf && node.occurrenceDate >= previousOccurrence && node.occurrenceDate <= node.revealDate
      && node.revealDate <= branch.horizonEnd && node.observableIndicators.length > 0 && node.assetAnchors.length > 0 && node.sourceRefs.length > 0;
    previousOccurrence = node.occurrenceDate;
    return valid;
  });
}

export function makeBranches(viewpoint: string, language: Language = "zh"): readonly Branch[] {
  const suffix = sha256Hex(viewpoint.trim() || "empty-thesis").slice(0, 8);
  const en = language === "en";
  const common = [
    { id: `${suffix}-0`, time: en ? "Baseline" : "基准日", event: en ? "Premise recorded" : "前提被记录", occurrenceDate: WORLDLINE_BASELINE_DATE, revealDate: WORLDLINE_BASELINE_DATE, dataAsOf: MARKET_DATA_AS_OF, observableIndicators: [en ? "Recorded premise" : "已记录前提"], causalParents: [], assetAnchors: ["GRID", "CHIP"], sourceRefs: ["fixture-b", "scenario-rule-v1"], stateChange: en ? "Research baseline frozen" : "研究基准冻结" },
    { id: `${suffix}-1`, time: en ? "+6 months" : "+6 个月", event: en ? "Demand and investment guidance" : "需求与投资指引", occurrenceDate: "2027-02-10", revealDate: "2027-02-28", dataAsOf: MARKET_DATA_AS_OF, observableIndicators: en ? ["Demand guidance", "Grid capex guidance"] : ["需求指引", "电网资本开支指引"], causalParents: [`${suffix}-0`], assetAnchors: ["GRID", "CHIP", "UTIL"], sourceRefs: ["fixture-b", "scenario-rule-v1"], stateChange: en ? "Transmission evidence updates" : "传导证据更新" },
    { id: `${suffix}-2`, time: en ? "+12 months" : "+12 个月", event: en ? "Capital expenditure and order check" : "资本开支与订单验证", occurrenceDate: "2027-08-10", revealDate: "2027-08-31", dataAsOf: MARKET_DATA_AS_OF, observableIndicators: en ? ["Orders", "Operating cash flow"] : ["订单", "经营现金流"], causalParents: [`${suffix}-1`], assetAnchors: ["GRID", "CHIP", "UTIL"], sourceRefs: ["fixture-b", "scenario-rule-v1"], stateChange: en ? "Path confirmation or invalidation" : "路径确认或证伪", invalidation: en ? "Demand growth falls below the baseline" : "需求增速低于基准率" },
    { id: `${suffix}-3`, time: en ? "+24 months" : "+24 个月", event: en ? "Valuation reprices or the path fails" : "估值再定价或路径失效", occurrenceDate: "2028-08-10", revealDate: WORLDLINE_HORIZON_END, dataAsOf: MARKET_DATA_AS_OF, observableIndicators: en ? ["Relative valuation", "Order backlog"] : ["相对估值", "订单积压"], causalParents: [`${suffix}-2`], assetAnchors: ["GRID", "CHIP", "GOLD"], sourceRefs: ["fixture-b", "scenario-rule-v1"], stateChange: en ? "Scenario resolves" : "情景收敛" },
  ];
  const build = (kind: BranchKind, title: string, description: string, reveal: string): Branch => {
    const probability = CONDITIONAL_POLICY[kind];
    return {
      kind, title, baselineDate: WORLDLINE_BASELINE_DATE, horizonEnd: WORLDLINE_HORIZON_END, dataAsOf: MARKET_DATA_AS_OF, fixtureState: FIXTURE_STATE,
      premise: PREMISE.range, premisePoint: PREMISE.point, conditional: probability.range, conditionalPoint: probability.point,
      joint: productRange(PREMISE.range, probability.range), jointPoint: PREMISE.point * probability.point, description, reveal, assetAnchors: ["GRID", "CHIP", "UTIL", "GOLD"], sourceRefs: ["fixture-b", "scenario-rule-v1"],
      nodes: common.map((node, index) => ({ ...node, id: `${node.id}-${kind}`, event: index === 1 && kind === "weak" ? (en ? "Demand and investment delayed" : "需求与投资延迟") : index === 1 && kind === "strong" ? (en ? "Policy and financing accelerate" : "政策与融资加速") : node.event })),
    };
  };
  return [
    build("weak", en ? "Weak W−" : "弱 W−", en ? `Transmission from “${viewpoint.trim() || "your thesis"}” is below expectations.` : `「${viewpoint.trim() || "你的观点"}」的传导低于预期。`, en ? "Orders and guidance do not expand" : "订单与指引没有展开"),
    build("main", en ? "Main W₀" : "主 W₀", en ? `“${viewpoint.trim() || "Your thesis"}” partly plays out and investment is gradually verified.` : `「${viewpoint.trim() || "你的观点"}」部分兑现，投资逐步验证。`, en ? "Order growth and cash flow become visible" : "订单增速与现金流可见"),
    build("strong", en ? "Strong W+" : "强 W+", en ? `“${viewpoint.trim() || "Your thesis"}” triggers second-order effects.` : `「${viewpoint.trim() || "你的观点"}」触发二阶效应。`, en ? "Orders and policy both beat expectations" : "订单与政策同时超预期"),
  ];
}

const FM_FACTORS: readonly FactorExpression[] = [
  { id: 10, icMean: 0.072, icir: 0.64, admitted: true, expression: { op: "field", name: "momentum" } },
  { id: 20, icMean: 0.051, icir: 0.58, admitted: true, expression: { op: "field", name: "quality" } },
  { id: 30, icMean: -0.044, icir: -0.52, admitted: true, expression: { op: "rank", input: { op: "field", name: "momentum" } } },
];

/** Explicit offline fallback bundle. A future configured provider must replace this at acquisition time. */
export const MARKET_SNAPSHOT_BUNDLE: MarketSnapshotBundle = createMockMarketSnapshotBundle({ assetIds: ASSETS.map((asset) => asset.id), cutoff: MARKET_DATA_AS_OF, timezone: "UTC", requestedAt: "2026-08-09T00:00:00Z" });
export const MARKET_DATA_STATUS = marketDataStatus(MARKET_SNAPSHOT_BUNDLE);

export function assetsForMarketBundle(bundle: MarketSnapshotBundle): readonly AssetFixture[] {
  return applyLatestCompletedSessionPrices(ASSETS, bundle);
}

export function fmInput(bundle: MarketSnapshotBundle = MARKET_SNAPSHOT_BUNDLE): ComputeFmInput {
  const converted = marketSnapshotBundleToFmInput(bundle, FM_FACTORS, { currentValues: Float64Array.from([280, 200, 180, 140, 120, 80]), maxFactors: 10, initialAllocation: true, costBps: 3 });
  if (converted.status !== "ready") throw new Error("The deterministic market snapshot bundle is limited and cannot drive the FM fixture.");
  return converted.input;
}
