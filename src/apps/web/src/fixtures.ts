import type { EvidenceItem, ProbabilityAssessment, ThesisDocument, Worldline } from "@foresightnine/contracts";
import { sha256Hex, type ComputeFmInput, type FactorExpression } from "@foresightnine/fm-engine";
import type { AssetFixture, BranchView, Currency, EvidenceView } from "./types";

export const MARKET_DATA_AS_OF = "2026-08-09";
export const FX_SNAPSHOT = {
  asOf: MARKET_DATA_AS_OF,
  provider: "fixture-fx-snapshot",
  label: "deterministic mock FX snapshot; not live market data",
  cnyPerUnit: { CNY: 1, USD: 7.18, EUR: 7.82 } as const,
} as const;

export const PUBLIC_CONSTRAINTS = {
  methodVersion: "Public-Default-v1",
  singleSecurityMax: 0.25,
  highVolatilityClassMax: 0.35,
  longOnly: true,
  fullyInvested: true,
  noLeverage: true,
  staleDataAllowed: false,
  lowLiquidityAllowed: false,
} as const;

const metadata = { schema_version: "v1" as const, created_at: "2026-08-10T00:00:00Z", source_refs: [{ ref_id: "fixture:web", source_type: "model_inference" as const, label: "Web offline fixture" }], content_hash: "fixture-web" };

export const ASSET_FIXTURES: readonly AssetFixture[] = [
  { assetId: "GRID", displayName: "电网基础设施", economicExposureId: "grid-infrastructure", instrumentId: "GRID-ETF", assetClass: "etf", quoteCurrency: "USD", price: 120, lotSize: 1, fractionalSupported: false, tradable: false, proxyInstrument: "GRID-ETF", liquidity: "high", volatility: "low", asOf: MARKET_DATA_AS_OF, freshness: "current" },
  { assetId: "CHIP", displayName: "半导体基础设施", economicExposureId: "semiconductor-infrastructure", instrumentId: "CHIP", assetClass: "equity", quoteCurrency: "USD", price: 80, lotSize: 1, fractionalSupported: false, tradable: true, liquidity: "high", volatility: "high", asOf: MARKET_DATA_AS_OF, freshness: "current" },
  { assetId: "UTIL", displayName: "公用事业", economicExposureId: "utilities", instrumentId: "UTIL", assetClass: "etf", quoteCurrency: "CNY", price: 36, lotSize: 1, fractionalSupported: true, tradable: true, liquidity: "medium", volatility: "low", asOf: MARKET_DATA_AS_OF, freshness: "current" },
  { assetId: "GOLD", displayName: "黄金对冲", economicExposureId: "gold", instrumentId: "GOLD-ETF", assetClass: "commodity_etf", quoteCurrency: "EUR", price: 12, lotSize: 1, fractionalSupported: false, tradable: true, liquidity: "medium", volatility: "high", asOf: MARKET_DATA_AS_OF, freshness: "current" },
  { assetId: "WORLD", displayName: "全球股票", economicExposureId: "global-equity", instrumentId: "WORLD", assetClass: "etf", quoteCurrency: "USD", price: 90, lotSize: 1, fractionalSupported: true, tradable: true, liquidity: "low", volatility: "low", asOf: MARKET_DATA_AS_OF, freshness: "historical_only" },
  { assetId: "CASH", displayName: "现金等价物", economicExposureId: "cash", instrumentId: "CASH", assetClass: "cash", quoteCurrency: "CNY", price: 1, lotSize: 1, fractionalSupported: true, tradable: true, liquidity: "high", volatility: "low", asOf: MARKET_DATA_AS_OF, freshness: "current" },
];

export const demoThesis = (viewpoint: string, capital: number, currency: Currency): ThesisDocument => ({
  ...metadata,
  thesis_id: "thesis-web-demo",
  session_id: "session-web-demo",
  raw_text: viewpoint,
  normalized_premise: viewpoint,
  horizon: "24 months",
  trigger_events: ["用户观点中的可观察触发事件", "需求或政策披露", "订单与现金流验证"],
  claimed_asset_impacts: ["电网基础设施", "半导体基础设施", "公用事业"],
  capital,
  base_currency: currency,
  user_private_evidence_claims: false,
  status: "DRAFT",
});

