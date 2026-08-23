import { sha256Hex, type ComputeFmInput, type FactorExpression } from "@foresightnine/fm-engine";
import type { AssetFixture, Branch, BranchKind, EvidenceBundle, RubricItem, TranscriptTurn } from "./types";

export const MARKET_DATA_AS_OF = "2026-08-09";
export const FX_SNAPSHOT = { asOf: MARKET_DATA_AS_OF, cnyPerUnit: { CNY: 1, USD: 7.18, EUR: 7.82 } } as const;

export const ASSETS: readonly AssetFixture[] = [
  { id: "GRID", name: "电网设备 ETF", role: "观点受益 · 代理映射", instrument: "GRID-ETF", quoteCurrency: "USD", price: 120, lotSize: 1, fractional: false, tradable: false, proxy: "GRID-ETF", liquidity: "high", volatility: "low", asOf: MARKET_DATA_AS_OF, freshness: "current" },
  { id: "CHIP", name: "全球半导体 ETF", role: "设备与芯片传导", instrument: "CHIP", quoteCurrency: "USD", price: 80, lotSize: 1, fractional: false, tradable: true, liquidity: "high", volatility: "high", asOf: MARKET_DATA_AS_OF, freshness: "current" },
  { id: "UTIL", name: "公用事业 ETF", role: "防御与现金流", instrument: "UTIL", quoteCurrency: "CNY", price: 36, lotSize: 1, fractional: true, tradable: true, liquidity: "medium", volatility: "low", asOf: MARKET_DATA_AS_OF, freshness: "current" },
  { id: "GOLD", name: "黄金 ETF", role: "宏观对冲", instrument: "GOLD-ETF", quoteCurrency: "EUR", price: 12, lotSize: 1, fractional: false, tradable: true, liquidity: "medium", volatility: "high", asOf: MARKET_DATA_AS_OF, freshness: "current" },
  { id: "WORLD", name: "全球股票 ETF", role: "广泛分散", instrument: "WORLD", quoteCurrency: "USD", price: 90, lotSize: 1, fractional: true, tradable: true, liquidity: "low", volatility: "low", asOf: MARKET_DATA_AS_OF, freshness: "stale" },
  { id: "CASH", name: "现金等价物", role: "舍入余量", instrument: "CASH", quoteCurrency: "CNY", price: 1, lotSize: 1, fractional: true, tradable: true, liquidity: "high", volatility: "low", asOf: MARKET_DATA_AS_OF, freshness: "current" },
];

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

export const MOCK_TURNS: readonly TranscriptTurn[] = [
  { id: "system-1", author: "system", body: "Mock provider · 本地确定性挑战，不代表真实模型输出。" },
  { id: "challenger-1", author: "challenger", body: "最强反例：数据中心能效提升、需求延迟或资本开支已被市场定价，可能削弱电网设备的传导。请补充可观察证据，并说明什么条件会让结论失效。" },
];

export const MOCK_EVIDENCE: readonly EvidenceBundle[] = [
  { evidence_id: "fixture-b", challenge_id: "mobile-challenge", source_type: "reliable_secondary", url: "https://www.iea.org/", title: "IEA 数据中心电力需求与电网投资摘要", publisher: "IEA · deterministic offline fixture", published_at: "2024-10-01", retrieved_at: "2026-08-10T00:00:00Z", excerpt: "公开摘要用于验证需求增长与电网投资的方向；具体数字仍需回到原始报告核验。", user_note: "客户端 illustrative mock；没有远程抓取。", content_hash: "fixture-evidence-b", grade: "B", supports_or_challenges: "supports", independently_reviewable: true },
  { evidence_id: "fixture-d", challenge_id: "mobile-challenge", source_type: "user_private", url: "", title: "行业专家访谈摘要", publisher: "用户私有声明 · fixture", published_at: "2026-08-09", retrieved_at: "2026-08-10T00:00:00Z", excerpt: "专家认为订单可见度改善，但访谈不能替代公开证据。", user_note: "D 级：不可独立复核，不作为公开事实。", content_hash: "fixture-evidence-d", grade: "D", supports_or_challenges: "context", independently_reviewable: false },
  { evidence_id: "fixture-e", challenge_id: "mobile-challenge", source_type: "model_inference", url: "", title: "电网设备订单弹性", publisher: "本地模型推断 · fixture", published_at: null, retrieved_at: "2026-08-10T00:00:00Z", excerpt: "模型推断订单可能对需求增长更敏感。", user_note: "E 级：不可作为公开事实。", content_hash: "fixture-evidence-e", grade: "E", supports_or_challenges: "context", independently_reviewable: false },
];

function range(low: number, high: number): { readonly low: number; readonly high: number } { return { low, high }; }

export function makeBranches(viewpoint: string): readonly Branch[] {
  const suffix = sha256Hex(viewpoint.trim() || "empty-thesis").slice(0, 8);
  const common = [
    { id: `${suffix}-0`, time: "现在", event: "前提被记录", occurrenceDate: "2026-08-10", revealDate: "2026-08-10" },
    { id: `${suffix}-1`, time: "+6 个月", event: "需求与投资指引", occurrenceDate: "2027-02-10", revealDate: "2027-02-28" },
    { id: `${suffix}-2`, time: "+12 个月", event: "资本开支与订单验证", occurrenceDate: "2027-08-10", revealDate: "2027-08-31", invalidation: "需求增速低于基准率" },
    { id: `${suffix}-3`, time: "+24 个月", event: "估值再定价或路径失效", occurrenceDate: "2028-08-10", revealDate: "2028-08-31" },
  ];
  const build = (kind: BranchKind, title: string, interval: [number, number], conditional: [number, number], joint: [number, number], description: string, reveal: string): Branch => ({
    kind, title, interval: range(...interval), conditional: range(...conditional), joint: range(...joint), description, reveal,
    nodes: common.map((node, index) => ({ ...node, id: `${node.id}-${kind}`, event: index === 1 && kind === "weak" ? "需求与投资延迟" : index === 1 && kind === "strong" ? "政策与融资加速" : node.event })),
  });
  return [
    build("weak", "弱 W−", [0.20, 0.35], [0.20, 0.35], [0.07, 0.17], `「${viewpoint.trim() || "你的观点"}」的传导低于预期。`, "订单与指引没有展开"),
    build("main", "主 W₀", [0.45, 0.62], [0.45, 0.62], [0.16, 0.30], `「${viewpoint.trim() || "你的观点"}」部分兑现，投资逐步验证。`, "订单增速与现金流可见"),
    build("strong", "强 W+", [0.12, 0.25], [0.12, 0.25], [0.04, 0.12], `「${viewpoint.trim() || "你的观点"}」触发二阶效应。`, "订单与政策同时超预期"),
  ];
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
