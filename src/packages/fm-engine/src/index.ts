export interface FactorDescriptor {
  readonly id: number;
  readonly icMean: number;
  readonly icir: number;
  readonly admitted: boolean;
  readonly signal: Float64Array;
}

export interface CombinationResult {
  readonly combined: Float64Array;
  readonly supportWeight: Float64Array;
  readonly contributions: ReadonlyMap<number, Float64Array>;
  readonly qualityWeights: ReadonlyMap<number, number>;
  readonly usedFactorIds: readonly number[];
}

export function toFloat64(values: readonly (number | null)[]): Float64Array {
  return Float64Array.from(values, (value) => value === null ? Number.NaN : value);
}

function finite(value: number): boolean { return Number.isFinite(value); }
function mean(values: readonly number[]): number { return values.reduce((total, value) => total + value, 0) / values.length; }
function std0(values: readonly number[]): number {
  if (values.length === 0) return Number.NaN;
  const average = mean(values);
  return Math.sqrt(mean(values.map((value) => (value - average) ** 2)));
}

/** pandas.Series.rank(pct=True), average tie method, with invalid inputs left as NaN. */
export function pandasPercentRank(values: Float64Array): Float64Array {
  const result = new Float64Array(values.length).fill(Number.NaN);
  const entries: Array<{ index: number; value: number }> = [];
  for (let index = 0; index < values.length; index += 1) if (finite(values[index]!)) entries.push({ index, value: values[index]! });
  entries.sort((left, right) => left.value - right.value || left.index - right.index);
  for (let start = 0; start < entries.length;) {
    let end = start + 1;
    while (end < entries.length && entries[end]!.value === entries[start]!.value) end += 1;
    const percentile = ((start + 1 + end) / 2) / entries.length;
    for (let cursor = start; cursor < end; cursor += 1) result[entries[cursor]!.index] = percentile;
    start = end;
  }
  return result;
}

/** Admission is supplied by factor-library metadata; this function never hardcodes thresholds. */
export function selectFactors(factors: readonly FactorDescriptor[], maximum = 10): readonly FactorDescriptor[] {
  if (!Number.isInteger(maximum) || maximum < 1) throw new RangeError("maximum must be a positive integer");
  // Non-finite quality metadata cannot establish admission.  Skip it before
  // truncation so an invalid factor cannot consume the maxFactors boundary.
  return factors.filter((factor) => factor.admitted && finite(factor.icMean) && finite(factor.icir)).slice().sort((left, right) => {
    const leftQuality = Math.abs(left.icMean) * Math.abs(left.icir);
    const rightQuality = Math.abs(right.icMean) * Math.abs(right.icir);
    // Python's ``sorted(..., reverse=True)`` is stable: equal-quality factors
    // retain library/input order.  Do not use factor IDs as a hidden tie-breaker.
    return rightQuality - leftQuality;
  }).slice(0, maximum);
}

export function combineLastSignals(factors: readonly FactorDescriptor[]): CombinationResult {
  if (factors.length === 0) return { combined: new Float64Array(), supportWeight: new Float64Array(), contributions: new Map(), qualityWeights: new Map(), usedFactorIds: [] };
  const size = factors[0]!.signal.length;
  if (factors.some((factor) => factor.signal.length !== size)) throw new RangeError("all factor signals must share an asset shape");
  const combined = new Float64Array(size);
  const supportWeight = new Float64Array(size);
  const contributions = new Map<number, Float64Array>();
  const qualityWeights = new Map<number, number>();
  const usedFactorIds: number[] = [];
  for (const factor of factors) {
    if (!finite(factor.icMean) || !finite(factor.icir)) continue;
    let valid = 0;
    for (const value of factor.signal) if (finite(value)) valid += 1;
    if (valid < 2) continue;
    const rank = pandasPercentRank(factor.signal);
    const direction = factor.icMean >= 0 ? 1 : -1;
    const contribution = new Float64Array(size).fill(Number.NaN);
    const quality = Math.max(Math.abs(factor.icMean), 1e-6) * Math.max(Math.abs(factor.icir), 1e-6);
    for (let index = 0; index < size; index += 1) {
      if (!finite(rank[index]!)) continue;
      contribution[index] = direction * (rank[index]! - 0.5);
      combined[index] = combined[index]! + contribution[index]! * quality;
      supportWeight[index] = supportWeight[index]! + quality;
    }
    contributions.set(factor.id, contribution);
    qualityWeights.set(factor.id, quality);
    usedFactorIds.push(factor.id);
  }
  for (let index = 0; index < size; index += 1) combined[index] = supportWeight[index]! > 0 ? combined[index]! / supportWeight[index]! : Number.NaN;
  return { combined, supportWeight, contributions, qualityWeights, usedFactorIds };
}

