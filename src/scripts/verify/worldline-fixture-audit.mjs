/**
 * Audit the 9WL regression fixture against its read-only narrative and news
 * sources.  It verifies provenance and temporal/probability semantics; it
 * does not turn the synthetic future paths into market facts or forecasts.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const workspace = resolve(fileURLToPath(new URL("../..", import.meta.url)));
const fixturePath = resolve(workspace, "packages", "test-fixtures", "worldlines", "v1", "9wl-regression.json");
const fixture = JSON.parse(await readFile(fixturePath, "utf8"));

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function iso(value) {
  assert.match(value, /^\d{4}-\d{2}-\d{2}$/);
  return value;
}

function productInterval(left, right) {
  return [left[0] * right[0], left[1] * right[1]];
}

const premise = fixture.probability_inputs.premise;
assert.equal(fixture.fixture_version, "v2");
assert.equal(fixture.worldlines.length, 9);
assert.equal(fixture.baseline_date, "2026-07-17");
assert.ok(premise.scalar >= premise.range[0] && premise.scalar <= premise.range[1]);

const audited = [];
for (const worldline of fixture.worldlines) {
  const [narrative, newsBytes] = await Promise.all([
    readFile(worldline.source.narrative.path),
    readFile(worldline.source.stage_news.path),
  ]);
  assert.equal(sha256(narrative), worldline.source.narrative.sha256, `${worldline.id}: narrative source changed`);
  assert.equal(sha256(newsBytes), worldline.source.stage_news.sha256, `${worldline.id}: news source changed`);

  const narrativeText = narrative.toString("utf8");
  assert.ok(narrativeText.includes(worldline.trigger), `${worldline.id}: fixture trigger is not present in the narrative`);
  const stageNews = JSON.parse(newsBytes.toString("utf8"));
  assert.ok(Array.isArray(stageNews) && stageNews.length >= 2, `${worldline.id}: stage-two news is unavailable`);
  const triggerNews = stageNews[1];
  assert.equal(triggerNews.news_date, worldline.news_date, `${worldline.id}: event reveal must use stage-two news`);

  iso(worldline.event_date);
  iso(worldline.news_date);
  iso(worldline.reveal_date);
  iso(worldline.horizon_end);
  assert.ok(fixture.baseline_date < worldline.event_date && worldline.event_date < worldline.horizon_end, `${worldline.id}: event must be inside the scenario horizon`);
  assert.equal(worldline.reveal_date, worldline.news_date, `${worldline.id}: reveal must be tied to a news-visible date`);
  if (worldline.rule === "leak") assert.ok(worldline.reveal_date < worldline.event_date, `${worldline.id}: leak reveal must precede event`);
  else {
    assert.equal(worldline.rule, "observed", `${worldline.id}: unknown reveal rule`);
    assert.ok(worldline.reveal_date >= worldline.event_date, `${worldline.id}: observed reveal cannot precede event`);
  }

  const conditionalMass = worldline.branches.reduce((sum, branch) => sum + branch.probability.conditional, 0);
  assert.ok(Math.abs(conditionalMass - 1) < 1e-12, `${worldline.id}: conditional branch mass must equal one`);
  for (const branch of worldline.branches) {
    assert.equal(branch.trigger, worldline.trigger, `${branch.id}: trigger diverges from source worldline`);
    assert.equal(branch.event_date, worldline.event_date, `${branch.id}: event date diverges from source worldline`);
    assert.equal(branch.reveal_date, worldline.reveal_date, `${branch.id}: reveal date diverges from source worldline`);
    assert.equal(branch.rule, worldline.rule, `${branch.id}: reveal rule diverges from source worldline`);
    const probability = branch.probability;
    assert.ok(probability.conditional >= probability.conditional_range[0] && probability.conditional <= probability.conditional_range[1], `${branch.id}: conditional point must lie in its interval`);
    assert.ok(Math.abs(probability.joint - premise.scalar * probability.conditional) < 1e-12, `${branch.id}: joint probability must be recomputable`);
    assert.deepEqual(probability.joint_range, productInterval(premise.range, probability.conditional_range), `${branch.id}: joint interval must be recomputable`);
  }
  audited.push({ id: worldline.id, rule: worldline.rule, event_date: worldline.event_date, reveal_date: worldline.reveal_date });
}

console.log(JSON.stringify({
  fixture: fixturePath,
  worldline_count: audited.length,
  baseline_date: fixture.baseline_date,
  audited,
  result: "pass",
  limitation: "Synthetic branch probabilities and market paths remain regression fixtures, not forecasts or historical facts.",
}, null, 2));