export function evidenceHash(item: Pick<EvidenceView, "evidence_id" | "challenge_id" | "source_type" | "url" | "title" | "publisher" | "published_at" | "retrieved_at" | "excerpt" | "user_note" | "grade" | "supports_or_challenges" | "independently_reviewable" | "search_provider" | "search_result_hash">): string {
  return sha256Hex(JSON.stringify({ evidence_id: item.evidence_id, challenge_id: item.challenge_id, source_type: item.source_type, url: item.url, title: item.title, publisher: item.publisher, published_at: item.published_at, retrieved_at: item.retrieved_at, excerpt: item.excerpt, user_note: item.user_note, grade: item.grade, supports_or_challenges: item.supports_or_challenges, independently_reviewable: item.independently_reviewable, search_provider: item.search_provider ?? null, search_result_hash: item.search_result_hash ?? null }));
}

export interface SourceMaterialHashInput {
  readonly url: string | null;
  readonly title: string;
  readonly published_at: string | null;
  readonly retrieved_at: string;
  readonly excerpt: string;
  readonly user_note: string;
}

function normalizeApiDateTime(value: string | null): string | null {
  const trimmed = value?.trim() ?? "";
  if (!trimmed) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(trimmed)) return `${trimmed}T00:00:00`;
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$/i.exec(trimmed);
  if (!match) return trimmed;
  const fraction = match[3] ? `.${match[3].slice(0, 6).padEnd(6, "0")}` : "";
  const timezone = match[4] ? (match[4].toUpperCase() === "Z" ? "+00:00" : match[4]) : "";
  return `${match[1]}T${match[2]}${fraction}${timezone}`;
}

/** API-compatible subset hash; distinct from the full Web audit hash above. */
export function sourceMaterialHash(item: SourceMaterialHashInput): string {
  return sha256Hex(JSON.stringify({
    excerpt: item.excerpt,
    published_at: normalizeApiDateTime(item.published_at),
    retrieved_at: normalizeApiDateTime(item.retrieved_at),
    title: item.title,
    url: item.url?.trim() || null,
    user_note: item.user_note,
  }));
}

export function isEvidenceIndependentlyReviewable(grade: EvidenceView["grade"], sourceType: EvidenceView["source_type"], url: string): boolean {
  return (grade === "A" || grade === "B" || grade === "C") && sourceType !== "user_private" && sourceType !== "model_inference" && Boolean(url.trim());
}

function makeEvidence(item: Omit<EvidenceView, "sourceLabel" | "publisher" | "content_hash" | "retrieved_at" | "challenge_id"> & { sourceLabel: string; challenge_id?: string; retrieved_at?: string }): EvidenceView {
  const retrievedAt = item.retrieved_at ?? `${MARKET_DATA_AS_OF}T00:00:00Z`;
  const base = {
    ...metadata,
    evidence_id: item.evidence_id,
    challenge_id: item.challenge_id ?? "challenge-web-demo",
    source_type: item.source_type,
    url: item.url,
    title: item.title,
    publisher: item.sourceLabel,
    published_at: item.published_at,
    retrieved_at: retrievedAt,
    excerpt: item.excerpt,
    user_note: item.user_note,
    grade: item.grade,
    supports_or_challenges: item.supports_or_challenges,
    independently_reviewable: isEvidenceIndependentlyReviewable(item.grade, item.source_type, item.url),
    ...(item.search_provider && item.search_result_hash ? { search_provider: item.search_provider, search_result_hash: item.search_result_hash } : {}),
    sourceLabel: item.sourceLabel,
  };
  return { ...base, content_hash: evidenceHash(base), fixture: true };
}

export const searchEvidenceFixture: EvidenceView = makeEvidence({
  evidence_id: "evidence-search-1", title: "IEA 数据中心电力需求与电网投资摘要", source_type: "reliable_secondary", grade: "B", sourceLabel: "IEA · deterministic offline fixture", published_at: "2024-10-01", excerpt: "公开摘要用于验证需求增长与电网投资的方向；具体数字仍需回到原始报告核验。", user_note: "fixture: 仅用于离线验证 SearchEvidenceBundle 字段闭环。", supports_or_challenges: "supports", independently_reviewable: true, url: "https://www.iea.org/", fixture: true,
});

