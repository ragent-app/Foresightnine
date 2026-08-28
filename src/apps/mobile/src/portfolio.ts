import { allocateCapital, type ComputeFmResult } from "@foresightnine/fm-engine";
import { ASSETS, FIXTURE_STATE, FX_SNAPSHOT, MARKET_DATA_AS_OF, WORLDLINE_BASELINE_DATE } from "./fixtures";
import { t, type CopyKey } from "./i18n";
import type { AllocationRow, AssetFixture, BranchKind, Currency, Language, NavPoint, PortfolioMode, ScenarioNav } from "./types";

const SINGLE_SECURITY_CAP = 0.25;
const HIGH_VOLATILITY_CLASS_CAP = 0.35;
const EPSILON = 1e-9;

const assetCopy: Readonly<Record<string, readonly [CopyKey, CopyKey]>> = {
  GRID: ["assetGrid", "roleGrid"], CHIP: ["assetChip", "roleChip"], UTIL: ["assetUtil", "roleUtil"], GOLD: ["assetGold", "roleGold"], WORLD: ["assetWorld", "roleWorld"], CASH: ["assetCash", "roleCash"],
};

export function displayAsset(asset: AssetFixture, language: Language): { readonly name: string; readonly role: string } {
  const keys = assetCopy[asset.id];
  return keys ? { name: t(language, keys[0]), role: t(language, keys[1]) } : { name: asset.name, role: asset.role };
}

function fxToBase(quote: Currency, base: Currency): number {
  return FX_SNAPSHOT.cnyPerUnit[quote] / FX_SNAPSHOT.cnyPerUnit[base];
}

function sum(values: ArrayLike<number>): number {
  let total = 0;
  for (let index = 0; index < values.length; index += 1) total += values[index] ?? 0;
  return total;
}

function isCash(asset: AssetFixture): boolean { return asset.assetClass === "cash"; }
function rawExecutable(asset: AssetFixture): boolean { return isCash(asset) || asset.tradable || Boolean(asset.proxy); }

/** A proxy is allowed only when it is explicitly named; unknown economic exposure is never silently tradable. */
export function isPublicEligible(asset: AssetFixture): boolean {
  return isCash(asset) || (rawExecutable(asset) && asset.liquidity !== "low" && asset.freshness === "current");
}

function securityCap(asset: AssetFixture): number { return isCash(asset) ? 1 : SINGLE_SECURITY_CAP; }

function fillRemainder(result: Float64Array, preferred: ArrayLike<number>, eligible: readonly boolean[], caps: readonly number[]): void {
  for (let iteration = 0; iteration < 16; iteration += 1) {
    const remainder = 1 - sum(result);
    if (remainder <= EPSILON) return;
    const open = eligible.map((allowed, index) => allowed && result[index]! < caps[index]! - EPSILON ? index : -1).filter((index) => index >= 0);
    if (open.length === 0) return;
    const preferenceTotal = open.reduce((total, index) => total + Math.max(preferred[index] ?? 0, 0), 0);
    let allocated = 0;
    for (const index of open) {
      const proportion = preferenceTotal > EPSILON ? Math.max(preferred[index] ?? 0, 0) / preferenceTotal : 1 / open.length;
      const addition = Math.min(remainder * proportion, caps[index]! - result[index]!);
      result[index] = result[index]! + addition;
      allocated += addition;
    }
    if (allocated <= EPSILON) return;
  }
}

function normalizePublicWeights(raw: Float64Array, assets: readonly AssetFixture[]): Float64Array {
  const eligible = assets.map(isPublicEligible);
  const caps = assets.map(securityCap);
  const initial = Float64Array.from(raw, (weight, index) => eligible[index] ? Math.min(Math.max(weight, 0), caps[index]!) : 0);
  const initialTotal = sum(initial);
  if (initialTotal > 1 + EPSILON) {
    for (let index = 0; index < initial.length; index += 1) initial[index] = initial[index]! / initialTotal;
  }
  fillRemainder(initial, raw, eligible, caps);

  const highVolatility = assets.reduce((total, asset, index) => total + (asset.volatility === "high" ? initial[index]! : 0), 0);
  if (highVolatility > HIGH_VOLATILITY_CLASS_CAP + EPSILON) {
    const scale = HIGH_VOLATILITY_CLASS_CAP / highVolatility;
    for (let index = 0; index < initial.length; index += 1) if (assets[index]!.volatility === "high") initial[index] = initial[index]! * scale;
    fillRemainder(initial, raw, assets.map((asset, index) => eligible[index]! && asset.volatility !== "high"), caps);
  }
  return initial;
}

