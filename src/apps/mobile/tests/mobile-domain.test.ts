import { describe, expect, it } from "vitest";
import { computeFm } from "@foresightnine/fm-engine";
import { createMarketRuntimeController } from "@foresightnine/market-data";
import { archiveIncludesPrivateEvidence, buildPrivacySafeDocument, canRestoreArchive, makeArchiveEnvelope } from "../src/archive";
import { isDuplicateEvidence, normalizeShareToBundle, normalizeSharedText } from "../src/evidence";
import { candidateToLocalBundle, safeProjectApiBase, searchProjectCandidates } from "../src/search-client";
import { configuredApiBaseUrl, configuredApiBaseUrlFromEnvironment, configuredMarketDataRuntimeConfigFromEnvironment, STAGING_API_BASE_URL } from "../src/runtime-config";
import { ASSETS, MOCK_EVIDENCE, fmInput, makeBranches, mockTurnsFor, rubricFor, validateBranchTimeline, validateProbabilityLedger } from "../src/fixtures";
import { t } from "../src/i18n";
import { createPortfolioPlan, displayAsset, publicWeights, scenarioNavFor } from "../src/portfolio";
import { canConfirmNormally, canConfirmReserved, canEnterWorldline, canUseChallengeRoute, parseCapital, validateThesis } from "../src/validation";
import { mobileInitialState, reduceChallengeAction, resetMobileWorkflowState } from "../src/lifecycle";
import { switchLanguageState } from "../src/language-state";
import { fmInput as webFmInput } from "../../web/src/fixtures";

const FRESH_FIXTURE = { runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus: "fresh" as const, fxProviderStatus: "fresh" as const, fxAsOf: "2026-08-10", fxCnyPerUnit: { CNY: 1, USD: 7.18, EUR: 7.82 } };

