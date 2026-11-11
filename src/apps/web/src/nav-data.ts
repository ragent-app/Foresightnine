import type { AllocationRow, BranchView } from "./types";

export const BRANCH_KINDS = ["weak", "main", "strong"] as const;
export type BranchKind = BranchView["kind"];
export type PathMap = Readonly<Record<BranchKind, readonly number[]>>;
/** Explicit user assumptions: worldline ID -> instrument ID -> horizon return in percent. */
export type NavAssumptions = Readonly<Record<string, Readonly<Record<string, string>>>>;
export const NAV_METHOD = "user-shock-buy-and-hold-linear-v1";
export interface ScenarioPath {
  readonly status: "ready" | "missing_inputs" | "invalid_inputs";
  readonly worldlineId: string;
  readonly missingAssets: readonly string[];
  readonly terminalReturn: number | null;
  readonly inputs: readonly { asset: string; instrument: string; amount: number; weight: number; returnPercent: number | null; dataAsOf: string }[];
}
export interface NavExportData {
  readonly status: "ready" | "partial" | "not_available";
  readonly method: string;
  readonly historical: readonly number[];
  readonly weak: readonly number[];
  readonly main: readonly number[];
  readonly strong: readonly number[];
  readonly dates: readonly string[];
  readonly drawdown: PathMap;
  readonly scenarios: Readonly<Record<BranchKind, ScenarioPath>>;
  readonly capital: number;
  readonly residualCash: number;
  readonly boundary: { readonly historicalCount: number; readonly simulatedStartIndex: number; readonly label: string };
  readonly assumptions: readonly string[];
}
export function parseReturn(value: string | undefined): number | null {
  if (typeof value !== "string" || !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(value.trim())) return null;
  const number = Number(value.trim());
  return Number.isFinite(number) && number >= -100 && number <= 1000 ? number : null;
}
export function toDrawdown(values: readonly number[]): readonly number[] {
  let peak = 1;
  return values.map(value => { peak = Math.max(peak, value); return value / peak - 1; });
}
export function drawdownPaths(paths: PathMap): PathMap {
  return {weak:toDrawdown(paths.weak),main:toDrawdown(paths.main),strong:toDrawdown(paths.strong)};
}
export function buildNavExport(branches: readonly BranchView[] = [], rows: readonly AllocationRow[] = [], capital = 0, assumptions: NavAssumptions = {}): NavExportData {
  const first = branches[0]?.worldline;
  const start = Date.parse(first?.baseline_date ?? "");
  const end = Date.parse(first?.horizon_end ?? "");
  const validDates = Number.isFinite(start) && Number.isFinite(end) && end > start && branches.every(b => Date.parse(b.worldline.baseline_date) === start && Date.parse(b.worldline.horizon_end) === end);
  const allocated = rows.reduce((sum, row) => sum + row.amount, 0);
  // Values use selected rounded amounts, NOT target weights. Leftover capital stays cash.
  const validAllocation = Number.isFinite(capital) && capital > 0 && rows.length > 0 && rows.every(r => Number.isFinite(r.amount) && r.amount >= 0) && Number.isFinite(allocated) && allocated <= capital + 1e-6 && new Set(rows.map(r => r.instrumentId)).size === rows.length;
  const held = validAllocation ? rows.filter(r => r.amount > 0) : [];
  const paths: Record<BranchKind, number[]> = {weak:[],main:[],strong:[]};
  const scenarios = {} as Record<BranchKind, ScenarioPath>;
  for (const kind of BRANCH_KINDS) {
    const branch = branches.find(b => b.kind === kind);
    const worldlineId = branch?.worldline.worldline_id ?? "";
    const inputs = held.map(row => ({asset:row.id,instrument:row.instrumentId,amount:row.amount,weight:row.amount / capital,returnPercent:parseReturn(assumptions[worldlineId]?.[row.instrumentId]),dataAsOf:row.dataAsOf}));
    const missingAssets = inputs.filter(i => i.returnPercent === null).map(i => i.asset);
    const valid = validAllocation && validDates && branches.filter(b => b.kind === kind).length === 1;
    const ready = valid && missingAssets.length === 0;
    const terminalReturn = ready ? inputs.reduce((sum,i) => sum + i.weight * i.returnPercent! / 100,0) : null;
    if (ready) paths[kind] = Array.from({length:25}, (_,i) => 1 + terminalReturn! * i / 24);
    scenarios[kind] = {status: !valid ? "invalid_inputs" : ready ? "ready" : "missing_inputs",worldlineId,missingAssets,terminalReturn,inputs};
  }
  const count = BRANCH_KINDS.filter(k => scenarios[k].status === "ready").length;
  return {status:count === 3 ? "ready" : count > 0 ? "partial" : "not_available",method:NAV_METHOD,historical:[],...paths,
    dates:validDates ? Array.from({length:25},(_,i) => new Date(start + (end-start)*i/24).toISOString().slice(0,10)) : [],
    drawdown:drawdownPaths(paths),scenarios,capital:Number.isFinite(capital) ? capital : 0,residualCash:validAllocation ? Math.max(0,capital-allocated) : 0,
    boundary:{historicalCount:0,simulatedStartIndex:0,label:"User assumptions only; no observed history"},
    assumptions:["Explicit user-entered horizon returns, not provider forecasts or model-calibrated returns.","Fixed units; selected rounded allocation amounts; residual cash earns zero; no leverage/rebalancing.","Linear interpolation over the worldline horizon; no volatility fit, random noise, quantiles or probability weighting.","Returns are in the portfolio base currency; FX changes must be included by the user. Fees, taxes, interest and dividends are not added separately.","Drawdown is of this assumed linear path only, not an estimate of real intra-period risk."]};
}
