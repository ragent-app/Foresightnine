# ADR-005: Worldline and probability provider boundary

- Status: accepted
- Date: 2026-08-15

## Decision

Phase 4 exposes provider-neutral `WorldlineGenerator` and `ProbabilityAssessor` interfaces. The default implementations are deterministic mocks and require no key. Generation receives a retained `CONFIRMED` challenge and its retained thesis, rather than client-supplied premise or path strings. It returns exactly weak, main and strong paths plus one assessment per path.

Probability is interval-first. Every assessment carries P(premise), P(path | premise), and joint P(path), with scalar values kept only as reproducible representative values. The required relations are `joint_scalar = premise_scalar × conditional_scalar` and `joint_interval = [premise_min × conditional_min, premise_max × conditional_max]`. The three conditional scalar branch probabilities must sum to one; the three intervals must jointly admit one. Mock estimates are explicitly `model_estimate_unvalidated` and low confidence.

Provider output is accepted only as generated canonical models and then checked for graph, temporal, probability and branch-partition semantics. Unknown output types and semantic violations fail closed before storage or SSE completion.

## Consequences

Nodes now declare event time and `data_as_of`; every data time is no later than the baseline and its event. Edges must be time-monotonic and acyclic. Scheduled/observed reveals cannot precede events; leak events may, while both dates remain in the scenario horizon. Asset impacts are embedded in each worldline alongside economic anchors so first- and second-order reasoning is auditable.

The 9WL fixture is a minimal regression index copied from the read-only reference set. Its scenario labels and trigger timing are fixtures only, not facts or forecasts, and its probability values are deterministic test inputs.
