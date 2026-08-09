import assert from "node:assert/strict";
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
  assert.equal(confirmWithReservations("EVIDENCE_REQUIRED", {
    command: "CONFIRM_WITH_RESERVATIONS",
    unresolved_issues_acknowledged: true,
    reservation_note: "Base-rate evidence remains unresolved."
  }), "CONFIRMED");
  assert.throws(() => confirmWithReservations("CHALLENGING", {
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

test("worldlines reject cycles and reveal-before-event leakage", () => {
  assert.throws(() => assertAcyclicWorldline({
    nodes: [
      { node_id: "a", time_window: "a", event: "a", observable_indicators: [], causal_parents: [], state_changes: [], confidence: "low" },
      { node_id: "b", time_window: "b", event: "b", observable_indicators: [], causal_parents: ["a"], state_changes: [], confidence: "low" }
    ],
    edges: [{ from_node_id: "a", to_node_id: "b" }, { from_node_id: "b", to_node_id: "a" }]
  }), /acyclic/);
  assert.throws(() => assertWorldlineDates({
    baseline_date: "2026-08-10",
    horizon_end: "2027-08-10",
    event_reveal_schedule: [{ event_id: "e", event_date: "2027-01-02", reveal_date: "2027-01-01", rule: "leak" }]
  }), /cannot precede/);
});