export interface WeightResult { readonly z: Float64Array; readonly weights: Float64Array; readonly fallback: boolean; }
export function rawLongOnlyWeights(combined: Float64Array, kappa = 1): WeightResult {
  if (!finite(kappa)) throw new RangeError("kappa must be finite");
  const eligible: number[] = [];
  for (let index = 0; index < combined.length; index += 1) if (finite(combined[index]!)) eligible.push(index);
  const equal = (z = new Float64Array(combined.length).fill(Number.NaN)): WeightResult => ({ z, weights: Float64Array.from({ length: combined.length }, () => combined.length === 0 ? 0 : 1 / combined.length), fallback: true });
  if (combined.length === 0 || eligible.length < 2) return equal();
  const values = eligible.map((index) => combined[index]!);
  const average = mean(values);
  const deviation = std0(values.map((value) => value - average));
  if (!finite(deviation) || deviation <= 1e-12) return equal();
  const z = new Float64Array(combined.length).fill(Number.NaN);
  const weights = new Float64Array(combined.length);
  for (const index of eligible) {
    z[index] = (combined[index]! - average) / deviation;
    weights[index] = Math.max(0, (1 + kappa * z[index]!) / eligible.length);
  }
  let total = 0;
  for (const weight of weights) total += weight;
  if (total <= 1e-12) return equal(z);
  for (let index = 0; index < weights.length; index += 1) weights[index] = weights[index]! / total;
  return { z, weights, fallback: false };
}

export interface ForecastScale { readonly crossSectionalVolatility: Float64Array; readonly returnScale: number; }
export function forecastScale(closes: readonly Float64Array[], horizon: number): ForecastScale {
  if (!Number.isInteger(horizon) || horizon < 1 || closes.length <= horizon) throw new RangeError("horizon requires at least one forward row");
  const width = closes[0]!.length;
  if (closes.some((row) => row.length !== width)) throw new RangeError("close rows must share an asset shape");
  const volatility = new Float64Array(closes.length - horizon).fill(Number.NaN);
  const validVolatility: number[] = [];
  for (let row = horizon; row < closes.length; row += 1) {
    const returns: number[] = [];
    for (let asset = 0; asset < width; asset += 1) {
      const prior = closes[row - horizon]![asset]!;
      const current = closes[row]![asset]!;
      if (finite(prior) && finite(current) && prior !== 0) returns.push(current / prior - 1);
    }
    const value = std0(returns);
    volatility[row - horizon] = value;
    if (finite(value)) validVolatility.push(value);
  }
  const recent = validVolatility.slice(-252).sort((left, right) => left - right);
  let scale = recent.length === 0 ? 0.01 : (recent.length % 2 === 1 ? recent[(recent.length - 1) / 2]! : (recent[recent.length / 2 - 1]! + recent[recent.length / 2]!) / 2);
  if (!finite(scale) || scale <= 0) scale = 0.01;
  return { crossSectionalVolatility: volatility, returnScale: scale };
}

