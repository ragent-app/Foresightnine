import type { ProbabilityAssessment, ProbabilityRange } from "@foresightnine/contracts";

export function assertProbabilityRange(range: ProbabilityRange, label: string): void {
  if (range.minimum < 0 || range.maximum > 1 || range.minimum > range.maximum) {
    throw new RangeError(`${label} must be an ordered probability range within [0, 1]`);
  }
}

/** Interval multiplication preserves uncertainty without presenting pseudo-precise probabilities. */
export function jointProbabilityRange(
  premise: ProbabilityRange,
  conditional: ProbabilityRange
): ProbabilityRange {
  assertProbabilityRange(premise, "premise");
  assertProbabilityRange(conditional, "conditional");
  return {
    minimum: premise.minimum * conditional.minimum,
    maximum: premise.maximum * conditional.maximum
  };
}

export function assertProbabilityAssessment(
  assessment: Pick<
    ProbabilityAssessment,
    | "premise_probability"
    | "premise_probability_range"
    | "conditional_probability"
    | "conditional_probability_range"
    | "joint_probability"
    | "joint_probability_range"
  >,
  tolerance = 1e-12
): void {
  const pairs = [
    [assessment.premise_probability, assessment.premise_probability_range, "premise"],
    [assessment.conditional_probability, assessment.conditional_probability_range, "conditional"],
    [assessment.joint_probability, assessment.joint_probability_range, "joint"]
  ] as const;
  for (const [scalar, range, label] of pairs) {
    assertProbabilityRange(range, label);
    if (scalar < range.minimum || scalar > range.maximum) {
      throw new RangeError(`${label} scalar must be contained by its interval`);
    }
  }
  const expectedRange = jointProbabilityRange(
    assessment.premise_probability_range,
    assessment.conditional_probability_range
  );
  if (
    Math.abs(assessment.joint_probability_range.minimum - expectedRange.minimum) > tolerance ||
    Math.abs(assessment.joint_probability_range.maximum - expectedRange.maximum) > tolerance
  ) {
    throw new RangeError("joint probability interval must equal the premise/conditional interval product");
  }
  if (
    Math.abs(
      assessment.joint_probability -
      assessment.premise_probability * assessment.conditional_probability
    ) > tolerance
  ) {
    throw new RangeError("joint probability scalar must equal the premise/conditional scalar product");
  }
}
