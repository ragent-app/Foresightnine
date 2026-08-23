import { allocateCapital, type ComputeFmResult } from "@foresightnine/fm-engine";
import { ASSETS, FX_SNAPSHOT, MARKET_DATA_AS_OF } from "./fixtures";
import type { AllocationRow, AssetFixture, Currency, PortfolioMode } from "./types";

function fxToBase(quote: Currency, base: Currency): number {
  return FX_SNAPSHOT.cnyPerUnit[quote] / FX_SNAPSHOT.cnyPerUnit[base];
}

function redistribute(weights: ArrayLike<number>, eligible: readonly boolean[]): Float64Array {
  const result = Float64Array.from(weights, (value, index) => eligible[index] ? Math.min(value, 0.25) : 0);
  for (let iteration = 0; iteration < 8; iteration += 1) {
    const remainder = 1 - Array.from(result).reduce((sum, value) => sum + value, 0);
    if (remainder <= 1e-9) break;
    const open = eligible.map((allowed, index) => allowed && result[index]! < 0.25 - 1e-9 ? index : -1).filter((index) => index >= 0);
    if (open.length === 0) break;
    const share = remainder / open.length;
    for (const index of open) result[index] = Math.min(0.25, result[index]! + share);
  }
  return result;
}

export function publicWeights(raw: Float64Array, assets: readonly AssetFixture[] = ASSETS): Float64Array {
  const eligible = assets.map((asset) => (asset.tradable || Boolean(asset.proxy)) && asset.liquidity !== "low" && asset.freshness === "current");
  const initial = Float64Array.from(raw, (weight, index) => eligible[index] ? weight : 0);
  return redistribute(initial, eligible);
}

function allocationFor(weights: Float64Array, capital: number, baseCurrency: Currency, assets: readonly AssetFixture[]): Map<string, { amount: number; units: number; residual: number }> {
  const products = assets.map((asset, index) => ({ id: asset.id, weight: weights[index]!, price: asset.price, fxToBase: fxToBase(asset.quoteCurrency, baseCurrency), lotSize: asset.lotSize, fractionalSupported: asset.fractional }));
  const allocation = allocateCapital(capital, products.filter((asset) => asset.weight > 0));
  const result = new Map<string, { amount: number; units: number; residual: number }>();
  for (const item of allocation.allocations) {
    const product = products.find((candidate) => candidate.id === item.id)!;
    result.set(item.id, { amount: item.allocatedAmount, units: item.units, residual: 0 });
    if (!product.fractionalSupported && item.units === 0) result.set(item.id, { amount: 0, units: 0, residual: capital * product.weight });
  }
  return result;
}

export interface PortfolioPlan {
  readonly rows: readonly AllocationRow[];
  readonly rawTotal: number;
  readonly publicTotal: number;
  readonly rawReasons: readonly string[];
}

export function createPortfolioPlan(result: ComputeFmResult, capital: number, baseCurrency: Currency, assets: readonly AssetFixture[] = ASSETS): PortfolioPlan {
  const raw = result.weights.weights;
  const safe = publicWeights(raw, assets);
  const rawAllocations = allocationFor(raw, capital, baseCurrency, assets);
  const publicAllocations = allocationFor(safe, capital, baseCurrency, assets);
  const rows = assets.map((asset, index) => {
    const rawWeight = raw[index] ?? 0;
    const publicWeight = safe[index] ?? 0;
    const rawAllocation = rawAllocations.get(asset.id) ?? { amount: 0, units: 0, residual: 0 };
    const publicAllocation = publicAllocations.get(asset.id) ?? { amount: 0, units: 0, residual: 0 };
    const reasons: string[] = [];
    if (asset.proxy) reasons.push(`经济暴露映射到可交易代理 ${asset.proxy}`);
    if (asset.freshness === "stale") reasons.push("市场数据过期：Public Default 禁止配置");
    if (asset.liquidity === "low") reasons.push("流动性不足：Public Default 禁止配置");
    if (asset.volatility === "high" && publicWeight < rawWeight) reasons.push("高波动类别/单项 25% 上限");
    if (publicAllocation.residual > 0) reasons.push("资金太小，无法满足最小交易单位；余量回到现金等价物");
    return { asset, rawWeight, publicWeight, rawAmount: rawAllocation.amount, publicAmount: publicAllocation.amount, rawUnits: rawAllocation.units, publicUnits: publicAllocation.units, rawResidual: rawAllocation.residual, publicResidual: publicAllocation.residual, reasons };
  });
  return { rows, rawTotal: raw.reduce((sum, value) => sum + value, 0), publicTotal: safe.reduce((sum, value) => sum + value, 0), rawReasons: result.reasons.map((reason) => reason.replaceAll("_", " ")) };
}

export function formatMoney(value: number, currency: Currency): string {
  return new Intl.NumberFormat(currency === "CNY" ? "zh-CN" : "en-US", { style: "currency", currency, maximumFractionDigits: 0 }).format(value);
}

export function modeWeight(row: AllocationRow, mode: PortfolioMode): number { return mode === "raw" ? row.rawWeight : row.publicWeight; }
export const portfolioMetadata = { method: "FM-v1 / Public-Default-v1", dataAsOf: MARKET_DATA_AS_OF, navLabel: "mock scenario NAV; not a return forecast" } as const;