export function forecasts(combined: Float64Array, usedFactors: readonly FactorDescriptor[], scale: number): Float64Array {
  const eligible = Array.from(combined).filter(finite);
  const result = new Float64Array(combined.length).fill(Number.NaN);
  if (eligible.length < 2 || !finite(scale)) return result;
  const average = mean(eligible);
  const deviation = std0(eligible.map((value) => value - average));
  if (!finite(deviation) || deviation <= 1e-12) return result;
  const usable = usedFactors.filter((factor) => finite(factor.icMean));
  const meanAbsIc = usable.length === 0 ? 0 : mean(usable.map((factor) => Math.abs(factor.icMean)));
  for (let index = 0; index < combined.length; index += 1) if (finite(combined[index]!)) result[index] = ((combined[index]! - average) / deviation) * meanAbsIc * scale;
  return result;
}

function rankAverage(values: readonly number[]): number[] {
  const entries = values.map((value, index) => ({ value, index })).sort((left, right) => left.value - right.value || left.index - right.index);
  const result = new Array<number>(values.length);
  for (let start = 0; start < entries.length;) {
    let end = start + 1; while (end < entries.length && entries[end]!.value === entries[start]!.value) end += 1;
    const rank = (start + 1 + end) / 2; for (let cursor = start; cursor < end; cursor += 1) result[entries[cursor]!.index] = rank;
    start = end;
  }
  return result;
}
export function informationCoefficient(signals: readonly Float64Array[], returns: readonly Float64Array[]): Float64Array {
  if (signals.length !== returns.length || signals.length === 0 || signals[0]!.length !== returns[0]!.length) throw new RangeError("matrices must share asset-by-time shape");
  const times = signals[0]!.length; const output = new Float64Array(times).fill(Number.NaN);
  for (const row of signals) if (row.length !== times) throw new RangeError("signal shape mismatch");
  for (const row of returns) if (row.length !== times) throw new RangeError("return shape mismatch");
  for (let time = 0; time < times; time += 1) {
    const x: number[] = []; const y: number[] = [];
    for (let asset = 0; asset < signals.length; asset += 1) { const a = signals[asset]![time]!; const b = returns[asset]![time]!; if (finite(a) && finite(b)) { x.push(a); y.push(b); } }
    if (x.length < 5) continue;
    const rx = rankAverage(x); const ry = rankAverage(y); const ax = mean(rx); const ay = mean(ry);
    let numerator = 0; let left = 0; let right = 0;
    for (let index = 0; index < x.length; index += 1) { const dx = rx[index]! - ax; const dy = ry[index]! - ay; numerator += dx * dy; left += dx * dx; right += dy * dy; }
    output[time] = Math.sqrt(left * right) < 1e-12 ? 0 : numerator / Math.sqrt(left * right);
  }
  return output;
}
export function icMean(series: Float64Array): number { const valid = Array.from(series).filter(finite); return valid.length === 0 ? 0 : mean(valid); }
export function icir(series: Float64Array): number {
  const valid = Array.from(series).filter(finite); if (valid.length < 3) return 0;
  const average = mean(valid); const sampleStd = Math.sqrt(valid.reduce((total, value) => total + (value - average) ** 2, 0) / (valid.length - 1));
  return sampleStd < 1e-12 ? 0 : average / sampleStd;
}

export interface RebalanceResult { readonly postTradeNav: number; readonly transferred: number; readonly cost: number; readonly turnover: number; }
export function assetTransferRebalance(currentValues: Float64Array, targetWeights: Float64Array, preTradeNav: number, costBps = 3, initialAllocation = false): RebalanceResult {
  if (currentValues.length !== targetWeights.length || preTradeNav <= 0 || !finite(preTradeNav)) throw new RangeError("invalid rebalance inputs");
  if (initialAllocation) return { postTradeNav: preTradeNav, transferred: preTradeNav, cost: 0, turnover: 0 };
  let post = preTradeNav; let transferred = 0; let cost = 0; const rate = costBps / 10_000;
  for (let iteration = 0; iteration < 100; iteration += 1) {
    transferred = 0; for (let index = 0; index < currentValues.length; index += 1) transferred += Math.max(currentValues[index]! - post * targetWeights[index]!, 0);
    cost = transferred * rate; const updated = preTradeNav - cost;
    if (Math.abs(updated - post) <= Math.max(1e-9, preTradeNav * 1e-12)) { post = updated; break; }
    post = updated;
  }
  return { postTradeNav: post, transferred, cost, turnover: transferred / preTradeNav };
}

