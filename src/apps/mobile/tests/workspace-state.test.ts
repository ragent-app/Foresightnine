import { describe, expect, it } from "vitest";
import { buildWorkspaceDocument, normalizeWorkspaceDocument, restoreWorkspaceState, serializeWorkspaceDocument, workspaceStorageScope } from "../src/workspace-state";
import { mobileInitialState } from "../src/lifecycle";
import { computeFm } from "@foresightnine/fm-engine";
import { ASSETS, FIXTURE_STATE, makeBranches, fmInput, MOCK_EVIDENCE, rubricFor } from "../src/fixtures";
import { portfolioMetadata } from "../src/portfolio";
const input = () => ({ state: { ...mobileInitialState, viewpoint: "Synthetic study", stage: "challenge" as const, challengeStatus: "complete" as const, challengeTurns: [{ id: "question", author: "challenger" as const, body: "Synthetic question", review: { questions: ["Which evidence falsifies this?"], objections: [], missingEvidence: [] } }], replyDraft: "Unsubmitted reply" }, branches: [], methodData: { methodVersion: portfolioMetadata.method, dataAsOf: portfolioMetadata.dataAsOf, navLabel: portfolioMetadata.navLabel, fixtureState: FIXTURE_STATE, assetIds: ASSETS.map(a => a.id) }, apiChallengeId: "synthetic-challenge-id", evidenceWorkspace: { sharedText: "Unsubmitted source", searchQuery: "Synthetic search", candidates: [] }, scenarioDrafts: { main: { [ASSETS[0]!.id]: "-12.5" } }, assets: ASSETS });
describe("private cold-start workspace", () => {
  it("round-trips formal questions, replies, research ID, unsubmitted sources and scenario inputs", () => {
    const document = buildWorkspaceDocument(input()); const restored = normalizeWorkspaceDocument(JSON.parse(new TextDecoder().decode(serializeWorkspaceDocument(document)))); expect(restored).not.toBeNull();
    expect(restored?.apiChallengeId).toBe("synthetic-challenge-id"); expect(restored?.research.replyDraft).toBe("Unsubmitted reply"); expect(restored?.research.challengeTurns[0]?.review?.questions).toEqual(["Which evidence falsifies this?"]); expect(restored?.evidenceWorkspace).toEqual(input().evidenceWorkspace); expect(restored?.scenarioDrafts).toEqual(input().scenarioDrafts);
  });
  it("restores a completed worldline/FM plan with its actual saved assets and assumptions", () => {
    const original = input(); const branches = makeBranches(original.state.viewpoint, "zh");
    const state = { ...original.state, stage: "portfolio" as const, confirmation: "reserved" as const, unresolvedIssuesAcknowledged: true, reservationNote: "Synthetic unresolved evidence", challengeTurns: [{ id: "system", author: "system" as const, body: "Synthetic protocol" }, ...original.state.challengeTurns, { id: "user-reply", author: "user" as const, body: "Efficiency gains could delay the assumed demand path." }], rubric: rubricFor("zh").map(item => item.id === "confirmation" ? { ...item, score: 4 } : item), evidence: MOCK_EVIDENCE, branchSnapshot: branches, fmStatus: "complete" as const, fmProgress: 100, fmResult: computeFm(fmInput()) };
    const document = buildWorkspaceDocument({ ...original, state, branches }); const restored = restoreWorkspaceState(mobileInitialState, document);
    expect(restored.stage).toBe("portfolio"); expect(restored.branchSnapshot).toEqual(branches); expect(restored.fmStatus).toBe("complete"); expect(Array.from(restored.fmResult!.weights.weights)).toEqual(Array.from(state.fmResult.weights.weights)); expect(document.assets).toEqual(ASSETS); expect(document.scenarioDrafts).toEqual(original.scenarioDrafts);
  });
  it("keeps incomplete input text without inventing valid money or a thesis", () => {
    const original = input(); const document = buildWorkspaceDocument({ ...original, state: { ...original.state, viewpoint: "", capital: "-" } });
    const restored = restoreWorkspaceState(mobileInitialState, document); expect(restored.viewpoint).toBe(""); expect(restored.capital).toBe("-");
  });
  it("recovers interrupted work as cancelled, without granting confirmation or starting network work", () => {
    const original = input(); const document = buildWorkspaceDocument({ ...original, state: { ...original.state, challengeStatus: "streaming", searchStatus: "loading" } });
    const state = restoreWorkspaceState(mobileInitialState, document); expect(state.challengeStatus).toBe("cancelled"); expect(state.searchStatus).toBe("idle"); expect(state.confirmation).toBe("pending"); expect(state.freshness.providerStatus).toBe("unavailable");
  });
  it("rejects extra credential fields rather than persisting them", () => {
    const document = buildWorkspaceDocument(input()); expect(normalizeWorkspaceDocument({ ...document, access_token: "synthetic-only" })).toBeNull();
  });
  it("rejects unsafe links, malformed runtime IDs and undeclared scenario assets", () => {
    const d = buildWorkspaceDocument(input()); expect(normalizeWorkspaceDocument({ ...d, apiChallengeId: 12 })).toBeNull(); expect(normalizeWorkspaceDocument({ ...d, scenarioDrafts: { main: { unregistered: "5" } } })).toBeNull();
    expect(normalizeWorkspaceDocument({ ...d, evidenceWorkspace: { ...d.evidenceWorkspace, candidates: [{ searchResultHash: "a".repeat(64), searchProvider: "synthetic", title: "test", url: "javascript:alert(1)", excerpt: "test", publisher: null, publishedAt: null, retrievedAt: "2026-10-03T00:00:00Z", independentlyReviewable: false }] } })).toBeNull();
  });
  it("rejects inconsistent assets and oversized private workspaces", () => {
    const d = buildWorkspaceDocument(input()); expect(normalizeWorkspaceDocument({ ...d, assets: d.assets.slice(1) })).toBeNull(); expect(() => serializeWorkspaceDocument({ ...d, evidenceWorkspace: { ...d.evidenceWorkspace, sharedText: "x".repeat(1024 * 1024) } })).toThrow();
  });
  it("separates server environments and account owners", () => {
    expect(workspaceStorageScope("account:A", "https://app.foresightnine.com")).not.toBe(workspaceStorageScope("account:A", "https://api-staging.foresightnine.com")); expect(workspaceStorageScope("account:A", null)).not.toBe(workspaceStorageScope("account:B", null));
  });
});
