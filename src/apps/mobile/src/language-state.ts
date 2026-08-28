import { canEnterPortfolio } from "./archive";
import { rubricFor } from "./fixtures";
import { resetMobileWorkflowState } from "./lifecycle";
import type { AppState, Language } from "./types";
import { canConfirmNormally, canConfirmReserved, isCanonicalRubric } from "./validation";

function localizedRubric(current: AppState, language: Language): AppState["rubric"] {
  const defaults = rubricFor(language);
  if (!isCanonicalRubric(current.rubric, current.language)) return defaults;
  return defaults.map((item, index) => ({ ...item, score: current.rubric[index]!.score }));
}

function downgradeToWorldline(state: AppState): AppState {
  return { ...state, stage: "worldline", branchSnapshot: null, selectedBranch: "main", fmStatus: "idle", fmProgress: 0, fmStep: "", fmResult: null };
}

/** Preserve rubric scores across locales, but never preserve an unverifiable lifecycle. */
export function switchLanguageState(current: AppState, language: Language): AppState {
  const next: AppState = { ...current, language, rubric: localizedRubric(current, language), branchSnapshot: null };
  const confirmationValid = next.confirmation === "pending"
    ? true
    : next.confirmation === "confirmed" ? canConfirmNormally(next) : canConfirmReserved(next);
  if (!confirmationValid) return resetMobileWorkflowState(next);
  if (next.confirmation === "pending" && ["worldline", "portfolio", "export"].includes(next.stage)) return resetMobileWorkflowState(next);
  if (next.confirmation !== "pending" && ["thesis", "challenge", "evidence"].includes(next.stage)) return downgradeToWorldline(next);
  if (next.stage === "portfolio" && !canEnterPortfolio(next)) return downgradeToWorldline(next);
  if (next.stage === "worldline" && (next.fmStatus !== "idle" || next.fmResult !== null)) return downgradeToWorldline(next);
  return next;
}
