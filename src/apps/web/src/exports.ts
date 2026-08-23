import type { ComputeFmResult } from "@foresightnine/fm-engine";
import { buildNavExport } from "./nav-data";
import { demoThesis, MARKET_DATA_AS_OF, PUBLIC_CONSTRAINTS, sourceMaterialHash } from "./fixtures";
import type { AllocationRow, AppState, BranchView } from "./types";

export const EXPORT_METHOD_VERSION = "Web-Export-v1 / FM-v1 / Public-Default-v1";

function jsonSafe(value: unknown): unknown {
  if (value instanceof Float64Array) return Array.from(value);
  if (value instanceof Map) return Array.from(value.entries()).map(([key, item]) => [jsonSafe(key), jsonSafe(item)]);
  if (Array.isArray(value)) return value.map((item) => jsonSafe(item));
  if (value !== null && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, jsonSafe(item)]));
  return value;
}

export function serializeFmResult(result: ComputeFmResult): Record<string, unknown> {
  return {
    cutoff: result.cutoff,
    selectedFactorIds: [...result.selectedFactorIds],
    factorSignalsByTime: jsonSafe(result.factorSignalsByTime),
    lastFactorDescriptors: jsonSafe(result.lastFactorDescriptors),
    combination: jsonSafe(result.combination),
    weights: jsonSafe(result.weights),
    forecastScale: jsonSafe(result.forecastScale),
    forecasts: Array.from(result.forecasts),
    metrics: result.metrics ? { icSeries: Array.from(result.metrics.icSeries), icMean: result.metrics.icMean, icir: result.metrics.icir } : null,
    rebalance: jsonSafe(result.rebalance),
    deadband: jsonSafe(result.deadband),
    reasons: [...result.reasons],
    rawClientResult: { targetWeights: Array.from(result.rawClientResult.targetWeights), forecastReturns: Array.from(result.rawClientResult.forecastReturns), transactionCost: result.rawClientResult.transactionCost, reasons: [...result.rawClientResult.reasons] },
  };
}

function evidenceExport(item: AppState["evidence"][number]): Record<string, unknown> {
  return { evidence_id: item.evidence_id, challenge_id: item.challenge_id, url: item.url, title: item.title, publisher: item.publisher, published_at: item.published_at, retrieved_at: item.retrieved_at, excerpt: item.excerpt, user_note: item.user_note, content_hash: item.content_hash, source_material_hash: sourceMaterialHash({ url: item.url, title: item.title, published_at: item.published_at, retrieved_at: item.retrieved_at, excerpt: item.excerpt, user_note: item.user_note }), grade: item.grade, source_type: item.source_type, independently_reviewable: item.independently_reviewable, supports_or_challenges: item.supports_or_challenges, fixture: item.fixture ?? false };
}

function worldlineExport(branch: BranchView): Record<string, unknown> {
  const assessment = branch.assessment;
  return { kind: branch.kind, title: branch.title, current_premise: branch.worldline.premise, probability: { premise_probability: assessment.premise_probability, premise_probability_range: assessment.premise_probability_range, conditional_probability: assessment.conditional_probability, conditional_probability_range: assessment.conditional_probability_range, joint_probability: assessment.joint_probability, joint_probability_range: assessment.joint_probability_range, confidence: assessment.confidence, calibration_status: assessment.calibration_status, adjustment_reasons: assessment.adjustment_reasons }, nodes: branch.worldline.nodes, asset_anchors: branch.worldline.asset_anchors, reveal: branch.worldline.event_reveal_schedule, invalidation: branch.worldline.invalidation_conditions, worldline: branch.worldline };
}

function allocationExport(state: AppState, rows: readonly AllocationRow[]): Record<string, unknown> {
  if (!state.fmResult) return { status: "not_computed", mode: state.portfolioMode, currency: state.currency, allocations: [], note: "FM 尚未计算；没有导出任何权重、金额或单位。" };
  const rawAllocated = rows.reduce((sum, row) => sum + row.rawAmount, 0);
  const publicAllocated = rows.reduce((sum, row) => sum + row.publicAmount, 0);
  const capital = Number(state.capital.replace(/,/g, ""));
  return { mode: state.portfolioMode, currency: state.currency, selected: { amount: rows.reduce((sum, row) => sum + row.amount, 0), units: rows.reduce((sum, row) => sum + row.units, 0), residual_cash: Math.max(0, capital - rows.reduce((sum, row) => sum + row.amount, 0)) }, raw_residual_cash: Math.max(0, capital - rawAllocated), public_residual_cash: Math.max(0, capital - publicAllocated), constraints: PUBLIC_CONSTRAINTS, allocations: rows.map((row) => ({ asset: row.id, role: row.role, currency: row.currency, quote_currency: row.quoteCurrency, price: row.price, lot_size: row.lotSize, instrument_id: row.instrumentId, proxy_instrument: row.proxyInstrument ?? null, data_as_of: row.dataAsOf, raw: { weight: row.weight, amount: row.rawAmount, units: row.rawUnits, residual_reason: row.rawResidualReason ?? null }, public: { weight: row.publicWeight, amount: row.publicAmount, units: row.publicUnits, residual_reason: row.publicResidualReason ?? null }, selected: { weight: state.portfolioMode === "public" ? row.publicWeight : row.weight, amount: row.amount, units: row.units, residual_reason: row.residualReason ?? null }, constraint_reasons: row.constraintReasons, tradable: row.tradable, liquidity: row.liquidity, volatility: row.volatility })) };
}