/** Public Default is a separate, deterministic constraint layer; it never mutates FM Raw. */
export function publicWeights(raw: Float64Array, assets: readonly AssetFixture[] = ASSETS): Float64Array {
  if (raw.length !== assets.length) throw new RangeError("raw weights and assets must share a shape");
  return normalizePublicWeights(raw, assets);
}

interface AllocationValue { readonly amount: number; readonly units: number; readonly residual: number; }
interface AllocationComputation {
  readonly values: ReadonlyMap<string, AllocationValue>;
  readonly cashRemainder: number;
  readonly executable: boolean;
}

function allocationFor(weights: Float64Array, capital: number, baseCurrency: Currency, assets: readonly AssetFixture[], requirePublicEligibility: boolean): AllocationComputation {
  const values = new Map<string, AllocationValue>();
  const candidates = assets.map((asset, index) => ({ asset, index, weight: Math.max(weights[index] ?? 0, 0) })).filter((candidate) => candidate.weight > EPSILON && (!requirePublicEligibility || isPublicEligible(candidate.asset)));
  let executable = candidates.every((candidate) => rawExecutable(candidate.asset));
  if (candidates.length === 0) return { values, cashRemainder: capital, executable: false };
  const products = candidates.map(({ asset, weight }) => ({ id: asset.id, weight, price: asset.price, fxToBase: fxToBase(asset.quoteCurrency, baseCurrency), lotSize: asset.lotSize, fractionalSupported: asset.fractional }));
  const allocated = allocateCapital(capital, products);
  const byId = new Map(allocated.allocations.map((item) => [item.id, item]));
  for (const candidate of candidates) {
    const item = byId.get(candidate.asset.id)!;
    const desired = capital * candidate.weight;
    values.set(candidate.asset.id, { amount: item.allocatedAmount, units: item.units, residual: Math.max(desired - item.allocatedAmount, 0) });
  }
  const cashRemainder = Math.max(allocated.cashRemainder, 0);
  const cash = assets.find(isCash);
  if (cashRemainder > EPSILON) {
    if (!cash || (requirePublicEligibility && !isPublicEligible(cash))) executable = false;
    else {
      const current = values.get(cash.id) ?? { amount: 0, units: 0, residual: 0 };
      const unitValue = cash.price * fxToBase(cash.quoteCurrency, baseCurrency);
      values.set(cash.id, { amount: current.amount + cashRemainder, units: current.units + cashRemainder / unitValue, residual: current.residual });
    }
  }
  return { values, cashRemainder, executable };
}

const HISTORICAL_NAV: readonly [string, number][] = [["2026-08-05", 96], ["2026-08-07", 99], [MARKET_DATA_AS_OF, 100]];
const SIMULATED_NAV: Readonly<Record<BranchKind, readonly [string, number][]>> = {
  weak: [[WORLDLINE_BASELINE_DATE, 100], ["2027-02-28", 97], ["2027-08-31", 93], ["2028-08-31", 88]],
  main: [[WORLDLINE_BASELINE_DATE, 100], ["2027-02-28", 104], ["2027-08-31", 109], ["2028-08-31", 114]],
  strong: [[WORLDLINE_BASELINE_DATE, 100], ["2027-02-28", 110], ["2027-08-31", 119], ["2028-08-31", 130]],
};

/** Builds an explicitly mock, selected-worldline NAV series and a running-peak drawdown. */
export function scenarioNavFor(branch: BranchKind): ScenarioNav {
  const raw = [...HISTORICAL_NAV.map(([date, nav]) => ({ date, nav, segment: "historical" as const })), ...SIMULATED_NAV[branch].map(([date, nav]) => ({ date, nav, segment: "simulated" as const }))];
  let peak = 0;
  const first = raw[0]!.nav;
  const points: NavPoint[] = raw.map((point) => {
    peak = Math.max(peak, point.nav);
    return { ...point, returnFromStart: point.nav / first - 1, drawdown: point.nav / peak - 1 };
  });
  const base = HISTORICAL_NAV.at(-1)![1];
  return { branch, dataAsOf: MARKET_DATA_AS_OF, simulatedFrom: WORLDLINE_BASELINE_DATE, fixtureState: FIXTURE_STATE, points, scenarioReturn: points.at(-1)!.nav / base - 1, maxDrawdown: Math.min(...points.map((point) => point.drawdown)) };
}