export interface DeadbandResult { readonly execute: boolean; readonly expectedGain: number; readonly cost: number; readonly reason: "initial_or_unknown_forecast" | "target_unchanged" | "below_cost_deadband" | "clears_cost_deadband"; }
export function shouldRebalance(current: Float64Array, target: Float64Array, forecast?: Float64Array, costBps = 3): DeadbandResult {
  if (current.length !== target.length || (forecast !== undefined && forecast.length !== 0 && forecast.length !== target.length)) throw new RangeError("weight shape mismatch");
  let delta = 0; let migration = 0; for (let index = 0; index < current.length; index += 1) { delta += Math.abs(target[index]! - current[index]!); migration += Math.max(current[index]! - target[index]!, 0); }
  if (delta <= 1e-10) return { execute: false, expectedGain: 0, cost: 0, reason: "target_unchanged" };
  const hasUsable = forecast !== undefined && Array.from(forecast).some((value) => finite(value) && Math.abs(value) > 0);
  if (!hasUsable) return { execute: true, expectedGain: 0, cost: 0, reason: "initial_or_unknown_forecast" };
  let gain = 0; for (let index = 0; index < current.length; index += 1) gain += (target[index]! - current[index]!) * (finite(forecast![index]!) ? forecast![index]! : 0);
  const cost = costBps / 10_000 * migration;
  return gain <= cost ? { execute: false, expectedGain: gain, cost, reason: "below_cost_deadband" } : { execute: true, expectedGain: gain, cost, reason: "clears_cost_deadband" };
}

export interface ProductAsset { readonly id: string; readonly weight: number; readonly price: number; readonly fxToBase: number; readonly lotSize: number; readonly fractionalSupported: boolean; }
export interface ProductAllocation { readonly id: string; readonly units: number; readonly allocatedAmount: number; }
export function allocateCapital(capital: number, assets: readonly ProductAsset[]): { readonly allocations: readonly ProductAllocation[]; readonly cashRemainder: number } {
  if (!finite(capital) || capital <= 0) throw new RangeError("capital must be positive");
  let allocated = 0;
  const allocations = assets.map((asset) => {
    if (!finite(asset.weight) || asset.weight <= 0 || !finite(asset.price) || asset.price <= 0 || !finite(asset.fxToBase) || asset.fxToBase <= 0 || !finite(asset.lotSize) || asset.lotSize <= 0) throw new RangeError(`invalid asset ${asset.id}`);
    const desiredUnits = capital * asset.weight / (asset.price * asset.fxToBase);
    const units = asset.fractionalSupported ? desiredUnits : Math.floor(desiredUnits / asset.lotSize) * asset.lotSize;
    const allocatedAmount = units * asset.price * asset.fxToBase; allocated += allocatedAmount;
    return { id: asset.id, units, allocatedAmount };
  });
  return { allocations, cashRemainder: capital - allocated };
}