export function researchExport(state: AppState, branches: readonly BranchView[], rows: readonly AllocationRow[]): Record<string, unknown> {
  const capital = Number(state.capital.replace(/,/g, ""));
  return {
    product: { name: "ForesightNine", mode: "public research", simulated_only: true },
    method_version: EXPORT_METHOD_VERSION,
    data_as_of: MARKET_DATA_AS_OF,
    thesis: demoThesis(state.viewpoint, capital, state.currency),
    challenge: { status: state.challengeStatus, confirmation: state.confirmation, transcript: state.challengeTurns, evidence_ids: state.evidence.map((item) => item.evidence_id) },
    evidence: state.evidence.map(evidenceExport),
    worldlines: branches.map(worldlineExport),
    fm: state.fmResult ? { status: "computed", fixture: true, data_source: "deterministic offline fixture computed in the client", result: serializeFmResult(state.fmResult) } : { status: "not_computed", fixture: true, result: null, note: "No local FM result exists yet; no weights or forecasts were fabricated." },
    portfolio: allocationExport(state, rows),
    nav: navExport(state, branches),
    labels: { historical: "historical_only", simulated: "simulated fixture", probability: "model_estimate_unvalidated", product: "not investment advice", execution: "no broker, order, or custody" },
    risk_disclosures: ["情景研究与组合模拟，不构成投资建议。", "历史数据与模拟路径不代表未来表现。", "金额和单位仅按 deterministic fixture quote/FX snapshot 模拟，不是 live market quote。", "数据许可、法律审查和生产市场数据仍是公开发布 gate。"],
  };
}

export function navExport(state: AppState, branches: readonly BranchView[]): Record<string, unknown> {
  const nav = buildNavExport();
  return { ...nav, selected: state.selectedBranch, current_premise: branches.find((branch) => branch.kind === state.selectedBranch)?.worldline.premise ?? state.viewpoint, worldlines: branches.map(worldlineExport), method_version: EXPORT_METHOD_VERSION, data_as_of: MARKET_DATA_AS_OF, labels: { historical: "historical_only", simulated: "simulated fixture" } };
}

export const CSV_HEADER = ["asset", "role", "currency", "raw_weight", "public_weight", "raw_amount", "public_amount", "raw_units", "public_units", "selected_amount", "selected_units", "price", "lot_size", "quote_currency", "instrument_id", "proxy_instrument", "raw_residual_reason", "public_residual_reason", "selected_reason", "constraint_reasons", "data_as_of", "method_version"] as const;

export function csvExport(rows: readonly AllocationRow[]): string {
  const lines = rows.map((row) => [row.id, row.role, row.currency, row.weight.toFixed(6), row.publicWeight.toFixed(6), row.rawAmount.toFixed(2), row.publicAmount.toFixed(2), row.rawUnits.toFixed(6), row.publicUnits.toFixed(6), row.amount.toFixed(2), row.units.toFixed(6), row.price.toFixed(4), row.lotSize.toString(), row.quoteCurrency, row.instrumentId, row.proxyInstrument ?? "", row.rawResidualReason ?? "", row.publicResidualReason ?? "", row.residualReason ?? "", row.constraintReasons.map((reason) => `${reason.code}:${reason.detail}`).join("; "), row.dataAsOf, EXPORT_METHOD_VERSION].map(csvCell).join(","));
  return [CSV_HEADER.join(","), ...lines].join("\n");
}

function csvCell(value: string): string { return /[",\n\r]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value; }

export function downloadText(filename: string, content: string, mime = "text/plain;charset=utf-8"): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url; anchor.download = filename; anchor.click();
  URL.revokeObjectURL(url);
}
