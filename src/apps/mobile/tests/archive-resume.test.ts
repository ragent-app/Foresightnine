import { describe, expect, it } from "vitest";
import { computeFm } from "@foresightnine/fm-engine";
import { buildPrivacySafeDocument, buildResearchSnapshot, canRestoreArchive, canRestorePrivacySafeDocument, makeArchiveEnvelope, normalizePrivacySafeDocument, restoreAppState } from "../src/archive";
import { ASSETS, FIXTURE_STATE, fmInput, makeBranches, MOCK_EVIDENCE, PROVIDER_FRESHNESS_STATUS, rubricFor, RUNTIME_AS_OF } from "../src/fixtures";
import { createPortfolioPlan, portfolioMetadata } from "../src/portfolio";
import type { AppState } from "../src/types";

const FRESH_FIXTURE = { runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus: "fresh" as const };

function stateForCompletedWorkflow(): AppState {
  const language = "en" as const;
  return {
    stage: "portfolio",
    language,
    viewpoint: "Data-center power demand accelerates grid-equipment investment.",
    capital: "250000",
    currency: "USD",
    draftRestored: false,
    challengeStatus: "complete",
    challengeFailure: null,
    challengeTurns: [
      { id: "system-1", author: "system", body: "Offline challenge" },
      { id: "challenger-1", author: "challenger", body: "State the strongest counterexample." },
      { id: "user-1", author: "user", body: "Efficiency gains could delay the demand path." },
    ],
    replyDraft: "",
    rubric: rubricFor(language).map((item) => item.id === "confirmation" ? { ...item, score: 4 } : item),
    evidence: MOCK_EVIDENCE,
    branchSnapshot: null,
    searchStatus: "mock_ready",
    confirmation: "reserved",
    unresolvedIssuesAcknowledged: true,
    reservationNote: "Order and cash-flow evidence remains unresolved.",
    selectedBranch: "strong",
    fmStatus: "complete",
    fmProgress: 100,
    fmStep: "FM client computation complete",
    fmResult: computeFm(fmInput()),
    portfolioMode: "public",
    freshness: { runtimeAsOf: RUNTIME_AS_OF, providerStatus: PROVIDER_FRESHNESS_STATUS },
    exportNotice: "",
  };
}

function coldStartState(): AppState {
  const source = stateForCompletedWorkflow();
  return { ...source, stage: "thesis", challengeStatus: "idle", challengeTurns: [], rubric: rubricFor("zh"), evidence: [], branchSnapshot: null, fmStatus: "idle", fmProgress: 0, fmStep: "", fmResult: null, language: "zh", viewpoint: "", capital: "", currency: "CNY", searchStatus: "idle", confirmation: "pending", selectedBranch: "main" };
}

function currentDocumentJson(): Record<string, unknown> {
  const completed = stateForCompletedWorkflow();
  const branches = makeBranches(completed.viewpoint, completed.language);
  const researchState = buildResearchSnapshot({
    state: completed,
    branches,
    methodData: { methodVersion: portfolioMetadata.method, dataAsOf: portfolioMetadata.dataAsOf, navLabel: portfolioMetadata.navLabel, fixtureState: FIXTURE_STATE, assetIds: ASSETS.map((asset) => asset.id) },
  });
  return JSON.parse(JSON.stringify(buildPrivacySafeDocument({ title: "ForesightNine local archive", viewpoint: completed.viewpoint, capital: 250000, currency: completed.currency, selectedBranch: completed.selectedBranch, evidence: researchState.evidence, language: completed.language, researchState }))) as Record<string, unknown>;
}