export interface PortfolioPlan {
  readonly rows: readonly AllocationRow[];
  readonly rawTotal: number;
  readonly publicTotal: number;
  readonly rawMarketValue: number;
  readonly publicMarketValue: number;
  readonly rawCashRemainder: number;
  readonly publicCashRemainder: number;
  readonly rawImplementable: boolean;
  readonly publicImplementable: boolean;
  readonly scenario: ScenarioNav;
  readonly rawReasons: readonly string[];
}

function constraintReasons(asset: AssetFixture, rawWeight: number, publicWeight: number, raw: Float64Array, assets: readonly AssetFixture[], language: Language): string[] {
  const reasons: string[] = [];
  if (asset.proxy) reasons.push(`${t(language, "proxyReason")} ${asset.proxy}`);
  if (!isPublicEligible(asset) && rawWeight > EPSILON) reasons.push(asset.freshness === "stale" ? t(language, "staleReason") : t(language, "liquidityReason"));
  if (asset.volatility === "high" && publicWeight < rawWeight - EPSILON) {
    const highWeight = assets.reduce((total, candidate, index) => total + (candidate.volatility === "high" ? raw[index]! : 0), 0);
    reasons.push(highWeight > HIGH_VOLATILITY_CLASS_CAP + EPSILON ? t(language, "highVolatilityCap") : t(language, "singleSecurityCap"));
  }
  if (!isCash(asset) && rawWeight > SINGLE_SECURITY_CAP + EPSILON && publicWeight < rawWeight - EPSILON) reasons.push(t(language, "singleSecurityCap"));
  if (Math.abs(publicWeight - rawWeight) > EPSILON && reasons.length === 0) reasons.push(t(language, "publicRebalanced"));
  return reasons;
}

export function createPortfolioPlan(result: ComputeFmResult, capital: number, baseCurrency: Currency, assets: readonly AssetFixture[] = ASSETS, language: Language = "zh", selectedBranch: BranchKind = "main"): PortfolioPlan {
  const raw = result.weights.weights;
  if (raw.length !== assets.length) throw new RangeError("FM result and asset registry must share a shape");
  const safe = publicWeights(raw, assets);
  const rawAllocations = allocationFor(raw, capital, baseCurrency, assets, false);
  const publicAllocations = allocationFor(safe, capital, baseCurrency, assets, true);
  const rows = assets.map((asset, index) => {
    const rawWeight = raw[index] ?? 0;
    const publicWeight = safe[index] ?? 0;
    const rawAllocation = rawAllocations.values.get(asset.id) ?? { amount: 0, units: 0, residual: 0 };
    const publicAllocation = publicAllocations.values.get(asset.id) ?? { amount: 0, units: 0, residual: 0 };
    const reasons = constraintReasons(asset, rawWeight, publicWeight, raw, assets, language);
    if (rawAllocation.residual > EPSILON || publicAllocation.residual > EPSILON) reasons.push(t(language, "lotReason"));
    return { asset, rawWeight, publicWeight, rawAmount: rawAllocation.amount, publicAmount: publicAllocation.amount, rawUnits: rawAllocation.units, publicUnits: publicAllocation.units, rawResidual: rawAllocation.residual, publicResidual: publicAllocation.residual, rawExecutable: rawExecutable(asset), publicExecutable: isPublicEligible(asset), reasons };
  });
  return {
    rows,
    rawTotal: sum(raw), publicTotal: sum(safe),
    rawMarketValue: rows.reduce((total, row) => total + row.rawAmount, 0), publicMarketValue: rows.reduce((total, row) => total + row.publicAmount, 0),
    rawCashRemainder: rawAllocations.cashRemainder, publicCashRemainder: publicAllocations.cashRemainder,
    rawImplementable: rawAllocations.executable, publicImplementable: publicAllocations.executable,
    scenario: scenarioNavFor(selectedBranch), rawReasons: result.reasons.map((reason) => reason.replaceAll("_", " ")),
  };
}

export function formatMoney(value: number, currency: Currency, language: Language = "zh"): string {
  return new Intl.NumberFormat(language === "zh" ? "zh-CN" : "en-US", { style: "currency", currency, maximumFractionDigits: 0 }).format(value);
}

export function modeWeight(row: AllocationRow, mode: PortfolioMode): number { return mode === "raw" ? row.rawWeight : row.publicWeight; }
export const portfolioMetadata = { method: "FM-v1 / Public-Default-v2", dataAsOf: MARKET_DATA_AS_OF, navLabel: "historical reference plus simulated scenario NAV; not a return forecast" } as const;