describe("mobile thesis and evidence boundaries", () => {
  it("keeps the staging default out of deterministic test mode while allowing explicit overrides", () => {
    expect(STAGING_API_BASE_URL).toBe("https://api-staging.foresightnine.com");
    expect(configuredApiBaseUrl()).toBeNull();
    expect(configuredApiBaseUrlFromEnvironment({ canonical: "http://127.0.0.1:8000", legacy: undefined })).toBe("http://127.0.0.1:8000");
  });

  it("prefers the canonical API variable and preserves the legacy alias", () => {
    expect(configuredApiBaseUrlFromEnvironment({ canonical: "https://canonical.example", legacy: "https://legacy.example" })).toBe("https://canonical.example");
    expect(configuredApiBaseUrlFromEnvironment({ canonical: "", legacy: "https://legacy.example" })).toBe("https://legacy.example");
    expect(configuredApiBaseUrlFromEnvironment({ canonical: "not-a-url", legacy: "https://legacy.example" })).toBeNull();
  });

  it("keeps an explicit public market mode from silently becoming mock when its URL is missing", async () => {
    let calls = 0;
    const runtime = createMarketRuntimeController({ fetcher: (async () => { calls += 1; throw new Error("must not fetch"); }) as typeof fetch });
    runtime.configure(configuredMarketDataRuntimeConfigFromEnvironment({ mode: "public" }));
    await expect(runtime.refresh({ assetIds: ["GRID"], cutoff: "2026-08-09", timezone: "UTC" })).rejects.toMatchObject({ code: "invalid_provider_url" });
    expect(calls).toBe(0);
    expect(runtime.getState()).toMatchObject({ status: "error", mode: "public", providerId: "configured-public", bundle: null });
  });
  it("does not complete or open worldline from an idle challenge action", () => {
    const completed = reduceChallengeAction(mobileInitialState, { type: "challenge_complete" });
    expect(completed).toEqual(mobileInitialState);
    expect(canEnterWorldline(completed)).toBe(false);
    expect(canUseChallengeRoute(mobileInitialState)).toBe(false);

    const started = reduceChallengeAction(mobileInitialState, { type: "start_challenge" });
    expect(started.challengeStatus).toBe("streaming");
    expect(canUseChallengeRoute(started)).toBe(true);
    expect(reduceChallengeAction(started, { type: "challenge_complete" }).challengeStatus).toBe("complete");
  });

  it("requires a legal started challenge for retry and reply completion", () => {
    expect(reduceChallengeAction(mobileInitialState, { type: "challenge_retry" })).toEqual(mobileInitialState);
    expect(reduceChallengeAction(mobileInitialState, { type: "submit_reply", body: "forged completion" })).toEqual(mobileInitialState);
    const started = reduceChallengeAction(mobileInitialState, { type: "start_challenge" });
    const failed = reduceChallengeAction(started, { type: "challenge_fail", failure: "timeout" });
    expect(failed.challengeStatus).toBe("failed");
    const recovering = reduceChallengeAction(failed, { type: "challenge_retry" });
    expect(recovering.challengeStatus).toBe("recovering");
    expect(reduceChallengeAction(recovering, { type: "submit_reply", body: "A considered response." }).challengeStatus).toBe("complete");
  });

  it("resets every downstream field before a fresh challenge", () => {
    const stale = { ...mobileInitialState, language: "en" as const, stage: "portfolio" as const, viewpoint: "old premise", challengeStatus: "complete" as const, challengeTurns: [{ id: "old", author: "user" as const, body: "old" }], replyDraft: "old reply", evidence: MOCK_EVIDENCE, selectedBranch: "strong" as const, fmStatus: "complete" as const, fmProgress: 100, fmStep: "old", fmResult: computeFm(fmInput()), portfolioMode: "raw" as const, exportNotice: "old export" };
    const fresh = resetMobileWorkflowState(stale, "new premise");
    expect(fresh).toMatchObject({ stage: "thesis", language: "en", viewpoint: "new premise", capital: stale.capital, currency: stale.currency, challengeStatus: "idle", challengeTurns: [], replyDraft: "", evidence: [], branchSnapshot: null, searchStatus: "idle", confirmation: "pending", unresolvedIssuesAcknowledged: false, reservationNote: "", selectedBranch: "main", fmStatus: "idle", fmProgress: 0, fmStep: "", fmResult: null, portfolioMode: "public", exportNotice: "" });
    expect(fresh.rubric).toEqual(rubricFor("en"));
  });

  it("rejects invalid capital and enforces the thesis range", () => {
    expect(parseCapital("0")).toBeNull();
    expect(parseCapital("12,345.67")).toBe(12345.67);
    expect(validateThesis("short", "not-money")).toMatchObject({ viewpoint: expect.any(String), capital: expect.any(String) });
    expect(validateThesis("short", "not-money", "en").viewpoint).toContain("testable premise");
  });

  it("imports a safe https share without fetching and rejects unsafe schemes", () => {
    const share = normalizeShareToBundle("A report https://example.com/research?x=1", undefined, "en");
    expect(share.ok).toBe(true);
    if (share.ok) expect(share.bundle?.url).toBe("https://example.com/research?x=1");
    expect(normalizeSharedText("javascript:alert(1)")).toMatchObject({ ok: false, reason: "unsupported_url" });
    if (share.ok && share.bundle) {
      expect(isDuplicateEvidence([share.bundle], share.bundle)).toBe(true);
      expect(share.bundle.independently_reviewable).toBe(false);
      expect(share.bundle.user_note).toContain("not independently reviewed");
    }
    expect(normalizeSharedText("ftp://example.com/report")).toMatchObject({ ok: false, reason: "unsupported_url" });
  });

  it("rejects private, credentialed, and nonstandard-port imports before public serialization", () => {
    const rejected = [
      "https://127.0.0.1/loopback",
      "https://[::1]/loopback-ipv6",
      "https://10.0.0.7/private-ipv4",
      "https://[fd00::1]/private-ipv6",
      "https://198.51.100.7/reserved-ipv4",
      "https://localhost/private-host",
      "https://user:password@example.com/credentials",
      "https://example.com:8443/unsafe-port",
    ];
    for (const input of rejected) {
      const result = normalizeShareToBundle(`Imported ${input}`, undefined, "en");
      expect(result).toMatchObject({ ok: false, reason: "invalid_url" });
      expect("bundle" in result).toBe(false);
      expect(JSON.stringify(result)).not.toContain(input);
    }
    const mixed = normalizeShareToBundle("Safe https://example.com/report and unsafe https://127.0.0.1/loopback", undefined, "en");
    expect(mixed).toMatchObject({ ok: false, reason: "invalid_url" });
    expect("bundle" in mixed).toBe(false);

    const safe = normalizeShareToBundle("Imported https://example.com:443/research", undefined, "en");
    expect(safe.ok).toBe(true);
    if (safe.ok) {
      expect(safe.bundle?.url).toBe("https://example.com/research");
      expect(JSON.stringify(safe.bundle)).toContain("https://example.com/research");
    }
  });

  it("uses bilingual dictionary strings", () => {
    expect(t("zh", "start")).toContain("审查");
    expect(t("en", "start")).toBe("Start the challenge");
    expect(rubricFor("en")[0]?.label).toBe("Premise clarity");
    expect(mockTurnsFor("en")[1]?.body).toContain("Strongest counterexample");
  });

  it("requires the plan's strict normal-confirmation gates and excludes D/E public evidence", () => {
    const strongRubric = rubricFor("en").map((item) => ({ ...item, score: 4 }));
    const publicEvidence = MOCK_EVIDENCE.filter((item) => item.grade === "B");
    const base = { ...mobileInitialState, language: "en" as const, challengeStatus: "complete" as const, challengeFailure: null, challengeTurns: [{ id: "challenger-1", author: "challenger" as const, body: "State the strongest counterexample." }, { id: "user-1", author: "user" as const, body: "I will test the order path." }], rubric: strongRubric, evidence: publicEvidence };
    expect(canConfirmNormally(base)).toBe(true);
    expect(canConfirmNormally({ ...base, evidence: MOCK_EVIDENCE.filter((item) => item.grade === "D" || item.grade === "E") })).toBe(false);
    expect(canConfirmNormally({ ...base, rubric: strongRubric.map((item) => item.id === "causal" ? { ...item, score: 2 } : item) })).toBe(false);
    expect(canConfirmNormally({ ...base, rubric: strongRubric.map((item) => item.id === "premise" ? { ...item, score: 2 } : item) })).toBe(false);
    expect(canConfirmNormally({ ...base, rubric: strongRubric.map((item) => item.id === "confirmation" ? { ...item, score: 1 } : item) })).toBe(false);
    expect(canConfirmNormally({ ...base, evidence: [{ ...publicEvidence[0]!, independently_reviewable: true, url: "" }] })).toBe(false);
    expect(canConfirmNormally({ ...base, evidence: [{ ...publicEvidence[0]!, independently_reviewable: true, grade: "D", source_type: "user_private", url: "https://example.com/private" }] })).toBe(false);
    expect(canConfirmNormally({ ...base, evidence: [{ ...publicEvidence[0]!, independently_reviewable: true, grade: "E", source_type: "model_inference", url: "" }] })).toBe(false);
  });

  it("uses only a configured HTTPS project API for candidate discovery, then preserves user-selection provenance", async () => {
    expect(safeProjectApiBase("http://api.example.com")).toBeNull();
    expect(safeProjectApiBase("https://127.0.0.1")).toBeNull();
    expect(safeProjectApiBase("https://api.example.com/base/")).toBe("https://api.example.com/base");
    const unavailable = await searchProjectCandidates("grid investment", { apiBaseUrl: "" });
    expect(unavailable).toEqual({ status: "api_unavailable", candidates: [] });
    const response = await searchProjectCandidates("grid investment", {
      apiBaseUrl: "https://api.example.com/base",
      fetcher: async (input) => {
        expect(String(input)).toContain("https://api.example.com/base/v1/search/candidates?query=grid+investment&limit=10");
        return new Response(JSON.stringify({ schema_version: "v1", provider: "glm-coding-mcp-search", project_search_available: true, fallback: "client_bundle", candidates: [{ search_result_hash: "a".repeat(64), search_provider: "glm-coding-mcp-search", title: "Safe source", url: "https://example.com/report", excerpt: "A bounded candidate.", publisher: "Example", published_at: "2026-08-01", retrieved_at: "2026-08-27T00:00:00Z", independently_reviewable: true }] }), { status: 200 });
      },
    });
    expect(response.status).toBe("available");
    expect(response.candidates).toHaveLength(1);
    const bundle = candidateToLocalBundle(response.candidates[0]!, "en");
    expect(bundle.search_provider).toBe("glm-coding-mcp-search");
    expect(bundle.search_result_hash).toBe("a".repeat(64));
    expect(bundle.independently_reviewable).toBe(false);
    expect(bundle.user_note).toContain("unverified");
  });

  it("fails closed on malformed project-search output or unavailable API status", async () => {
    for (const hash of ["result-1", "g".repeat(64)]) {
      const malformed = await searchProjectCandidates("grid", { apiBaseUrl: "https://api.example.com", fetcher: async () => new Response(JSON.stringify({ schema_version: "v1", provider: "test", project_search_available: true, fallback: "client_bundle", candidates: [{ title: "unsafe", url: "https://example.com/private", excerpt: "no", search_provider: "test", search_result_hash: hash, independently_reviewable: true }] }), { status: 200 }) });
      expect(malformed).toEqual({ status: "available", candidates: [] });
    }
    const failed = await searchProjectCandidates("grid", { apiBaseUrl: "https://api.example.com", fetcher: async () => new Response("no", { status: 502 }) });
    expect(failed).toEqual({ status: "api_failed", candidates: [] });
  });
});

