import { allocateCapital, type ComputeFmResult } from "@foresightnine/fm-engine";
import { ASSETS, FRESHNESS_POLICY, FX_SNAPSHOT, MARKET_DATA_AS_OF, PROVIDER_FRESHNESS_STATUS, RUNTIME_AS_OF, freshnessFor, hasCurrentFx } from "./fixtures";
import { t, type CopyKey } from "./i18n";
import type { AllocationRow, AssetFixture, BranchKind, Currency, FreshnessOptions, Language, PortfolioMode, ScenarioNav } from "./types";

import { scenarioNavFor, type ScenarioInputs } from "./scenario-inputs";
export { scenarioNavFor } from "./scenario-inputs";

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

function fxToBase(quote: Currency, base: Currency, cnyPerUnit: Readonly<Record<string, number>> = FX_SNAPSHOT.cnyPerUnit): number {
  const quoteRate = cnyPerUnit[quote];
  const baseRate = cnyPerUnit[base];
  if (typeof quoteRate !== "number" || typeof baseRate !== "number" || !Number.isFinite(quoteRate) || !Number.isFinite(baseRate) || quoteRate <= 0 || baseRate <= 0) return Number.NaN;
  return quoteRate / baseRate;
}

function sum(values: ArrayLike<number>): number {
  let total = 0;
  for (let index = 0; index < values.length; index += 1) total += values[index] ?? 0;
  return total;
}

function isCash(asset: AssetFixture): boolean { return asset.assetClass === "cash"; }
function rawExecutable(asset: AssetFixture): boolean { return isCash(asset) || asset.tradable || Boolean(asset.proxy); }