export const privateEvidenceFixture: EvidenceView = makeEvidence({
  evidence_id: "evidence-private-1", title: "行业专家访谈摘要", source_type: "user_private", grade: "D", sourceLabel: "用户私有声明 · fixture", published_at: "2026-08-09", excerpt: "专家认为订单可见度改善，但访谈本身不能替代公开证据。", user_note: "不可独立复核；保留为用户私有前提，不作为公开事实。", supports_or_challenges: "context", independently_reviewable: false, url: "", fixture: true,
});

function assessment(id: string, worldlineId: string, conditional: number, minimum: number, maximum: number): ProbabilityAssessment {
  return { ...metadata, assessment_id: id, worldline_id: worldlineId, premise_probability: 0.42, premise_probability_range: { minimum: 0.35, maximum: 0.49 }, conditional_probability: conditional, conditional_probability_range: { minimum, maximum }, joint_probability: 0.42 * conditional, joint_probability_range: { minimum: 0.35 * minimum, maximum: 0.49 * maximum }, confidence: "medium", base_rate_refs: [], adjustment_reasons: ["支持证据向上", "反对证据向下", "模型不确定性：区间加宽"], calibration_status: "model_estimate_unvalidated" };
}

function worldline(kind: BranchView["kind"], title: string, description: string, premise: string, suffix: string): Worldline {
  const id = `wl-${kind}-web-${suffix}`;
  const nodes: Worldline["nodes"] = [
    { node_id: `${id}-0`, time_window: "现在", event_date: "2026-08-10", data_as_of: MARKET_DATA_AS_OF, event: "观点基线与需求信号", observable_indicators: ["用户观点", "公开需求披露"], causal_parents: [], state_changes: ["观察窗口开始"], confidence: "medium" },
    { node_id: `${id}-1`, time_window: "+6 个月", event_date: "2027-02-10", data_as_of: MARKET_DATA_AS_OF, event: kind === "weak" ? "需求与投资指引走弱" : kind === "strong" ? "政策与融资提前加速" : "需求与投资指引确认", observable_indicators: ["投资计划", "设备订单"], causal_parents: [`${id}-0`], state_changes: [kind === "weak" ? "订单斜率下修" : "订单斜率改变"], confidence: kind === "main" ? "high" : "medium" },
    { node_id: `${id}-2`, time_window: "+12 个月", event_date: "2027-08-10", data_as_of: MARKET_DATA_AS_OF, event: "传导结果可见", observable_indicators: ["订单交付", "利用率", "现金流"], causal_parents: [`${id}-1`], state_changes: ["现金流验证"], confidence: "medium" },
    { node_id: `${id}-3`, time_window: "+24 个月", event_date: "2028-08-10", data_as_of: MARKET_DATA_AS_OF, event: "估值再定价或失效", observable_indicators: ["利润兑现", "需求修正"], causal_parents: [`${id}-2`], state_changes: ["路径结算"], confidence: "low" },
  ];
  const revealDates = ["2026-08-10", "2027-02-28", "2027-08-31", "2028-08-31"];
  return { ...metadata, worldline_id: id, bundle_id: `bundle-web-${suffix}`, branch_kind: kind, title, premise, baseline_date: "2026-08-10", horizon_end: "2028-08-31", nodes, edges: [{ from_node_id: `${id}-0`, to_node_id: `${id}-1` }, { from_node_id: `${id}-1`, to_node_id: `${id}-2` }, { from_node_id: `${id}-2`, to_node_id: `${id}-3` }], asset_anchors: ["GRID", "CHIP", "UTIL"], asset_impacts: [], event_reveal_schedule: nodes.map((node, index) => ({ event_id: node.node_id, event_date: node.event_date, reveal_date: revealDates[index]!, rule: "scheduled" as const })), invalidation_conditions: [kind === "weak" ? "需求与投资没有恢复" : "需求增长低于用户观点中的基线", kind === "strong" ? "融资收紧或政策约束" : "资本开支指引连续下修"], provenance: "offline deterministic fixture; consumes user viewpoint; model estimate", status: "WORLDLINE_READY" };
}

