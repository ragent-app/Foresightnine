import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  TRANSITIONS,
  assertAcyclicWorldline,
  assertProbabilityAssessment,
  assertWorldlineDates,
  canTransition,
  confirmWithReservations,
  transition
} from "../dist/index.js";

test("state machine exposes only the approved durable graph", () => {
  assert.deepEqual(TRANSITIONS.DRAFT, ["CHALLENGING"]);
  assert.deepEqual(TRANSITIONS.EVIDENCE_REQUIRED, ["CHALLENGING"]);
  assert.equal(canTransition("EVIDENCE_REQUIRED", "CONFIRMED"), false);
  assert.throws(() => transition("DRAFT", "CONFIRMED"), /Invalid research state transition/);
  assert.equal(transition("CHALLENGING", "CONFIRMED"), "CONFIRMED");
});

test("reservation confirmation requires the explicit audited command", () => {
  assert.equal(confirmWithReservations("CHALLENGING", {
    command: "CONFIRM_WITH_RESERVATIONS",
    unresolved_issues_acknowledged: true,
    reservation_note: "Direct confirmation retains the initial review gaps."
  }), "CONFIRMED");
  assert.equal(confirmWithReservations("EVIDENCE_REQUIRED", {
    command: "CONFIRM_WITH_RESERVATIONS",
    unresolved_issues_acknowledged: true,
    reservation_note: "Base-rate evidence remains unresolved."
  }), "CONFIRMED");
  assert.throws(() => confirmWithReservations("DRAFT", {
    command: "CONFIRM_WITH_RESERVATIONS",
    unresolved_issues_acknowledged: true,
    reservation_note: "Still unresolved."
  }), /explicit/);
});

test("probability scalars and intervals are jointly recomputable", () => {
  assert.doesNotThrow(() => assertProbabilityAssessment({
    premise_probability: 0.2,
    premise_probability_range: { minimum: 0.15, maximum: 0.25 },
    conditional_probability: 0.5,
    conditional_probability_range: { minimum: 0.4, maximum: 0.6 },
    joint_probability: 0.1,
    joint_probability_range: { minimum: 0.06, maximum: 0.15 }
  }));
  assert.throws(() => assertProbabilityAssessment({
    premise_probability: 0.2,
    premise_probability_range: { minimum: 0.15, maximum: 0.25 },
    conditional_probability: 0.5,
    conditional_probability_range: { minimum: 0.4, maximum: 0.6 },
    joint_probability: 0.11,
    joint_probability_range: { minimum: 0.06, maximum: 0.15 }
  }), /scalar product/);
});

test("worldlines reject cycles and require causally complete reveal schedules while retaining leak semantics", () => {
  assert.throws(() => assertAcyclicWorldline({
    nodes: [
      { node_id: "a", time_window: "a", event_date: "2026-08-10", data_as_of: "2026-08-10", event: "a", observable_indicators: [], causal_parents: [], state_changes: [], confidence: "low" },
      { node_id: "b", time_window: "b", event_date: "2026-09-01", data_as_of: "2026-08-10", event: "b", observable_indicators: [], causal_parents: ["a"], state_changes: [], confidence: "low" }
    ],
    edges: [{ from_node_id: "a", to_node_id: "b" }, { from_node_id: "b", to_node_id: "a" }]
  }), /acyclic/);
  assert.doesNotThrow(() => assertWorldlineDates({
    baseline_date: "2026-08-10",
    horizon_end: "2027-08-10",
    nodes: [
      { node_id: "a", time_window: "a", event_date: "2026-08-10", data_as_of: "2026-08-10", event: "a", observable_indicators: [], causal_parents: [], state_changes: [], confidence: "low" },
      { node_id: "b", time_window: "b", event_date: "2027-01-02", data_as_of: "2026-08-10", event: "b", observable_indicators: [], causal_parents: ["a"], state_changes: [], confidence: "low" }
    ],
    edges: [{ from_node_id: "a", to_node_id: "b" }],
    event_reveal_schedule: [
      { event_id: "a", event_date: "2026-08-10", reveal_date: "2026-08-10", rule: "observed" },
      { event_id: "b", event_date: "2027-01-02", reveal_date: "2027-01-01", rule: "leak" }
    ]
  }));
  assert.throws(() => assertWorldlineDates({
    baseline_date: "2026-08-10", horizon_end: "2027-08-10",
    nodes: [{ node_id: "a", time_window: "a", event_date: "2026-08-10", data_as_of: "2026-08-11", event: "a", observable_indicators: [], causal_parents: [], state_changes: [], confidence: "low" }],
    edges: [], event_reveal_schedule: []
  }), /future data/);
  assert.throws(() => assertWorldlineDates({
    baseline_date: "2026-08-10", horizon_end: "2027-08-10",
    nodes: [
      { node_id: "a", time_window: "a", event_date: "2026-08-10", data_as_of: "2026-08-10", event: "a", observable_indicators: [], causal_parents: [], state_changes: [], confidence: "low" },
      { node_id: "b", time_window: "b", event_date: "2027-01-02", data_as_of: "2026-08-10", event: "b", observable_indicators: [], causal_parents: [], state_changes: [], confidence: "low" }
    ],
    edges: [{ from_node_id: "a", to_node_id: "b" }],
    event_reveal_schedule: [
      { event_id: "a", event_date: "2026-08-10", reveal_date: "2026-08-10", rule: "observed" },
      { event_id: "b", event_date: "2027-01-01", reveal_date: "2027-01-02", rule: "observed" }
    ]
  }), /causal parents/);
});

