import {
  assetTransferRebalance,
  combineLastSignals,
  forecastScale,
  forecasts,
  icMean,
  icir,
  informationCoefficient,
  rawLongOnlyWeights,
  selectFactors,
  shouldRebalance,
  type CombinationResult,
  type FactorDescriptor,
  type ForecastScale,
  type RebalanceResult,
  type WeightResult,
} from "./index.js";
import { evaluateFactorAst, type FactorAst, type FactorObservation } from "./ast.js";

export interface FactorExpression {
  readonly id: number;
  readonly icMean: number;
  readonly icir: number;
  readonly admitted: boolean;
  readonly expression: FactorAst;
}

export interface ComputeFmInput {
  readonly cutoff: string;
  readonly observations: readonly FactorObservation[];
  readonly factors: readonly FactorExpression[];
  readonly maxFactors?: number;
  readonly forecastHorizon?: number;
  readonly forwardReturns?: readonly (readonly (number | null)[])[];
  readonly currentValues?: Float64Array;
  readonly initialAllocation?: boolean;
  readonly costBps?: number;
}

export interface ComputeFmResult {
  readonly cutoff: string;
  readonly selectedFactorIds: readonly number[];
  readonly factorSignalsByTime: ReadonlyMap<number, readonly Float64Array[]>;
  readonly lastFactorDescriptors: readonly FactorDescriptor[];
  readonly combination: CombinationResult;
  readonly weights: WeightResult;
  readonly forecastScale: ForecastScale;
  readonly forecasts: Float64Array;
  readonly metrics: { readonly icSeries: Float64Array; readonly icMean: number; readonly icir: number } | null;
  readonly rebalance: RebalanceResult | null;
  readonly deadband: ReturnType<typeof shouldRebalance> | null;
  readonly reasons: readonly ("no_valid_factors" | "single_factor_fallback" | "equal_weight_fallback" | "initial_allocation_free" | "no_current_portfolio")[];
  readonly rawClientResult: {
    readonly targetWeights: Float64Array;
    readonly forecastReturns: Float64Array;
    readonly transactionCost: number;
    readonly reasons: readonly string[];
  };
}

function assertCutoff(cutoff: string, asOf: string): void {
  const cutoffTime = Date.parse(cutoff);
  const asOfTime = Date.parse(asOf);
  if (!Number.isFinite(cutoffTime) || !Number.isFinite(asOfTime)) throw new RangeError("cutoff and as_of must be parseable dates");
  if (asOfTime > cutoffTime) throw new RangeError(`future_data_rejected: ${asOf} is after cutoff ${cutoff}`);
}

function transpose(rows: readonly Float64Array[]): readonly Float64Array[] {
  if (rows.length === 0) return [];
  return Array.from({ length: rows[0]!.length }, (_, asset) => Float64Array.from(rows, (row) => row[asset]!));
}

function returnsMatrix(forwardReturns: ComputeFmInput["forwardReturns"], order: readonly number[], width: number): readonly Float64Array[] | null {
  if (forwardReturns === undefined) return null;
  if (forwardReturns.length !== order.length) throw new RangeError("forwardReturns must align with observations");
  return order.map((originalIndex) => {
    const row = forwardReturns[originalIndex]!;
    if (row.length !== width) throw new RangeError("forwardReturns must share the asset width");
    return Float64Array.from(row, (value) => value === null || !Number.isFinite(value) ? Number.NaN : value);
  });
}

