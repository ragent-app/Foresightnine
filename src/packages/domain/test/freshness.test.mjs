import assert from "node:assert/strict";
import test from "node:test";
import { DEFAULT_FRESHNESS_POLICY, evaluateFreshness } from "../dist/index.js";

const runtimeAsOf = "2026-08-10T00:00:00Z";
const atThreshold = new Date(Date.parse(runtimeAsOf) - DEFAULT_FRESHNESS_POLICY.maxAgeMs).toISOString();

test("fresh data and the exact inclusive threshold are current", () => {
  assert.equal(evaluateFreshness({ dataAsOf: "2026-08-09", runtimeAsOf, providerStatus: "fresh" }).status, "fresh");
  const decision = evaluateFreshness({ dataAsOf: atThreshold, runtimeAsOf, providerStatus: "fresh" });
  assert.equal(decision.status, "fresh");
  assert.equal(decision.allowCurrentPublicDefault, true);
  assert.equal(decision.ageMs, DEFAULT_FRESHNESS_POLICY.maxAgeMs);
});

test("stale and historical-only data remain displayable only as historical/simulated", () => {
  const stale = evaluateFreshness({ dataAsOf: "2026-08-08", runtimeAsOf, providerStatus: "fresh" });
  const historical = evaluateFreshness({ dataAsOf: "2026-08-09", runtimeAsOf, providerStatus: "fresh", declaredFreshness: "historical_only" });
  for (const decision of [stale, historical]) {
    assert.equal(decision.historicalOnly, true);
    assert.equal(decision.allowHistoricalDisplay, true);
    assert.equal(decision.allowCurrentPublicDefault, false);
    assert.equal(decision.label, "historical_only");
  }
  assert.equal(stale.status, "stale");
  assert.equal(historical.status, "historical_only");
});

test("provider stale and unavailable fail closed even when the timestamp is fresh", () => {
  assert.equal(evaluateFreshness({ dataAsOf: "2026-08-09", runtimeAsOf, providerStatus: "stale" }).status, "provider_stale");
  assert.equal(evaluateFreshness({ dataAsOf: "2026-08-09", runtimeAsOf, providerStatus: "unavailable" }).status, "provider_unavailable");
  assert.equal(evaluateFreshness({ dataAsOf: "2026-08-09", runtimeAsOf, providerStatus: "unavailable" }).allowCurrentPublicDefault, false);
});

test("invalid and future dates never become current or executable", () => {
  for (const dataAsOf of ["not-a-date", "2026-02-30", "2026-02-30T00:00:00Z", "2026-08-11"]) {
    const decision = evaluateFreshness({ dataAsOf, runtimeAsOf, providerStatus: "fresh" });
    assert.equal(decision.allowCurrentPublicDefault, false);
    assert.equal(decision.historicalOnly, true);
  }
  assert.equal(evaluateFreshness({ dataAsOf: "2026-08-11", runtimeAsOf, providerStatus: "fresh" }).status, "future_date");
});