export function makeBranches(viewpoint: string): readonly BranchView[] {
  const premise = viewpoint.trim() || "尚未提供观点；请先返回输入页。";
  const suffix = sha256Hex(premise).slice(0, 8);
  return [
    { kind: "weak", title: "弱情景 W−", range: "20–35%", color: "weak", description: `「${premise}」中的传导低于预期，订单或需求延后。`, reveal: "需求与订单没有按观点展开", invalidation: "需求下修、投资周期延迟", nodes: ["传导走弱", "资本开支延后", "估值压缩"], assessment: assessment(`assessment-weak-${suffix}`, `wl-weak-web-${suffix}`, 0.27, 0.20, 0.35), worldline: worldline("weak", "需求与投资延迟", `「${premise}」未能形成足够传导。`, premise, suffix) },
    { kind: "main", title: "主情景 W₀", range: "45–62%", color: "main", description: `「${premise}」按部分路径兑现，设备与基础设施投资逐步验证。`, reveal: "订单增速与现金流可见", invalidation: "需求增速持续低于观点基线", nodes: ["观点触发", "投资指引", "订单增速", "估值再定价"], assessment: assessment(`assessment-main-${suffix}`, `wl-main-web-${suffix}`, 0.54, 0.45, 0.62), worldline: worldline("main", "投资按计划兑现", `「${premise}」按计划传导。`, premise, suffix) },
    { kind: "strong", title: "强情景 W+", range: "12–25%", color: "strong", description: `「${premise}」触发二阶效应，政策与融资共同加速扩容。`, reveal: "订单和政策信号同时超预期", invalidation: "融资收紧、政策约束", nodes: ["需求加速", "资本开支提前", "订单超预期", "估值扩张"], assessment: assessment(`assessment-strong-${suffix}`, `wl-strong-web-${suffix}`, 0.19, 0.12, 0.25), worldline: worldline("strong", "政策与融资共振", `「${premise}」产生二阶效应。`, premise, suffix) },
  ];
}

export const branches: readonly BranchView[] = makeBranches("如果未来两年全球数据中心电力需求持续上升，并推动电网设备投资提速，那么电网设备与相关基础设施将受益。");

export function isPublicEligible(asset: AssetFixture): boolean {
  const asOf = Date.parse(asset.asOf);
  const snapshot = Date.parse(MARKET_DATA_AS_OF);
  return (asset.tradable || Boolean(asset.proxyInstrument)) && (asset.liquidity === "high" || asset.liquidity === "medium") && asset.freshness === "current" && Number.isFinite(asOf) && asOf <= snapshot;
}

export function fxToBase(quoteCurrency: Currency, baseCurrency: Currency): number {
  return FX_SNAPSHOT.cnyPerUnit[quoteCurrency] / FX_SNAPSHOT.cnyPerUnit[baseCurrency];
}

export function fmInput(): ComputeFmInput {
  const dates = ["2026-08-05", "2026-08-06", "2026-08-07", "2026-08-08", "2026-08-09"];
  const close = [[100, 80, 40, 12, 90, 1], [101, 81, 40.2, 12.1, 89, 1], [102, 82, 40.1, 12.2, 91, 1], [103, 83, 40.4, 12.1, 92, 1], [104, 84, 40.7, 12.3, 94, 1]];
  const observations = dates.map((asOf, index) => ({ asOf, fields: { close: close[index]!, momentum: [1 + index * 0.1, 2 + index * 0.05, 3 - index * 0.04, 4 + index * 0.02, 5 + index * 0.08, 2 + index * 0.03], quality: [5 - index * 0.1, 4 + index * 0.06, 2 + index * 0.1, 3 + index * 0.04, 1 + index * 0.08, 4 + index * 0.02] } }));
  const factors: readonly FactorExpression[] = [
    { id: 10, icMean: 0.072, icir: 0.64, admitted: true, expression: { op: "field", name: "momentum" } },
    { id: 20, icMean: 0.051, icir: 0.58, admitted: true, expression: { op: "field", name: "quality" } },
    { id: 30, icMean: -0.044, icir: -0.52, admitted: true, expression: { op: "rank", input: { op: "field", name: "momentum" } } },
  ];
  return { cutoff: "2026-08-09", observations, factors, maxFactors: 10, forecastHorizon: 1, forwardReturns: dates.map((asOf, index) => ({ asOf, values: close[index]!.map((value, asset) => (value / close[Math.max(0, index - 1)]![asset]! - 1) || 0) })), currentValues: Float64Array.from([280, 200, 180, 140, 120, 80]), initialAllocation: true, costBps: 3 };
}
