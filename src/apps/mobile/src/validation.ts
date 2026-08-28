import { t } from "./i18n";
import { normalizeEvidenceUrl } from "./evidence";
import { rubricFor } from "./fixtures";
import type { AppState, EvidenceBundle, Language } from "./types";

export interface ValidationErrors {
  viewpoint?: string;
  capital?: string;
}

export function parseCapital(raw: string): number | null {
  const normalized = raw.replace(/[,_\s￥¥$€]/g, "").trim();
  if (!normalized || !/^\d+(?:\.\d{1,2})?$/.test(normalized)) return null;
  const value = Number(normalized);
  return Number.isFinite(value) && value > 0 ? value : null;
}

export function validateThesis(viewpoint: string, capital: string, language: Language = "zh"): ValidationErrors {
  const errors: ValidationErrors = {};
  const trimmed = viewpoint.trim();
  if (trimmed.length < 12) errors.viewpoint = t(language, "viewpointTooShort");
  if (trimmed.length > 2000) errors.viewpoint = t(language, "viewpointTooLong");
  if (parseCapital(capital) === null) errors.capital = t(language, "invalidCapital");
  return errors;
}

export function hasValidationErrors(errors: ValidationErrors): boolean {
  return Object.values(errors).some(Boolean);
}

export function totalRubricScore(rubric: AppState["rubric"]): number {
  return rubric.reduce((sum, item) => sum + item.score, 0);
}

/** The ten rubric ids and critical flags are part of the persisted contract. */
export function isCanonicalRubric(rubric: AppState["rubric"], language?: Language): boolean {
  const canonical = language ? rubricFor(language) : null;
  if (rubric.length !== 10) return false;
  return rubric.every((item, index) => {
    const expected = canonical?.[index];
    return Number.isSafeInteger(item.score) && item.score >= 0 && item.score <= 4 && typeof item.label === "string" && typeof item.rationale === "string" && (expected
      ? item.id === expected.id && item.label === expected.label && item.critical === expected.critical
      : ["premise", "time", "trigger", "causal", "counterfactual", "evidence", "transmission", "priced", "falsifiable", "confirmation"][index] === item.id
        && item.critical === [true, true, false, true, false, false, true, false, true, true][index]);
  }) && new Set(rubric.map((item) => item.id)).size === 10;
}

export function hasPassingRubric(rubric: AppState["rubric"]): boolean {
  return isCanonicalRubric(rubric) && totalRubricScore(rubric) >= 30 && rubric.every((item) => !item.critical || item.score >= 3);
}

/** A complete transcript must show a real started challenge and user reply. */
export function hasValidCompletedChallenge(state: Pick<AppState, "challengeStatus"> & Partial<Pick<AppState, "challengeTurns" | "challengeFailure">>): boolean {
  if (state.challengeStatus !== "complete" || ("challengeFailure" in state && state.challengeFailure !== null) || !Array.isArray(state.challengeTurns) || state.challengeTurns.length < 2) return false;
  const ids = new Set<string>();
  let challengerSeen = false;
  let userReplySeen = false;
  for (const turn of state.challengeTurns) {
    if (!turn || typeof turn !== "object" || typeof turn.id !== "string" || turn.id.trim().length === 0 || ids.has(turn.id) || !["user", "challenger", "system"].includes(turn.author) || typeof turn.body !== "string" || turn.body.trim().length === 0) return false;
    ids.add(turn.id);
    if (turn.author === "challenger") challengerSeen = true;
    if (turn.author === "user" && challengerSeen) userReplySeen = true;
  }
  return challengerSeen && userReplySeen;
}

export function canUseChallengeRoute(state: Pick<AppState, "challengeStatus" | "challengeTurns">): boolean {
  return state.challengeStatus !== "idle" && state.challengeTurns.some((turn) => turn.author === "system") && state.challengeTurns.some((turn) => turn.author === "challenger");
}

const PUBLIC_EVIDENCE_SOURCE_GRADE: Readonly<Record<EvidenceBundle["source_type"], EvidenceBundle["grade"]>> = {
  primary: "A",
  reliable_secondary: "B",
  commentary: "C",
  user_private: "D",
  model_inference: "E",
};

export function isSemanticallyValidEvidence(item: EvidenceBundle): boolean {
  const expectedGrade = PUBLIC_EVIDENCE_SOURCE_GRADE[item.source_type];
  return expectedGrade === item.grade
    && (item.grade === "D" || item.grade === "E" || normalizeEvidenceUrl(item.url) !== null);
}

/** Derive reviewability from the source/grade/URL relation; never trust the flag. */
export function isDerivedPublicEvidence(item: EvidenceBundle): boolean {
  return (item.grade === "A" || item.grade === "B" || item.grade === "C")
    && PUBLIC_EVIDENCE_SOURCE_GRADE[item.source_type] === item.grade
    && normalizeEvidenceUrl(item.url) !== null;
}

/** D/E may be retained privately, but never satisfy the public evidence gate. */
export function hasPublicEvidence(evidence: readonly EvidenceBundle[]): boolean {
  return evidence.some(isDerivedPublicEvidence);
}

/** Normal confirmation follows the plan's 30/40, critical-score, and public-evidence gates. */
type ConfirmationState = Pick<AppState, "challengeStatus"> & Partial<Pick<AppState, "challengeTurns" | "challengeFailure" | "rubric" | "evidence" | "language" | "unresolvedIssuesAcknowledged" | "reservationNote">>;

export function canConfirmNormally(state: ConfirmationState): boolean {
  return hasValidCompletedChallenge(state)
    && state.rubric !== undefined
    && state.evidence !== undefined
    && state.language !== undefined
    && isCanonicalRubric(state.rubric, state.language)
    && hasPassingRubric(state.rubric)
    && state.evidence.every(isSemanticallyValidEvidence)
    && hasPublicEvidence(state.evidence);
}

export function canConfirmReserved(state: ConfirmationState): boolean {
  return hasValidCompletedChallenge(state)
    && state.rubric !== undefined
    && state.evidence !== undefined
    && state.language !== undefined
    && isCanonicalRubric(state.rubric, state.language)
    && state.evidence.every(isSemanticallyValidEvidence)
    && state.unresolvedIssuesAcknowledged === true
    && typeof state.reservationNote === "string"
    && state.reservationNote.trim().length > 0;
}

/** Worldline and FM routes are unavailable until one confirmation path has completed. */
export function canEnterWorldline(state: Pick<AppState, "challengeStatus" | "confirmation"> & Partial<Pick<AppState, "challengeTurns" | "challengeFailure" | "rubric" | "evidence" | "language" | "unresolvedIssuesAcknowledged" | "reservationNote">>): boolean {
  return state.confirmation === "confirmed" ? canConfirmNormally(state) : state.confirmation === "reserved" ? canConfirmReserved(state) : false;
}