/** Pure end-to-end FM calculation: all data is caller-supplied and cutoff-checked. */
export function computeFm(input: ComputeFmInput): ComputeFmResult {
  if (input.observations.length === 0) throw new RangeError("observations must not be empty");
  assertCutoff(input.cutoff, input.observations[0]!.asOf);
  for (const observation of input.observations) assertCutoff(input.cutoff, observation.asOf);
  const indexed = input.observations.map((observation, index) => ({ observation, index }));
  indexed.sort((left, right) => Date.parse(left.observation.asOf) - Date.parse(right.observation.asOf));
  const ordered = indexed.map((entry) => entry.observation);
  const order = indexed.map((entry) => entry.index);
  const firstField = Object.values(ordered[0]!.fields)[0];
  if (firstField === undefined) throw new RangeError("observations need at least one field");
  const width = firstField.length;
  if (width === 0) throw new RangeError("observations need at least one asset");
  for (const row of ordered) for (const values of Object.values(row.fields)) if (values.length !== width) throw new RangeError("all observation fields must share the asset width");

  const factorSignalsByTime = new Map<number, readonly Float64Array[]>();
  const descriptors: FactorDescriptor[] = [];
  for (const factor of input.factors) {
    const signals = evaluateFactorAst(factor.expression, ordered);
    factorSignalsByTime.set(factor.id, signals);
    descriptors.push({ id: factor.id, icMean: factor.icMean, icir: factor.icir, admitted: factor.admitted, signal: signals[signals.length - 1]! });
  }
  const selected = selectFactors(descriptors, input.maxFactors ?? 10);
  const combination = combineLastSignals(selected);
  const weights = rawLongOnlyWeights(combination.combined);
  const closeRows = ordered.map((row) => {
    const close = row.fields.close;
    return close === undefined ? new Float64Array() : Float64Array.from(close, (value) => value === null || !Number.isFinite(value) ? Number.NaN : value);
  });
  const scale = closeRows[0]!.length === width && closeRows.length > (input.forecastHorizon ?? 1)
    ? forecastScale(closeRows, input.forecastHorizon ?? 1)
    : { crossSectionalVolatility: new Float64Array(), returnScale: 0.01 };
  const forecast = forecasts(combination.combined, selected, scale.returnScale);
  const returns = returnsMatrix(input.forwardReturns, order, width);
  let metrics: ComputeFmResult["metrics"] = null;
  if (returns !== null && selected.length > 0) {
    const combinedSeries = selected.length === 0 ? [] : Array.from({ length: ordered.length }, (_, time) => {
      const signals = selected.map((factor) => factorSignalsByTime.get(factor.id)![time]!);
      return signals.length === 0 ? new Float64Array(width).fill(Number.NaN) : combineLastSignals(signals.map((signal, index) => ({ id: index, icMean: selected[index]!.icMean, icir: selected[index]!.icir, admitted: true, signal }))).combined;
    });
    const icSeries = informationCoefficient(transpose(combinedSeries), transpose(returns));
    metrics = { icSeries, icMean: icMean(icSeries), icir: icir(icSeries) };
  }
  let deadband: ReturnType<typeof shouldRebalance> | null = null;
  let rebalance: RebalanceResult | null = null;
  const reasons: Array<"no_valid_factors" | "single_factor_fallback" | "equal_weight_fallback" | "initial_allocation_free" | "no_current_portfolio"> = [];
  if (selected.length === 0 || combination.usedFactorIds.length === 0) reasons.push("no_valid_factors");
  if (combination.usedFactorIds.length === 1) reasons.push("single_factor_fallback");
  if (weights.fallback) reasons.push("equal_weight_fallback");
  if (input.currentValues === undefined) reasons.push("no_current_portfolio");
  else {
    if (input.currentValues.length !== width) throw new RangeError("currentValues must share the asset width");
    const nav = input.currentValues.reduce((total, value) => total + value, 0);
    if (!Number.isFinite(nav) || nav <= 0 || Array.from(input.currentValues).some((value) => !Number.isFinite(value) || value < 0)) throw new RangeError("currentValues must be finite, non-negative, and have positive NAV");
    const initialAllocation = input.initialAllocation ?? false;
    const currentWeights = Float64Array.from(input.currentValues, (value) => value / nav);
    deadband = shouldRebalance(currentWeights, weights.weights, forecast, input.costBps ?? 3);
    if (initialAllocation) {
      reasons.push("initial_allocation_free");
      rebalance = assetTransferRebalance(input.currentValues, weights.weights, nav, input.costBps ?? 3, true);
    } else if (deadband.execute) {
      rebalance = assetTransferRebalance(input.currentValues, weights.weights, nav, input.costBps ?? 3, false);
    }
  }
  const rawReasons = reasons.map((reason) => reason as string);
  return {
    cutoff: input.cutoff,
    selectedFactorIds: selected.map((factor) => factor.id),
    factorSignalsByTime,
    lastFactorDescriptors: selected,
    combination,
    weights,
    forecastScale: scale,
    forecasts: forecast,
    metrics,
    rebalance,
    deadband,
    reasons,
    rawClientResult: { targetWeights: weights.weights, forecastReturns: forecast, transactionCost: rebalance?.cost ?? 0, reasons: rawReasons },
  };
}