test("9WL regression fixture is source-auditable and has nine complete deterministic branch decompositions", async () => {
  const fixture = JSON.parse(await readFile(new URL("../../test-fixtures/worldlines/v1/9wl-regression.json", import.meta.url), "utf8"));
  assert.equal(fixture.worldlines.length, 9);
  assert.match(fixture.disclaimer, /not a forecast/i);
  assert.match(fixture.event_date_rule, /deterministic/i);
  const ids = new Set(fixture.worldlines.map((scenario) => scenario.id));
  assert.equal(ids.size, 9);
  for (const scenario of fixture.worldlines) {
    assert.match(scenario.id, /^WL[1-9]$/);
    for (const source of Object.values(scenario.source)) {
      assert.ok(source.path.length > 0);
      assert.match(source.sha256, /^[a-f0-9]{64}$/);
    }
    assert.ok(scenario.title.length > 0 && scenario.trigger.length > 0);
    assert.ok(scenario.event_date <= scenario.horizon_end);
    assert.equal(scenario.news_date, scenario.reveal_date);
    if (scenario.rule === "leak") assert.ok(scenario.reveal_date <= scenario.event_date);
    else assert.ok(scenario.event_date <= scenario.reveal_date);
    assert.deepEqual(scenario.branches.map((branch) => branch.kind).sort(), ["main", "strong", "weak"]);
    assert.equal(new Set(scenario.branches.map((branch) => branch.id)).size, 3);
    assert.equal(scenario.branches.reduce((sum, branch) => sum + branch.probability.conditional, 0), 1);
    for (const branch of scenario.branches) {
      assert.equal(branch.id, `${scenario.id}:${branch.kind}`);
      assert.ok(branch.title.length > 0 && branch.trigger.length > 0);
      assert.equal(branch.trigger, scenario.trigger);
      assert.equal(branch.event_date, scenario.event_date);
      assert.equal(branch.reveal_date, scenario.reveal_date);
      assert.equal(branch.rule, scenario.rule);
      if (branch.rule === "leak") assert.ok(branch.reveal_date <= branch.event_date);
      else assert.ok(branch.event_date <= branch.reveal_date);
      assert.ok(Math.abs(branch.probability.joint - fixture.probability_inputs.premise.scalar * branch.probability.conditional) < 1e-12);
      assert.deepEqual(
        branch.probability.joint_range.map((value) => Number(value.toFixed(12))),
        [
          fixture.probability_inputs.premise.range[0] * branch.probability.conditional_range[0],
          fixture.probability_inputs.premise.range[1] * branch.probability.conditional_range[1]
        ].map((value) => Number(value.toFixed(12)))
      );
    }
  }
});
