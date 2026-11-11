import type { AllocationRow, BranchKind, NavPoint, PortfolioMode, ScenarioNav } from "./types";

export type ScenarioReturns = Readonly<Record<string, string>>;
export type ScenarioDrafts = Partial<Readonly<Record<BranchKind, ScenarioReturns>>>;
export interface ScenarioInputs {
  readonly rows: readonly AllocationRow[];
  readonly capital: number;
  readonly mode: PortfolioMode;
  readonly baselineDate: string;
  readonly horizonEnd: string;
  readonly returns: ScenarioReturns;
}
export function parseScenarioReturn(text: string | undefined): number | null {
  if (typeof text !== "string" || text.length > 24 || !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(text.trim())) return null;
  const value = Number(text.trim());
  return Number.isFinite(value) && value >= -100 && value <= 1000 ? value : null;
}
/** User-entered base-currency shocks, rounded holdings, zero-yield residual cash; no observed history. */
export function scenarioNavFor(branch: BranchKind, input?: ScenarioInputs): ScenarioNav {
  const empty: ScenarioNav = { branch, status: "missing_inputs", dataAsOf: "", simulatedFrom: input?.baselineDate ?? "", fixtureState: "user_assumptions", points: [], scenarioReturn: null, maxDrawdown: null, missingAssets: [], inputs: [], residualCash: 0 };
  if (!input) return empty;
  const start = Date.parse(input.baselineDate); const end = Date.parse(input.horizonEnd);
  const amounts = input.rows.map(row => input.mode === "raw" ? row.rawAmount : row.publicAmount);
  const total = amounts.reduce((sum, amount) => sum + amount, 0);
  const valid = Number.isFinite(input.capital) && input.capital > 0 && input.rows.length > 0 && Number.isFinite(start) && Number.isFinite(end) && end > start && Number.isFinite(total) && total <= input.capital * (1 + 1e-12) && amounts.every(amount => Number.isFinite(amount) && amount >= 0) && new Set(input.rows.map(row => row.asset.id)).size === input.rows.length;
  if (!valid) return { ...empty, status: "invalid_inputs" };
  const inputs = input.rows.flatMap((row, index) => amounts[index]! > 0 ? [{ assetId: row.asset.id, amount: amounts[index]!, weight: amounts[index]! / input.capital, returnPercent: row.asset.assetClass === "cash" ? 0 : parseScenarioReturn(input.returns[row.asset.id]), cash: row.asset.assetClass === "cash" }] : []);
  const missingAssets = inputs.filter(item => item.returnPercent === null).map(item => item.assetId);
  const context = { ...empty, inputs, missingAssets, residualCash: Math.max(0, input.capital - total), dataAsOf: input.rows.map(row => row.asset.asOf).sort().at(-1) ?? "" };
  if (missingAssets.length) return context;
  const terminalReturn = Math.max(-1, inputs.reduce((sum, item) => sum + item.weight * item.returnPercent! / 100, 0));
  let peak = 100;
  const points: NavPoint[] = Array.from({ length: 25 }, (_, index) => {
    const nav = 100 * (1 + terminalReturn * index / 24); peak = Math.max(peak, nav);
    return { date: new Date(start + (end - start) * index / 24).toISOString().slice(0, 10), nav, returnFromStart: nav / 100 - 1, drawdown: nav / peak - 1, segment: "simulated" };
  });
  return { ...context, status: "ready", points, scenarioReturn: terminalReturn, maxDrawdown: Math.min(...points.map(point => point.drawdown)) };
}

export interface ScopedScenarioDraft { readonly scope: string; readonly values: ScenarioDrafts; }
const EMPTY_DRAFTS: ScenarioDrafts = {};
export function scenarioDraftFor(saved: ScopedScenarioDraft, scope: string): ScenarioDrafts { return saved.scope === scope ? saved.values : EMPTY_DRAFTS; }
export function updateScenarioDraft(saved: ScopedScenarioDraft, scope: string, branch: BranchKind, assetId: string, value: string): ScopedScenarioDraft {
  if (value.length > 24 || !assetId || ["__proto__", "constructor", "prototype"].includes(assetId)) return saved;
  const current = scenarioDraftFor(saved, scope);
  return { scope, values: { ...current, [branch]: { ...current[branch], [assetId]: value } } };
}
