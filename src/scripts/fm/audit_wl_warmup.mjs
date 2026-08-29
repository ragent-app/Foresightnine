/**
 * Runs the shared TypeScript FM engine against the real, cutoff-safe warm-up
 * portion of WL1.  It does not consume the panel's synthetic online segment.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { evaluateFactorAst, computeFm, icMean, icir, informationCoefficient } from "../../packages/fm-engine/dist/index.js";

const workspace = resolve(import.meta.dirname, "..", "..");
const source = process.argv[2] ?? "D:\\WL-data-final\\panels\\WL1_full.parquet";
const extractor = resolve(workspace, "scripts", "fm", "extract_wl_warmup.py");
const pythonOracle = resolve(workspace, "scripts", "fm", "audit_wl_warmup_oracle.py");
const extraction = spawnSync("uv", ["run", "--with", "pyarrow", "python", extractor, "--source", source], {
  cwd: workspace,
  encoding: "utf8",
  timeout: 180_000,
});
if (extraction.status !== 0) throw new Error(`warm-up extraction failed: ${extraction.stderr.trim() || extraction.stdout.trim()}`);
const extracted = JSON.parse(extraction.stdout);
const oracleRun = spawnSync("uv", ["run", "python", pythonOracle], {
  cwd: workspace,
  input: extraction.stdout,
  encoding: "utf8",
  timeout: 180_000,
});
if (oracleRun.status !== 0) throw new Error(`independent Python oracle failed: ${oracleRun.stderr.trim() || oracleRun.stdout.trim()}`);
const pythonOracleResult = JSON.parse(oracleRun.stdout);
const observations = extracted.observations;
const forwardReturns = extracted.forwardReturns;
const assetWidth = extracted.asset_ids.length;

function transpose(rows) {
  return Array.from({ length: rows[0].length }, (_, asset) => Float64Array.from(rows, (row) => row[asset]));
}

function metadata(expression) {
  const signalByTime = evaluateFactorAst(expression, observations);
  const returnsByTime = forwardReturns.map((row) => Float64Array.from(row.values, (value) => value ?? Number.NaN));
  const series = informationCoefficient(transpose(signalByTime), transpose(returnsByTime));
  return { icMean: icMean(series), icir: icir(series) };
}

function assertVectorParity(label, actual, expected, tolerance = 1e-10) {
  assert.equal(actual.length, expected.length, `${label} length`);
  let maximum = 0;
  for (let index = 0; index < actual.length; index += 1) {
    const left = actual[index];
    const right = expected[index];
    if (right === null) {
      assert.ok(!Number.isFinite(left), `${label}[${index}] should be non-finite`);
      continue;
    }
    assert.ok(Number.isFinite(left), `${label}[${index}] should be finite`);
    const difference = Math.abs(left - right);
    maximum = Math.max(maximum, difference);
    assert.ok(difference <= tolerance, `${label}[${index}] differed by ${difference}`);
  }
  return maximum;
}

const momentum = { op: "field", name: "momentum_20" };
const reversal = { op: "field", name: "reversal_5" };
const momentumMetadata = metadata(momentum);
const reversalMetadata = metadata(reversal);
assert.ok(Number.isFinite(momentumMetadata.icMean));
assert.ok(Number.isFinite(momentumMetadata.icir));
assert.ok(Number.isFinite(reversalMetadata.icMean));
assert.ok(Number.isFinite(reversalMetadata.icir));

const result = computeFm({
  cutoff: extracted.source.cutoff,
  observations,
  forwardReturns,
  factors: [
    { id: 101, admitted: true, expression: momentum, ...momentumMetadata },
    { id: 102, admitted: true, expression: reversal, ...reversalMetadata },
  ],
  currentValues: new Float64Array(assetWidth).fill(100),
  initialAllocation: true,
  costBps: 3,
});

assert.equal(extracted.source.synthetic_online_segment_used, false);
assert.equal(result.cutoff, extracted.source.cutoff);
assert.equal(result.weights.weights.length, assetWidth);
assert.ok(result.selectedFactorIds.length > 0);
assert.ok(Math.abs(Array.from(result.weights.weights).reduce((sum, value) => sum + value, 0) - 1) < 1e-12);
assert.equal(result.rebalance?.cost, 0);
assert.ok(result.reasons.includes("initial_allocation_free"));

assert.equal(pythonOracleResult.oracle, "independent_python");
assert.equal(pythonOracleResult.cutoff, extracted.source.cutoff);
assert.equal(pythonOracleResult.cutoff_safe, true);
assert.equal(pythonOracleResult.synthetic_online_segment_used, false);
assert.deepEqual(pythonOracleResult.asset_ids, extracted.asset_ids);
assert.deepEqual(pythonOracleResult.selected_factor_ids, result.selectedFactorIds);
const parityDifferences = [
  Math.abs(momentumMetadata.icMean - pythonOracleResult.factor_metadata.momentum_20.icMean),
  Math.abs(momentumMetadata.icir - pythonOracleResult.factor_metadata.momentum_20.icir),
  Math.abs(reversalMetadata.icMean - pythonOracleResult.factor_metadata.reversal_5.icMean),
  Math.abs(reversalMetadata.icir - pythonOracleResult.factor_metadata.reversal_5.icir),
  assertVectorParity("combined", result.combination.combined, pythonOracleResult.combination.combined),
  assertVectorParity("weights", result.weights.weights, pythonOracleResult.weights.weights),
  assertVectorParity("forecasts", result.forecasts, pythonOracleResult.forecasts),
  Math.abs(result.forecastScale.returnScale - pythonOracleResult.forecast_scale.return_scale),
  assertVectorParity("metrics.icSeries", result.metrics?.icSeries ?? new Float64Array(), pythonOracleResult.metrics.icSeries),
  Math.abs((result.metrics?.icMean ?? 0) - pythonOracleResult.metrics.icMean),
  Math.abs((result.metrics?.icir ?? 0) - pythonOracleResult.metrics.icir),
  Math.abs((result.rebalance?.postTradeNav ?? 0) - pythonOracleResult.rebalance.postTradeNav),
  Math.abs((result.rebalance?.transferred ?? 0) - pythonOracleResult.rebalance.transferred),
  Math.abs((result.rebalance?.cost ?? 0) - pythonOracleResult.rebalance.cost),
  Math.abs((result.rebalance?.turnover ?? 0) - pythonOracleResult.rebalance.turnover),
];
assert.ok(Math.max(...parityDifferences) <= 1e-10);

const output = {
  source_sha256: extracted.source.sha256,
  cutoff: extracted.source.cutoff,
  assets: extracted.asset_ids,
  selected_factor_ids: result.selectedFactorIds,
  factor_metadata: { momentum_20: momentumMetadata, reversal_5: reversalMetadata },
  weights: Object.fromEntries(extracted.asset_ids.map((asset, index) => [asset, result.weights.weights[index]])),
  python_oracle: {
    implementation: pythonOracleResult.oracle,
    status: "passed",
    cutoff_safe: pythonOracleResult.cutoff_safe,
    max_abs_difference: Math.max(...parityDifferences),
  },
  client_result_hash: createHash("sha256").update(JSON.stringify({ cutoff: result.cutoff, factors: result.selectedFactorIds, weights: Array.from(result.weights.weights) })).digest("hex"),
  synthetic_online_segment_used: false,
};
console.log(JSON.stringify(output, null, 2));