/** A proxy is allowed only when it is explicitly named; unknown economic exposure is never silently tradable. */
export function isPublicEligible(asset: AssetFixture, freshnessOptions: FreshnessOptions = { runtimeAsOf: RUNTIME_AS_OF, providerStatus: PROVIDER_FRESHNESS_STATUS, policy: FRESHNESS_POLICY }): boolean {
  return hasCurrentFx(freshnessOptions, ["CNY", asset.quoteCurrency]) && (isCash(asset) ? freshnessFor(asset, freshnessOptions).allowCurrentPublicDefault : (rawExecutable(asset) && asset.liquidity !== "low" && freshnessFor(asset, freshnessOptions).allowCurrentPublicDefault));
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

function normalizePublicWeights(raw: Float64Array, assets: readonly AssetFixture[], freshnessOptions: FreshnessOptions): Float64Array {
  const eligible = assets.map((asset) => isPublicEligible(asset, freshnessOptions));
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
export function publicWeights(raw: Float64Array, assets: readonly AssetFixture[] = ASSETS, freshnessOptions: FreshnessOptions = { runtimeAsOf: RUNTIME_AS_OF, providerStatus: PROVIDER_FRESHNESS_STATUS, policy: FRESHNESS_POLICY }): Float64Array {
  if (raw.length !== assets.length) throw new RangeError("raw weights and assets must share a shape");
  return normalizePublicWeights(raw, assets, freshnessOptions);
}

interface AllocationValue { readonly amount: number; readonly units: number; readonly residual: number; }
interface AllocationComputation {
  readonly values: ReadonlyMap<string, AllocationValue>;
  readonly cashRemainder: number;
  readonly executable: boolean;
}

function allocationFor(weights: Float64Array, capital: number, baseCurrency: Currency, assets: readonly AssetFixture[], requirePublicEligibility: boolean, freshnessOptions: FreshnessOptions): AllocationComputation {
  const values = new Map<string, AllocationValue>();
  const candidates = assets.map((asset, index) => ({ asset, index, weight: Math.max(weights[index] ?? 0, 0) })).filter((candidate) => candidate.weight > EPSILON && (!requirePublicEligibility || isPublicEligible(candidate.asset, freshnessOptions)));
  let executable = candidates.every((candidate) => rawExecutable(candidate.asset));
  if (candidates.length === 0) return { values, cashRemainder: capital, executable: false };
  const products = candidates.map(({ asset, weight }) => { const fx = fxToBase(asset.quoteCurrency, baseCurrency, freshnessOptions.fxCnyPerUnit); return { id: asset.id, weight, price: asset.price, fxToBase: Number.isFinite(fx) && fx > 0 ? fx : 1, lotSize: asset.lotSize, fractionalSupported: asset.fractional }; });
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
    if (!cash || (requirePublicEligibility && !isPublicEligible(cash, freshnessOptions))) executable = false;
    else {
      const current = values.get(cash.id) ?? { amount: 0, units: 0, residual: 0 };
      const cashFx = fxToBase(cash.quoteCurrency, baseCurrency, freshnessOptions.fxCnyPerUnit);
      const unitValue = cash.price * (Number.isFinite(cashFx) && cashFx > 0 ? cashFx : 1);
      values.set(cash.id, { amount: current.amount + cashRemainder, units: current.units + cashRemainder / unitValue, residual: current.residual });
    }
  }
  return { values, cashRemainder, executable };
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

function constraintReasons(asset: AssetFixture, rawWeight: number, publicWeight: number, raw: Float64Array, assets: readonly AssetFixture[], language: Language, freshnessOptions: FreshnessOptions): string[] {
  const reasons: string[] = [];
  if (asset.proxy) reasons.push(`${t(language, "proxyReason")} ${asset.proxy}`);
    if (!hasCurrentFx(freshnessOptions, ["CNY", asset.quoteCurrency]) && rawWeight > EPSILON) reasons.push(language === "en" ? "Current FX data is unavailable; Public Default is disabled." : "当前汇率数据不可用，Public Default 已停用。");
    else if (!isPublicEligible(asset, freshnessOptions) && rawWeight > EPSILON) reasons.push(freshnessFor(asset, freshnessOptions).historicalOnly ? t(language, "staleReason") : t(language, "liquidityReason"));
  if (asset.volatility === "high" && publicWeight < rawWeight - EPSILON) {
    const highWeight = assets.reduce((total, candidate, index) => total + (candidate.volatility === "high" ? raw[index]! : 0), 0);
    reasons.push(highWeight > HIGH_VOLATILITY_CLASS_CAP + EPSILON ? t(language, "highVolatilityCap") : t(language, "singleSecurityCap"));
  }
  if (!isCash(asset) && rawWeight > SINGLE_SECURITY_CAP + EPSILON && publicWeight < rawWeight - EPSILON) reasons.push(t(language, "singleSecurityCap"));
  if (Math.abs(publicWeight - rawWeight) > EPSILON && reasons.length === 0) reasons.push(t(language, "publicRebalanced"));
  return reasons;
}

export function createPortfolioPlan(result: ComputeFmResult, capital: number, baseCurrency: Currency, assets: readonly AssetFixture[] = ASSETS, language: Language = "zh", selectedBranch: BranchKind = "main", freshnessOptions: FreshnessOptions = { runtimeAsOf: RUNTIME_AS_OF, providerStatus: PROVIDER_FRESHNESS_STATUS, policy: FRESHNESS_POLICY }, scenarioInput?: Omit<ScenarioInputs, "rows" | "capital">): PortfolioPlan {
  const raw = result.weights.weights;
  if (raw.length !== assets.length) throw new RangeError("FM result and asset registry must share a shape");
  const safe = publicWeights(raw, assets, freshnessOptions);
  const rawAllocations = allocationFor(raw, capital, baseCurrency, assets, false, freshnessOptions);
  const publicAllocations = allocationFor(safe, capital, baseCurrency, assets, true, freshnessOptions);
  const rows = assets.map((asset, index) => {
    const rawWeight = raw[index] ?? 0;
    const publicWeight = safe[index] ?? 0;
    const rawAllocation = rawAllocations.values.get(asset.id) ?? { amount: 0, units: 0, residual: 0 };
    const publicAllocation = publicAllocations.values.get(asset.id) ?? { amount: 0, units: 0, residual: 0 };
    const freshness = freshnessFor(asset, freshnessOptions);
    const reasons = constraintReasons(asset, rawWeight, publicWeight, raw, assets, language, freshnessOptions);
    if (rawAllocation.residual > EPSILON || publicAllocation.residual > EPSILON) reasons.push(t(language, "lotReason"));
    return { asset, rawWeight, publicWeight, rawAmount: rawAllocation.amount, publicAmount: publicAllocation.amount, rawUnits: rawAllocation.units, publicUnits: publicAllocation.units, rawResidual: rawAllocation.residual, publicResidual: publicAllocation.residual, rawExecutable: rawExecutable(asset), publicExecutable: isPublicEligible(asset, freshnessOptions), reasons, freshness: freshness.status, freshnessLabel: freshness.label, publicExecutionAllowed: isPublicEligible(asset, freshnessOptions) };
  });
  return {
    rows,
    rawTotal: sum(raw), publicTotal: sum(safe),
    rawMarketValue: rows.reduce((total, row) => total + row.rawAmount, 0), publicMarketValue: rows.reduce((total, row) => total + row.publicAmount, 0),
    rawCashRemainder: rawAllocations.cashRemainder, publicCashRemainder: publicAllocations.cashRemainder,
    rawImplementable: rawAllocations.executable, publicImplementable: publicAllocations.executable,
    scenario: scenarioNavFor(selectedBranch, scenarioInput ? { ...scenarioInput, rows, capital } : undefined), rawReasons: result.reasons.map((reason) => reason.replaceAll("_", " ")),
  };
}

export function formatMoney(value: number, currency: Currency, language: Language = "zh"): string {
  return new Intl.NumberFormat(language === "zh" ? "zh-CN" : "en-US", { style: "currency", currency, maximumFractionDigits: 0 }).format(value);
}

export function modeWeight(row: AllocationRow, mode: PortfolioMode): number { return mode === "raw" ? row.rawWeight : row.publicWeight; }
export const portfolioMetadata = { method: "FM-v1 / Public-Default-v2", dataAsOf: MARKET_DATA_AS_OF, navLabel: "user-entered scenario assumptions only; no observed historical NAV or return forecast" } as const;