describe("worldline, FM and Public Default", () => {
  it("uses one shared premise and normalized conditional probabilities in both languages", () => {
    for (const language of ["zh", "en"] as const) {
      const branches = makeBranches("A testable premise", language);
      expect(validateProbabilityLedger(branches)).toBe(true);
      expect(branches.map((branch) => branch.premise)).toEqual([branches[0]!.premise, branches[0]!.premise, branches[0]!.premise]);
      expect(branches.reduce((total, branch) => total + branch.conditionalPoint, 0)).toBeCloseTo(1, 12);
      for (const branch of branches) {
        expect(branch.premise.low).toBeLessThan(branch.premise.high);
        expect(branch.joint.low).toBeCloseTo(branch.premise.low * branch.conditional.low, 12);
        expect(branch.joint.high).toBeCloseTo(branch.premise.high * branch.conditional.high, 12);
      }
    }
    expect(makeBranches("A testable premise", "en")[1]?.title).toBe("Main W₀");
  });

  it("keeps the worldline timeline cutoff-safe and auditable", () => {
    for (const branch of makeBranches("A testable premise")) {
      expect(validateBranchTimeline(branch)).toBe(true);
      expect(branch.dataAsOf <= branch.baselineDate).toBe(true);
      expect(branch.nodes.every((node) => node.dataAsOf === branch.dataAsOf && node.occurrenceDate <= node.revealDate && node.observableIndicators.length > 0 && node.assetAnchors.length > 0 && node.sourceRefs.length > 0)).toBe(true);
      expect(branch.conditional.low).toBeLessThan(branch.conditional.high);
    }
  });

  it("executes the shared client FM engine and applies public constraints separately", () => {
    const result = computeFm(fmInput());
    expect(result.weights.weights).toHaveLength(6);
    expect(result.selectedFactorIds).toEqual([10, 20, 30]);
    const safe = publicWeights(result.weights.weights, undefined, FRESH_FIXTURE);
    expect(safe).toHaveLength(6);
    expect(safe[4]).toBe(0); // stale / low-liquidity WORLD is excluded from Public Default
    expect(Math.max(...ASSETS.filter((asset) => asset.assetClass !== "cash").map((asset, index) => safe[ASSETS.indexOf(asset)]!))).toBeLessThanOrEqual(0.2500001);
    expect(ASSETS.reduce((total, asset, index) => total + (asset.volatility === "high" ? safe[index]! : 0), 0)).toBeLessThanOrEqual(0.3500001);
    const plan = createPortfolioPlan(result, 100, "CNY", undefined, "zh", "main", FRESH_FIXTURE);
    expect(plan.rows.some((row) => row.reasons.some((reason) => reason.includes("资金太小")))).toBe(true);
    expect(plan.publicMarketValue).toBeCloseTo(100, 9);
    expect(plan.rawMarketValue).toBeCloseTo(100, 9);
    expect(plan.publicCashRemainder).toBeGreaterThanOrEqual(0);
    expect(plan.rows.find((row) => row.asset.id === "CASH")?.publicAmount).toBeGreaterThan(0);
    expect(displayAsset(plan.rows[0]!.asset, "en").name).toBe("Grid equipment ETF");
    const englishPlan = createPortfolioPlan(result, 100, "CNY", undefined, "en", "main", FRESH_FIXTURE);
    expect(englishPlan.rows.some((row) => row.reasons.some((reason) => reason.includes("Capital is too small")))).toBe(true);
  });

  it("keeps the Android fixture and FM result exactly aligned with Web", () => {
    const mobileInput = fmInput();
    const webInput = webFmInput();
    expect(mobileInput).toEqual(webInput);

    const mobileResult = computeFm(mobileInput);
    const webResult = computeFm(webInput);
    expect(mobileResult.selectedFactorIds).toEqual(webResult.selectedFactorIds);
    expect(mobileResult.weights.weights).toEqual(webResult.weights.weights);
    expect(mobileResult.combination.combined).toEqual(webResult.combination.combined);
    expect(mobileResult.forecasts).toEqual(webResult.forecasts);
  });

  it("enforces the high-volatility boundary and sends residual value to cash", () => {
    const concentrated = Float64Array.from([0.05, 0.45, 0.05, 0.4, 0, 0.05]);
    const safe = publicWeights(concentrated, undefined, FRESH_FIXTURE);
    expect(safe.reduce((total, value) => total + value, 0)).toBeCloseTo(1, 12);
    expect(ASSETS.reduce((total, asset, index) => total + (asset.volatility === "high" ? safe[index]! : 0), 0)).toBeCloseTo(0.35, 12);
    expect(ASSETS.filter((asset) => asset.assetClass !== "cash").every((asset) => safe[ASSETS.indexOf(asset)]! <= 0.25 + 1e-12)).toBe(true);
    expect(safe[ASSETS.findIndex((asset) => asset.id === "CASH")]!).toBeGreaterThan(concentrated[5]!);
  });

  it("does not invent NAV, return or drawdown when assumptions are absent", () => {
    for (const kind of ["weak", "main", "strong"] as const) {
      const scenario = scenarioNavFor(kind);
      expect(scenario.fixtureState).toBe("user_assumptions");
      expect(scenario.status).toBe("missing_inputs");
      expect(scenario.points).toEqual([]);
      expect(scenario.scenarioReturn).toBeNull();
      expect(scenario.maxDrawdown).toBeNull();
    }
  });});

