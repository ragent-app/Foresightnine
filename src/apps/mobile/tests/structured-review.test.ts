import { describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactNode } from "react";
import type { ChallengeRecord } from "@foresightnine/contracts";
vi.mock("expo-crypto", () => ({ getRandomBytes: () => new Uint8Array(16) }));
vi.mock("react-native", () => ({ Text: "Text", View: "View" }));
vi.mock("../src/ui", () => ({ styles: {} }));
import { challengeToMobileUi } from "../src/api-runtime";
import { ChallengeTranscript } from "../src/challenge-transcript";
import { buildPrivacySafeDocument, buildResearchSnapshot, normalizePrivacySafeDocument, restoreAppState } from "../src/archive";
import { ASSETS, FIXTURE_STATE, makeBranches } from "../src/fixtures";
import { portfolioMetadata } from "../src/portfolio";
import { mobileInitialState } from "../src/lifecycle";
import { canConfirmReserved, hasValidCompletedChallenge } from "../src/validation";
import type { TranscriptTurn } from "../src/types";

const record: ChallengeRecord = {
  schema_version: "v1", created_at: "2026-10-03T00:00:00Z", source_refs: [], content_hash: "0".repeat(64),
  turn_id: "qa-turn-1", challenge_id: "qa-challenge", thesis_id: "qa-thesis", claims: [],
  objections: [" Alternative explanation. "], questions: ["What falsifies the premise?", "What is the observation window?"], missing_evidence: ["Primary source"],
  rubric_scores: [], unresolved_issues: [], citation_ids: [], public_fact_citation_ids: [], native_search_used: false,
  provenance: "synthetic unit fixture", user_confirmation: "pending", private_evidence_declared: false, status: "CHALLENGING",
};
const initial = () => challengeToMobileUi(record, mobileInitialState.viewpoint, "en");
function textOf(node: ReactNode): string[] {
  if (Array.isArray(node)) return node.flatMap(textOf);
  if (typeof node === "string" || typeof node === "number") return [String(node)];
  return isValidElement<{children?: ReactNode}>(node) ? textOf(node.props.children) : [];
}
function documentFor(turns: readonly TranscriptTurn[]) {
  const state = { ...mobileInitialState, stage: "challenge" as const, challengeStatus: "complete" as const, challengeTurns: turns, replyDraft: "Unsubmitted draft retained." };
  const researchState = buildResearchSnapshot({ state, branches: makeBranches(state.viewpoint, state.language), methodData: { methodVersion: portfolioMetadata.method, dataAsOf: portfolioMetadata.dataAsOf, navLabel: portfolioMetadata.navLabel, fixtureState: FIXTURE_STATE, assetIds: ASSETS.map((asset) => asset.id) } });
  return buildPrivacySafeDocument({ title: "Synthetic research", viewpoint: state.viewpoint, capital: Number(state.capital), currency: state.currency, selectedBranch: state.selectedBranch, evidence: [], language: state.language, researchState });
}

describe("Android structured review", () => {
  it("preserves distinct questions, objections and evidence rather than only a concatenated string", () => {
    expect(initial().turns.at(-1)?.review).toEqual({ questions: record.questions, objections: ["Alternative explanation."], missingEvidence: ["Primary source"] });
  });
  it.each(["CHALLENGING", "EVIDENCE_REQUIRED", "CONFIRMED"] as const)("treats returned %s output as completed transport, without inventing a reply", (status) => {
    const update = challengeToMobileUi({ ...record, status }, "Synthetic premise", "en");
    expect(update.status).toBe("complete");
    const state = { ...mobileInitialState, challengeTurns: update.turns, challengeStatus: update.status, unresolvedIssuesAcknowledged: true, reservationNote: "Not evidence" };
    expect(hasValidCompletedChallenge(state)).toBe(false);
    expect(canConfirmReserved(state)).toBe(false);
  });
  it("retains prior rounds and places the user's reply before the new review", () => {
    const before = initial();
    const after = challengeToMobileUi({ ...record, turn_id: "qa-turn-2", questions: ["Follow-up?"] }, mobileInitialState.viewpoint, "en", "Actual synthetic reply", before.turns);
    expect(after.turns.slice(0, before.turns.length)).toEqual(before.turns);
    expect(after.turns.slice(-2).map((turn) => turn.author)).toEqual(["user", "challenger"]);
    expect(after.turns.at(-2)?.body).toBe("Actual synthetic reply");
    expect(after.turns.at(-1)?.review?.questions).toEqual(["Follow-up?"]);
    expect(hasValidCompletedChallenge({ challengeTurns: after.turns, challengeStatus: after.status, challengeFailure: null })).toBe(true);
    expect(before.turns).toHaveLength(3);
  });
  it("does not duplicate an already applied response on replay", () => {
    const next = { ...record, turn_id: "qa-turn-2" };
    const after = challengeToMobileUi(next, "Synthetic premise", "en", "Reply", initial().turns);
    expect(challengeToMobileUi(next, "Synthetic premise", "en", "Reply", after.turns).turns).toEqual(after.turns);
  });
  it.each(["zh", "en"] as const)("renders numbered questions in %s and hides duplicated flattened/system text", (language) => {
    const update = initial();
    const turns = update.turns.map((turn) => turn.author === "challenger" ? { ...turn, body: "FLATTENED_SHOULD_NOT_RENDER" } : turn);
    const text = textOf(ChallengeTranscript({ turns, language }));
    expect(text).toContain("1. What falsifies the premise?");
    expect(text).toContain("2. What is the observation window?");
    expect(text).toContain(language === "zh" ? "待补充证据" : "Evidence needed");
    expect(text).not.toContain("FLATTENED_SHOULD_NOT_RENDER");
    expect(text.join(" ")).not.toContain("Server-backed");
    expect(text.indexOf("1. What falsifies the premise?")).toBeLessThan(text.indexOf("1. Alternative explanation."));
  });
  it("keeps old unstructured transcripts readable without guessing question boundaries", () => {
    const text = textOf(ChallengeTranscript({ turns: [{ id: "old", author: "challenger", body: "Legacy plain text, not JSON parsing." }], language: "en" }));
    expect(text).toContain("Legacy plain text, not JSON parsing.");
  });
  it("round-trips structured transcript and an unsent draft through a validated JSON archive", () => {
    const doc = JSON.parse(JSON.stringify(documentFor(initial().turns)));
    expect(normalizePrivacySafeDocument(doc)).not.toBeNull();
    const restored = restoreAppState(mobileInitialState, doc);
    expect(restored.challengeTurns).toEqual(initial().turns);
    expect(restored.replyDraft).toBe("Unsubmitted draft retained.");
  });
  it.each([
    { questions: "not an array", objections: [], missingEvidence: [] },
    { questions: [" "], objections: [], missingEvidence: [] },
    { questions: ["Question?"], objections: [], missingEvidence: [], secret: "not allowed" },
    { questions: Array(101).fill("Question?"), objections: [], missingEvidence: [] },
  ])("rejects malformed or unexpected structured archive fields", (review) => {
    const doc = JSON.parse(JSON.stringify(documentFor(initial().turns)));
    doc.researchState.challengeTurns[2].review = review;
    expect(normalizePrivacySafeDocument(doc)).toBeNull();
  });
  it("does not accept challenger metadata attached to a user turn", () => {
    const doc = JSON.parse(JSON.stringify(documentFor(initial().turns)));
    doc.researchState.challengeTurns[1].review = doc.researchState.challengeTurns[2].review;
    expect(normalizePrivacySafeDocument(doc)).toBeNull();
  });
});