describe("offline complete snapshot resume", () => {
  it("restores completed challenge, worldline, FM, portfolio and method state after a cold JSON start", () => {
    const completed = stateForCompletedWorkflow();
    const branches = makeBranches(completed.viewpoint, completed.language);
    const researchState = buildResearchSnapshot({
      state: completed,
      branches,
      methodData: { methodVersion: portfolioMetadata.method, dataAsOf: portfolioMetadata.dataAsOf, navLabel: portfolioMetadata.navLabel, fixtureState: FIXTURE_STATE, assetIds: ASSETS.map((asset) => asset.id) },
    });
    const document = buildPrivacySafeDocument({ title: "ForesightNine local archive", viewpoint: completed.viewpoint, capital: 250000, currency: completed.currency, selectedBranch: completed.selectedBranch, evidence: researchState.evidence, language: completed.language, researchState });

    const coldDocument: unknown = JSON.parse(JSON.stringify(document));
    expect(canRestorePrivacySafeDocument(coldDocument)).toBe(true);
    if (!canRestorePrivacySafeDocument(coldDocument)) return;

    const restored = restoreAppState(coldStartState(), coldDocument);
    expect(restored.stage).toBe("portfolio");
    expect(restored.challengeStatus).toBe("complete");
    expect(restored.challengeTurns).toEqual(completed.challengeTurns);
    expect(restored.confirmation).toBe("reserved");
    expect(restored.unresolvedIssuesAcknowledged).toBe(true);
    expect(restored.reservationNote).toBe(completed.reservationNote);
    expect(restored.selectedBranch).toBe("strong");
    expect(restored.branchSnapshot).toEqual(branches);
    expect(restored.fmStatus).toBe("complete");
    expect(restored.fmResult?.selectedFactorIds).toEqual(completed.fmResult?.selectedFactorIds);
    expect(Array.from(restored.fmResult?.weights.weights ?? [])).toEqual(Array.from(completed.fmResult?.weights.weights ?? []));

    const portfolio = restored.fmResult ? createPortfolioPlan(restored.fmResult, 250000, restored.currency, undefined, restored.language, restored.selectedBranch, FRESH_FIXTURE) : null;
    expect(portfolio?.scenario.branch).toBe("strong");
    expect(portfolio?.publicMarketValue).toBeCloseTo(250000, 6);
    expect(coldDocument.researchState?.methodData).toEqual(researchState.methodData);
    expect(coldDocument.evidence.some((item: { source_type: string }) => item.source_type === "user_private")).toBe(false);
    expect(coldDocument.researchState?.evidence.some((item: { source_type: string }) => item.source_type === "user_private")).toBe(false);
  });

  it("restores older archives with a pending, unacknowledged reservation state", () => {
    const branches = makeBranches("legacy premise", "en");
    const document = buildPrivacySafeDocument({ title: "legacy", viewpoint: "legacy premise", capital: 100, currency: "USD", selectedBranch: "main", evidence: [], language: "en", researchState: buildResearchSnapshot({ state: stateForCompletedWorkflow(), branches, methodData: { methodVersion: portfolioMetadata.method, dataAsOf: portfolioMetadata.dataAsOf, navLabel: portfolioMetadata.navLabel, fixtureState: FIXTURE_STATE, assetIds: ASSETS.map((asset) => asset.id) } }) });
    const legacy = JSON.parse(JSON.stringify(document)) as Record<string, unknown> & { schema: string; researchState?: Record<string, unknown> };
    legacy.schema = "f9-privacy-safe-research-v1";
    delete legacy.unresolvedIssuesAcknowledged;
    delete legacy.reservationNote;
    delete legacy.researchState?.unresolvedIssuesAcknowledged;
    delete legacy.researchState?.reservationNote;
    expect(canRestorePrivacySafeDocument(legacy)).toBe(true);
    if (!canRestorePrivacySafeDocument(legacy)) return;
    const restored = restoreAppState(coldStartState(), legacy);
    expect(restored.confirmation).toBe("pending");
    expect(restored.unresolvedIssuesAcknowledged).toBe(false);
    expect(restored.reservationNote).toBe("");
  });

  it("resets a warm workflow when a legacy document has no research snapshot", () => {
    const legacy = buildPrivacySafeDocument({ title: "legacy inputs only", viewpoint: "legacy premise", capital: 100, currency: "USD", selectedBranch: "strong", evidence: [], language: "en" });
    const restored = restoreAppState(stateForCompletedWorkflow(), legacy);
    expect(restored).toMatchObject({ stage: "thesis", language: "en", viewpoint: "legacy premise", capital: "100", currency: "USD", challengeStatus: "idle", challengeTurns: [], replyDraft: "", evidence: [], branchSnapshot: null, confirmation: "pending", unresolvedIssuesAcknowledged: false, reservationNote: "", selectedBranch: "main", fmStatus: "idle", fmProgress: 0, fmStep: "", fmResult: null, portfolioMode: "public", draftRestored: true });
    expect(restored.rubric).toEqual(rubricFor("en"));
  });

  it("rejects malformed JSON and archive envelopes without throwing", () => {
    expect(() => restoreAppState(coldStartState(), "{malformed-json")).not.toThrow();
    expect(restoreAppState(coldStartState(), "{malformed-json")).toEqual(coldStartState());
    const envelope = makeArchiveEnvelope({ aad: "f9-archive-envelope-v1", combined: "c2VhbGVk", createdAt: "2026-08-23T00:00:00Z", documentHash: "0".repeat(64) });
    expect(canRestoreArchive([envelope])).toBe(false);
    expect(canRestoreArchive({ ...envelope, aad: "wrong-aad" })).toBe(false);
    expect(canRestoreArchive({ ...envelope, combined: "%%%%" })).toBe(false);
    expect(canRestoreArchive({ ...envelope, documentHash: "short" })).toBe(false);
  });

  it("rejects malformed FM snapshots before restoration", () => {
    const malformed = currentDocumentJson();
    const researchState = malformed.researchState as Record<string, unknown>;
    const fmResult = researchState.fmResult as Record<string, unknown>;
    const rawClientResult = fmResult.rawClientResult as Record<string, unknown>;
    rawClientResult.targetWeights = ["not-a-number"];
    expect(canRestorePrivacySafeDocument(malformed)).toBe(false);
    expect(() => restoreAppState(coldStartState(), malformed)).not.toThrow();
    expect(restoreAppState(coldStartState(), malformed)).toEqual(coldStartState());
  });

  it("rejects contradictory typed and raw FM vectors before portfolio restoration", () => {
    const mismatchedWeights = currentDocumentJson();
    const mismatchedWeightsFm = (mismatchedWeights.researchState as Record<string, unknown>).fmResult as Record<string, unknown>;
    const weights = mismatchedWeightsFm.weights as number[];
    mismatchedWeightsFm.rawClientResult = { ...(mismatchedWeightsFm.rawClientResult as Record<string, unknown>), targetWeights: weights.map((value, index) => index === 0 ? value + 0.01 : value) };
    expect(canRestorePrivacySafeDocument(mismatchedWeights)).toBe(false);
    expect(restoreAppState(coldStartState(), mismatchedWeights)).toEqual(coldStartState());

    const mismatchedForecasts = currentDocumentJson();
    const mismatchedForecastsFm = (mismatchedForecasts.researchState as Record<string, unknown>).fmResult as Record<string, unknown>;
    const forecasts = mismatchedForecastsFm.forecasts as Array<number | null>;
    mismatchedForecastsFm.rawClientResult = { ...(mismatchedForecastsFm.rawClientResult as Record<string, unknown>), forecastReturns: forecasts.map((value, index) => index === 0 ? (value ?? 0) + 0.01 : value) };
    expect(canRestorePrivacySafeDocument(mismatchedForecasts)).toBe(false);
    expect(restoreAppState(coldStartState(), mismatchedForecasts)).toEqual(coldStartState());
  });

  it("re-derives public evidence reviewability when building documents and snapshots", () => {
    const publicEvidence = { ...MOCK_EVIDENCE[0]!, independently_reviewable: false };
    const noUrl = { ...MOCK_EVIDENCE[0]!, evidence_id: "fixture-no-url", url: "", independently_reviewable: true };
    const privateEvidence = { ...MOCK_EVIDENCE[1]!, independently_reviewable: true };
    const evidence = [publicEvidence, noUrl, privateEvidence];
    const document = buildPrivacySafeDocument({ title: "reviewability", viewpoint: "test", capital: 100, currency: "USD", selectedBranch: "main", evidence, language: "en" }, true);
    expect(document.evidence.map((item) => item.independently_reviewable)).toEqual([true, false, false]);

    const snapshot = buildResearchSnapshot({ state: { ...stateForCompletedWorkflow(), evidence }, branches: makeBranches("test", "en"), methodData: { methodVersion: portfolioMetadata.method, dataAsOf: portfolioMetadata.dataAsOf, navLabel: portfolioMetadata.navLabel, fixtureState: FIXTURE_STATE, assetIds: ASSETS.map((asset) => asset.id) } }, true);
    expect(snapshot.evidence.map((item) => item.independently_reviewable)).toEqual([true, false, false]);
  });

  it("fails closed for reserved snapshots with malformed rubric or public evidence", () => {
    const malformedRubric = currentDocumentJson();
    const malformedRubricState = malformedRubric.researchState as Record<string, unknown>;
    malformedRubricState.confirmation = "reserved";
    malformedRubricState.unresolvedIssuesAcknowledged = true;
    malformedRubricState.reservationNote = "A reservation with unresolved issues.";
    (malformedRubricState.rubric as Array<Record<string, unknown>>)[0]!.id = "forged-premise";
    expect(canRestorePrivacySafeDocument(malformedRubric)).toBe(false);
    expect(restoreAppState(coldStartState(), malformedRubric).confirmation).toBe("pending");

    const malformedEvidence = currentDocumentJson();
    const malformedEvidenceState = malformedEvidence.researchState as Record<string, unknown>;
    malformedEvidenceState.confirmation = "reserved";
    malformedEvidenceState.unresolvedIssuesAcknowledged = true;
    malformedEvidenceState.reservationNote = "A reservation with unresolved issues.";
    const evidence = malformedEvidenceState.evidence as Array<Record<string, unknown>>;
    evidence[0]!.url = "";
    evidence[0]!.independently_reviewable = true;
    const normalized = normalizePrivacySafeDocument(malformedEvidence);
    expect(normalized?.researchState?.confirmation).toBe("pending");
    expect(normalized?.researchState?.fmResult).toBeNull();
  });

  it("fails closed when a completed snapshot has no real user/challenger transcript", () => {
    const missingTurns = currentDocumentJson();
    delete (missingTurns.researchState as Record<string, unknown>).challengeTurns;
    expect(canRestorePrivacySafeDocument(missingTurns)).toBe(false);
    expect(restoreAppState(coldStartState(), missingTurns)).toEqual(coldStartState());

    const incompleteTurns = currentDocumentJson();
    const incompleteState = incompleteTurns.researchState as Record<string, unknown>;
    incompleteState.confirmation = "reserved";
    incompleteState.unresolvedIssuesAcknowledged = true;
    incompleteState.reservationNote = "A reservation with an incomplete challenge.";
    incompleteState.challengeTurns = (incompleteState.challengeTurns as Array<Record<string, unknown>>).filter((turn) => turn.author !== "user");
    const restored = restoreAppState(coldStartState(), incompleteTurns);
    expect(canRestorePrivacySafeDocument(incompleteTurns)).toBe(true);
    expect(restored.confirmation).toBe("pending");
    expect(restored.fmResult).toBeNull();
    expect(restored.branchSnapshot).toBeNull();
  });

  it("rejects FM snapshots whose vectors or asset registry do not match the six-asset schema", () => {
    const wrongWidth = currentDocumentJson();
    const wrongWidthState = wrongWidth.researchState as Record<string, unknown>;
    const wrongWidthFm = wrongWidthState.fmResult as Record<string, unknown>;
    wrongWidthFm.weights = [...(wrongWidthFm.weights as number[]), 0];
    expect(canRestorePrivacySafeDocument(wrongWidth)).toBe(false);

    const wrongAssets = currentDocumentJson();
    const wrongAssetsState = wrongAssets.researchState as Record<string, unknown>;
    const methodData = wrongAssetsState.methodData as Record<string, unknown>;
    methodData.assetIds = ["GRID", "CHIP", "UTIL", "GOLD", "WORLD"];
    expect(canRestorePrivacySafeDocument(wrongAssets)).toBe(false);
    expect(restoreAppState(coldStartState(), wrongAssets)).toEqual(coldStartState());

    const wrongVolatility = currentDocumentJson();
    const wrongVolatilityFm = (wrongVolatility.researchState as Record<string, unknown>).fmResult as Record<string, unknown>;
    const forecastScale = wrongVolatilityFm.forecastScale as Record<string, unknown>;
    forecastScale.crossSectionalVolatility = [];
    expect(canRestorePrivacySafeDocument(wrongVolatility)).toBe(false);

    const emptyVolatility = currentDocumentJson();
    const emptyVolatilityFm = (emptyVolatility.researchState as Record<string, unknown>).fmResult as Record<string, unknown>;
    (emptyVolatilityFm.forecastScale as Record<string, unknown>).crossSectionalVolatility = [0, 0, 0, 0, "malformed"];
    expect(canRestorePrivacySafeDocument(emptyVolatility)).toBe(false);
  });

  it("downgrades computing without a result to an idle worldline-safe state", () => {
    const computing = currentDocumentJson();
    const researchState = computing.researchState as Record<string, unknown>;
    researchState.stage = "portfolio";
    researchState.fmStatus = "computing";
    researchState.fmProgress = 42;
    researchState.fmStep = "Computing factors";
    researchState.fmResult = null;
    const normalized = normalizePrivacySafeDocument(computing);
    expect(normalized?.researchState).toMatchObject({ stage: "worldline", fmStatus: "idle", fmProgress: 0, fmStep: "", fmResult: null });
    const restored = restoreAppState(coldStartState(), computing);
    expect(restored).toMatchObject({ stage: "worldline", fmStatus: "idle", fmProgress: 0, fmStep: "", fmResult: null });
  });

  it("downgrades contradictory stage and FM result combinations before portfolio", () => {
    const resultInWorldline = currentDocumentJson();
    const worldlineState = resultInWorldline.researchState as Record<string, unknown>;
    worldlineState.stage = "worldline";
    worldlineState.fmStatus = "complete";
    const normalizedWorldline = normalizePrivacySafeDocument(resultInWorldline);
    expect(normalizedWorldline?.researchState).toMatchObject({ stage: "worldline", fmStatus: "idle", fmResult: null });

    const resultWithIdle = currentDocumentJson();
    const idleState = resultWithIdle.researchState as Record<string, unknown>;
    idleState.stage = "portfolio";
    idleState.fmStatus = "idle";
    const normalizedIdle = normalizePrivacySafeDocument(resultWithIdle);
    expect(normalizedIdle?.researchState).toMatchObject({ stage: "worldline", fmStatus: "idle", fmResult: null });
  });

  it("rejects future FM, branch, and method data cutoffs", () => {
    const futureFm = currentDocumentJson();
    ((futureFm.researchState as Record<string, unknown>).fmResult as Record<string, unknown>).cutoff = "2026-08-10";
    expect(canRestorePrivacySafeDocument(futureFm)).toBe(false);

    const futureBranch = currentDocumentJson();
    const branches = (futureBranch.researchState as Record<string, unknown>).branches as Array<Record<string, unknown>>;
    branches[0]!.dataAsOf = "2026-08-10";
    expect(canRestorePrivacySafeDocument(futureBranch)).toBe(false);

    const futureMethod = currentDocumentJson();
    ((futureMethod.researchState as Record<string, unknown>).methodData as Record<string, unknown>).dataAsOf = "2026-08-10";
    expect(canRestorePrivacySafeDocument(futureMethod)).toBe(false);
  });

  it("downgrades inconsistent confirmed and reserved lifecycle states to pending", () => {
    const confirmedWithoutCompletion = currentDocumentJson();
    const confirmedState = confirmedWithoutCompletion.researchState as Record<string, unknown>;
    confirmedState.challengeStatus = "streaming";
    confirmedState.confirmation = "confirmed";
    const restoredConfirmed = restoreAppState(coldStartState(), confirmedWithoutCompletion);
    expect(canRestorePrivacySafeDocument(confirmedWithoutCompletion)).toBe(true);
    expect(restoredConfirmed.confirmation).toBe("pending");
    expect(restoredConfirmed.unresolvedIssuesAcknowledged).toBe(false);
    expect(restoredConfirmed.reservationNote).toBe("");
    expect(restoredConfirmed.fmResult).toBeNull();
    expect(restoredConfirmed.branchSnapshot).toBeNull();

    const reservedWithoutRules = currentDocumentJson();
    const reservedState = reservedWithoutRules.researchState as Record<string, unknown>;
    reservedState.confirmation = "reserved";
    reservedState.unresolvedIssuesAcknowledged = true;
    reservedState.reservationNote = "";
    const restoredReserved = restoreAppState(coldStartState(), reservedWithoutRules);
    expect(restoredReserved.confirmation).toBe("pending");
    expect(restoredReserved.unresolvedIssuesAcknowledged).toBe(false);
    expect(restoredReserved.reservationNote).toBe("");
  });

  it("does not restore confirmed state when rubric or public-evidence gates fail", () => {
    const weakRubric = currentDocumentJson();
    const weakState = weakRubric.researchState as Record<string, unknown>;
    weakState.confirmation = "confirmed";
    weakState.unresolvedIssuesAcknowledged = false;
    weakState.reservationNote = "";
    const rubric = weakState.rubric as Array<Record<string, unknown>>;
    rubric[0]!.score = 2;
    const restoredWeak = restoreAppState(coldStartState(), weakRubric);
    expect(restoredWeak.confirmation).toBe("pending");
    expect(restoredWeak.stage).toBe("challenge");
    expect(restoredWeak.fmResult).toBeNull();

    const nonPublic = currentDocumentJson();
    const nonPublicState = nonPublic.researchState as Record<string, unknown>;
    nonPublicState.confirmation = "confirmed";
    nonPublicState.unresolvedIssuesAcknowledged = false;
    nonPublicState.reservationNote = "";
    const evidence = nonPublicState.evidence as Array<Record<string, unknown>>;
    evidence[0]!.grade = "E";
    evidence[0]!.source_type = "commentary";
    const restoredNonPublic = restoreAppState(coldStartState(), nonPublic);
    expect(restoredNonPublic.confirmation).toBe("pending");
    expect(restoredNonPublic.stage).toBe("challenge");
    expect(restoredNonPublic.fmResult).toBeNull();
  });

  it("rejects invalid numeric and array fields at the archive boundary", () => {
    const invalidProgress = currentDocumentJson();
    (invalidProgress.researchState as Record<string, unknown>).fmProgress = Number.NaN;
    expect(canRestorePrivacySafeDocument(invalidProgress)).toBe(false);

    const invalidArrays = currentDocumentJson();
    const researchState = invalidArrays.researchState as Record<string, unknown>;
    researchState.challengeTurns = { not: "an array" };
    researchState.branches = [];
    expect(canRestorePrivacySafeDocument(invalidArrays)).toBe(false);
    expect(() => restoreAppState(coldStartState(), invalidArrays)).not.toThrow();
  });
});
