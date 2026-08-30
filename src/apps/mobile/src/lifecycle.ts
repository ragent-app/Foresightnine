import { DEFAULT_RUNTIME_AS_OF, mockTurnsFor, PROVIDER_FRESHNESS_STATUS, rubricFor } from "./fixtures";
import type { AppAction, AppState } from "./types";

export const mobileInitialState: AppState = {
  stage: "thesis", language: "zh", viewpoint: "如果未来两年数据中心电力需求持续上升，并推动电网设备投资提速，那么电网基础设施将受益。", capital: "1000000", currency: "CNY", draftRestored: false,
  challengeStatus: "idle", challengeFailure: null, challengeTurns: [], replyDraft: "", rubric: rubricFor("zh"), evidence: [], branchSnapshot: null, searchStatus: "idle", confirmation: "pending", unresolvedIssuesAcknowledged: false, reservationNote: "", selectedBranch: "main",
  fmStatus: "idle", fmProgress: 0, fmStep: "", fmResult: null, portfolioMode: "public", exportNotice: "", freshness: { runtimeAsOf: DEFAULT_RUNTIME_AS_OF, providerStatus: PROVIDER_FRESHNESS_STATUS, fxProviderStatus: "unavailable" },
};

/** Reset every derived research field while retaining the user's locale and inputs. */
export function resetMobileWorkflowState(current: AppState, viewpoint = current.viewpoint): AppState {
  return { ...mobileInitialState, language: current.language, viewpoint, capital: current.capital, currency: current.currency, rubric: rubricFor(current.language), draftRestored: false };
}

type ChallengeAction = Extract<AppAction, { readonly type: "start_challenge" | "submit_reply" | "challenge_cancel" | "challenge_retry" | "challenge_fail" | "challenge_complete" }>;

function hasChallengeTranscript(state: AppState): boolean {
  const authors = new Set(state.challengeTurns.map((turn) => turn.author));
  return authors.has("system") && authors.has("challenger");
}

function canAdvanceChallenge(state: AppState): boolean {
  return (state.challengeStatus === "streaming" || state.challengeStatus === "recovering") && hasChallengeTranscript(state);
}

/**
 * The challenge transition boundary is deliberately small and pure so deep links,
 * UI callbacks, and archive tests share the same fail-closed lifecycle rules.
 */
export function reduceChallengeAction(state: AppState, action: ChallengeAction): AppState {
  switch (action.type) {
    case "start_challenge":
      return { ...resetMobileWorkflowState(state), stage: "challenge", challengeStatus: "streaming", challengeTurns: [...mockTurnsFor(state.language)] };
    case "challenge_retry":
      if (!["streaming", "recovering", "failed", "cancelled"].includes(state.challengeStatus)) return state;
      return { ...state, stage: "challenge", challengeStatus: "recovering", challengeFailure: null, challengeTurns: [...mockTurnsFor(state.language)] };
    case "challenge_fail":
      return canAdvanceChallenge(state) ? { ...state, challengeStatus: "failed", challengeFailure: action.failure } : state;
    case "challenge_cancel":
      return canAdvanceChallenge(state) ? { ...state, challengeStatus: "cancelled", challengeFailure: null } : state;
    case "submit_reply": {
      const body = action.body.trim();
      if (!body || !canAdvanceChallenge(state)) return state;
      return { ...state, replyDraft: "", challengeStatus: "complete", challengeFailure: null, challengeTurns: [...state.challengeTurns, { id: `user-${Date.now()}`, author: "user", body }] };
    }
    case "challenge_complete":
      return canAdvanceChallenge(state) ? { ...state, challengeStatus: "complete", challengeFailure: null } : state;
  }
}
