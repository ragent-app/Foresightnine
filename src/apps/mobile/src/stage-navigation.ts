import { canEnterPortfolio } from "./archive";
import { canEnterWorldline, canUseChallengeRoute } from "./validation";
import type { AppState } from "./types";

export const RESEARCH_ROUTES = ["/", "/challenge", "/evidence", "/worldline", "/portfolio"] as const;
export type ResearchRoute = typeof RESEARCH_ROUTES[number];

/** Inspect reached state only; navigation must never rerun research or reset drafts. */
export function researchStageAvailability(state: AppState, hasWorldlines: boolean): readonly boolean[] {
  const review = canUseChallengeRoute(state);
  const worldline = hasWorldlines && canEnterWorldline(state);
  return [true, review || state.challengeStatus === "streaming", review, worldline,
    canEnterPortfolio(state) || worldline && state.fmStatus === "computing"];
}

export function researchStageRoute(state: AppState, hasWorldlines: boolean, index: number): ResearchRoute | null {
  return Number.isInteger(index) && researchStageAvailability(state, hasWorldlines)[index] ? RESEARCH_ROUTES[index] ?? null : null;
}