describe("privacy-safe archive envelope", () => {
  it("excludes private evidence by default and makes an explicit device-only opt-in visible", () => {
    const source = { title: "local", viewpoint: "private premise", capital: 100, currency: "CNY", selectedBranch: "main", evidence: MOCK_EVIDENCE };
    const publicDocument = buildPrivacySafeDocument(source);
    const localDocument = buildPrivacySafeDocument(source, true);
    const englishDocument = buildPrivacySafeDocument({ ...source, language: "en" });
    expect(archiveIncludesPrivateEvidence(publicDocument)).toBe(false);
    expect(archiveIncludesPrivateEvidence(localDocument)).toBe(true);
    expect(englishDocument.disclaimer).toContain("scenario research");
    const envelope = makeArchiveEnvelope({ aad: "f9-archive-envelope-v1", combined: "c2VhbGVk", createdAt: "2026-08-23T00:00:00Z", documentHash: "0".repeat(64) });
    expect(canRestoreArchive(envelope)).toBe(true);
    expect(canRestoreArchive({ ...envelope, algorithm: "none" })).toBe(false);
  });

  it("requires the complete challenge, explicit acknowledgement and a note for reserved confirmation", () => {
    const base = { ...mobileInitialState, challengeStatus: "complete" as const, challengeFailure: null, challengeTurns: [{ id: "challenger-1", author: "challenger" as const, body: "Counterargument" }, { id: "user-1", author: "user" as const, body: "Response" }], unresolvedIssuesAcknowledged: false, reservationNote: "" };
    expect(canConfirmReserved(base)).toBe(false);
    expect(canConfirmReserved({ ...base, unresolvedIssuesAcknowledged: true })).toBe(false);
    expect(canConfirmReserved({ ...base, unresolvedIssuesAcknowledged: true, reservationNote: " still unresolved " })).toBe(true);
    expect(canConfirmReserved({ ...base, challengeStatus: "streaming", unresolvedIssuesAcknowledged: true, reservationNote: "note" })).toBe(false);
  });

  it("keeps worldline and FM routes closed until confirmation succeeds", () => {
    const challengeTurns = [{ id: "challenger-1", author: "challenger" as const, body: "Counterargument" }, { id: "user-1", author: "user" as const, body: "Response" }];
    const ready = { ...mobileInitialState, challengeStatus: "complete" as const, challengeFailure: null, challengeTurns, rubric: rubricFor("zh").map((item) => ({ ...item, score: 4 })), evidence: MOCK_EVIDENCE };
    expect(canEnterWorldline({ ...ready, confirmation: "pending" })).toBe(false);
    expect(canEnterWorldline({ ...ready, challengeStatus: "streaming", confirmation: "reserved" })).toBe(false);
    expect(canEnterWorldline({ ...ready, confirmation: "confirmed" })).toBe(true);
    expect(canEnterWorldline({ ...ready, confirmation: "reserved", unresolvedIssuesAcknowledged: true, reservationNote: "Response remains reserved." })).toBe(true);
  });

  it("keeps empty evidence empty in privacy-safe archive/export builders", () => {
    const document = buildPrivacySafeDocument({ title: "empty", viewpoint: "test premise", capital: 100, currency: "CNY", selectedBranch: "main", evidence: [] });
    expect(document.evidence).toEqual([]);
    expect(document.researchState).toBeUndefined();
    expect(JSON.stringify(document)).not.toContain("fixture-evidence");
  });

  it("preserves confirmed lifecycle state while localizing rubric scores", () => {
    const confirmed = { ...mobileInitialState, stage: "worldline" as const, language: "zh" as const, challengeStatus: "complete" as const, challengeFailure: null, challengeTurns: [{ id: "challenger-1", author: "challenger" as const, body: "反方问题" }, { id: "user-1", author: "user" as const, body: "用户回复" }], rubric: rubricFor("zh").map((item) => ({ ...item, score: 4 })), evidence: MOCK_EVIDENCE, confirmation: "confirmed" as const };
    const switched = switchLanguageState(confirmed, "en");
    expect(switched).toMatchObject({ language: "en", stage: "worldline", confirmation: "confirmed", fmStatus: "idle", fmResult: null });
    expect(switched.rubric[0]).toMatchObject({ id: "premise", label: "Premise clarity", score: 4 });
  });

  it("preserves a valid portfolio lifecycle and downgrades an unverifiable one on language switch", () => {
    const portfolio = { ...mobileInitialState, stage: "portfolio" as const, language: "zh" as const, challengeStatus: "complete" as const, challengeFailure: null, challengeTurns: [{ id: "challenger-1", author: "challenger" as const, body: "反方问题" }, { id: "user-1", author: "user" as const, body: "用户回复" }], rubric: rubricFor("zh").map((item) => ({ ...item, score: 4 })), evidence: MOCK_EVIDENCE, confirmation: "confirmed" as const, fmStatus: "complete" as const, fmProgress: 100, fmResult: computeFm(fmInput()) };
    const switchedPortfolio = switchLanguageState(portfolio, "en");
    expect(switchedPortfolio.stage).toBe("portfolio");
    expect(switchedPortfolio.confirmation).toBe("confirmed");
    expect(switchedPortfolio.rubric[0]?.label).toBe("Premise clarity");

    const forged = { ...portfolio, fmStatus: "idle" as const, fmResult: null };
    const downgraded = switchLanguageState(forged, "en");
    expect(downgraded).toMatchObject({ language: "en", stage: "worldline", confirmation: "confirmed", fmStatus: "idle", fmResult: null });
  });
});