export type CanonicalJson = null | boolean | number | string | readonly CanonicalJson[] | { readonly [key: string]: CanonicalJson };
export function canonicalJson(value: CanonicalJson): string {
  if (value === null || typeof value === "boolean") return String(value);
  if (typeof value === "number") { if (!finite(value)) throw new TypeError("canonical JSON forbids non-finite numbers"); return Object.is(value, -0) ? "0" : String(value); }
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const record = value as { readonly [key: string]: CanonicalJson };
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key]!)}`).join(",")}}`;
}
function utf8(text: string): number[] { const bytes: number[] = []; for (let i = 0; i < text.length; i += 1) { let c = text.charCodeAt(i); if (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length) { const low = text.charCodeAt(i + 1); if (low >= 0xdc00 && low <= 0xdfff) { c = 0x10000 + ((c - 0xd800) << 10) + low - 0xdc00; i += 1; } } if (c <= 0x7f) bytes.push(c); else if (c <= 0x7ff) bytes.push(0xc0 | c >> 6, 0x80 | c & 63); else if (c <= 0xffff) bytes.push(0xe0 | c >> 12, 0x80 | c >> 6 & 63, 0x80 | c & 63); else bytes.push(0xf0 | c >> 18, 0x80 | c >> 12 & 63, 0x80 | c >> 6 & 63, 0x80 | c & 63); } return bytes; }
export function sha256Hex(text: string): string {
  const bytes = utf8(text); const bitLength = bytes.length * 8; bytes.push(0x80); while (bytes.length % 64 !== 56) bytes.push(0); for (let shift = 56; shift >= 0; shift -= 8) bytes.push(Math.floor(bitLength / 2 ** shift) & 255);
  const k = [1116352408,1899447441,3049323471,3921009573,961987163,1508970993,2453635748,2870763221,3624381080,310598401,607225278,1426881987,1925078388,2162078206,2614888103,3248222580,3835390401,4022224774,264347078,604807628,770255983,1249150122,1555081692,1996064986,2554220882,2821834349,2952996808,3210313671,3336571891,3584528711,113926993,338241895,666307205,773529912,1294757372,1396182291,1695183700,1986661051,2177026350,2456956037,2730485921,2820302411,3259730800,3345764771,3516065817,3600352804,4094571909,275423344,430227734,506948616,659060556,883997877,958139571,1322822218,1537002063,1747873779,1955562222,2024104815,2227730452,2361852424,2428436474,2756734187,3204031479,3329325298];
  let h0=1779033703,h1=3144134277,h2=1013904242,h3=2773480762,h4=1359893119,h5=2600822924,h6=528734635,h7=1541459225; const rotr=(x:number,n:number)=>(x>>>n)|(x<<(32-n));
  for (let offset=0; offset<bytes.length; offset+=64) { const w=new Uint32Array(64); for(let i=0;i<16;i+=1) w[i]=(bytes[offset+i*4]!<<24)|(bytes[offset+i*4+1]!<<16)|(bytes[offset+i*4+2]!<<8)|bytes[offset+i*4+3]!; for(let i=16;i<64;i+=1){const a=rotr(w[i-15]!,7)^rotr(w[i-15]!,18)^(w[i-15]!>>>3),b=rotr(w[i-2]!,17)^rotr(w[i-2]!,19)^(w[i-2]!>>>10);w[i]=(((w[i-16]!+a)>>>0)+((w[i-7]!+b)>>>0))>>>0;} let a=h0,b=h1,c=h2,d=h3,e=h4,f=h5,g=h6,h=h7; for(let i=0;i<64;i+=1){const s1=rotr(e,6)^rotr(e,11)^rotr(e,25),ch=(e&f)^(~e&g),t1=(((h+s1)>>>0)+((ch+k[i]!)>>>0)+w[i]!)>>>0,s0=rotr(a,2)^rotr(a,13)^rotr(a,22),maj=(a&b)^(a&c)^(b&c),t2=(s0+maj)>>>0;h=g;g=f;f=e;e=(d+t1)>>>0;d=c;c=b;b=a;a=(t1+t2)>>>0;}h0=(h0+a)>>>0;h1=(h1+b)>>>0;h2=(h2+c)>>>0;h3=(h3+d)>>>0;h4=(h4+e)>>>0;h5=(h5+f)>>>0;h6=(h6+g)>>>0;h7=(h7+h)>>>0; }
  return [h0,h1,h2,h3,h4,h5,h6,h7].map((value)=>value.toString(16).padStart(8,"0")).join("");
}

export * from "./ast.js";
export * from "./orchestration.js";
export * from "./worker.js";
