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
const extraction = spawnSync("uv", ["run", "--with", "pyarrow", "python", extractor, "--source", source], {
  cwd: workspace,
  encoding: "utf8",
  timeout: 180_000,
});
if (extraction.status !== 0) throw new Error(`warm-up extraction failed: ${extraction.stderr.trim() || extraction.stdout.trim()}`);
const extracted = JSON.parse(extraction.stdout);
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

const output = {
  source_sha256: extracted.source.sha256,
  cutoff: extracted.source.cutoff,
  assets: extracted.asset_ids,
  selected_factor_ids: result.selectedFactorIds,
  factor_metadata: { momentum_20: momentumMetadata, reversal_5: reversalMetadata },
  weights: Object.fromEntries(extracted.asset_ids.map((asset, index) => [asset, result.weights.weights[index]])),
  client_result_hash: createHash("sha256").update(JSON.stringify({ cutoff: result.cutoff, factors: result.selectedFactorIds, weights: Array.from(result.weights.weights) })).digest("hex"),
  synthetic_online_segment_used: false,
};
console.log(JSON.stringify(output, null, 2));
