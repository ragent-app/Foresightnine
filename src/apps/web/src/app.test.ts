import { describe, expect, it } from "vitest";
import { assertProbabilityAssessment, assertWorldlineDates } from "@foresightnine/domain";
import { computeFm, mountFmWorker, type FmWorkerPort, type FmWorkerResponse } from "@foresightnine/fm-engine";
import { ASSET_FIXTURES, FX_SNAPSHOT, MARKET_DATA_AS_OF, PUBLIC_CONSTRAINTS, branches, evidenceHash, fmInput, isEvidenceIndependentlyReviewable, makeBranches, privateEvidenceFixture, searchEvidenceFixture, sourceMaterialHash } from "./fixtures";
import { allocationRows, canConfirmNormally, initialState, publicWeights, reducer, rubricDefaults, totalRubricScore } from "./state";
import { CSV_HEADER, csvExport, navExport, researchExport } from "./exports";
import { validateSearchEvidence, validateThesis } from "./validation";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NavPanel } from "./components/NavPanel";
import { ChallengePanel } from "./components/ChallengePanel";
import { PortfolioPanel } from "./components/PortfolioPanel";
import { WorldlinePanel } from "./components/WorldlinePanel";
import { historical } from "./nav-data";

describe("Web thesis validation", () => {
  it("rejects empty, overlong, and non-positive inputs", () => {
    expect(validateThesis({ viewpoint: "", capital: "0", currency: "CNY" })).toEqual({ viewpoint: "请先写下你的观点。", capital: "请输入大于 0 的可投资金额。" });
    expect(validateThesis({ viewpoint: "a".repeat(2001), capital: "1000", currency: "CNY" }).viewpoint).toContain("2,000");
    expect(validateThesis({ viewpoint: "valid", capital: "1,000", currency: "CNY" })).toEqual({});
  });
});

describe("worldline fixture semantics", () => {
  it("keeps conditional probabilities exhaustive and reveals every node on its node date", () => {
    const generated = makeBranches("可观察的电网投资观点");
    expect(generated.reduce((sum, branch) => sum + branch.assessment.conditional_probability, 0)).toBe(1);
    for (const branch of generated) {
      assertProbabilityAssessment(branch.assessment);
      assertWorldlineDates(branch.worldline);
      expect(branch.worldline.event_reveal_schedule.map((reveal) => reveal.event_id)).toEqual(branch.worldline.nodes.map((node) => node.node_id));
      expect(branch.worldline.event_reveal_schedule.every((reveal) => reveal.event_date === branch.worldline.nodes.find((node) => node.node_id === reveal.event_id)?.event_date)).toBe(true);
    }
  });
});

describe("challenge recovery state", () => {
  it("uses all ten rubric criteria and fail-closed normal confirmation", () => {
    expect(rubricDefaults).toHaveLength(10);
    expect(rubricDefaults.every((item) => item.max === 4)).toBe(true);
    let state = reducer({ ...initialState, viewpoint: "带门槛的观点" }, { type: "start_challenge" });
    state = reducer(state, { type: "challenge_complete" });
    expect(canConfirmNormally(state)).toBe(false);
    expect(reducer(state, { type: "confirm", confirmation: "confirmed" }).stage).toBe("challenge");
    state = reducer(state, { type: "submit_reply", body: "我会提供公开验证条件。" });
    state = reducer(state, { type: "add_evidence", evidence: searchEvidenceFixture });
    expect(totalRubricScore(state.rubric)).toBeGreaterThanOrEqual(30);
    expect(canConfirmNormally(state)).toBe(true);
    expect(reducer(state, { type: "confirm", confirmation: "confirmed" }).stage).toBe("worldline");
  });

  it("retains transcript across cancel/retry and reaches confirmation", () => {
    let state = reducer(initialState, { type: "start_challenge" });
    expect(state.challengeStatus).toBe("streaming");
    expect(state.challengeTurns.length).toBe(2);
    state = reducer(state, { type: "challenge_cancel" });
    expect(state.challengeStatus).toBe("cancelled");
    expect(state.challengeTurns.at(-1)?.body).toContain("仍保留");
    state = reducer(state, { type: "challenge_recover" });
    state = reducer(state, { type: "challenge_complete" });
    state = reducer(state, { type: "confirm", confirmation: "reserved" });
    expect(state.stage).toBe("worldline");
    expect(state.confirmation).toBe("reserved");
  });

  it("uses the current viewpoint, retains an editable reply, and keeps evidence fields", () => {
    const viewpoint = "欧洲电网储能投资会因可再生能源并网而加速";
    let state = reducer({ ...initialState, viewpoint }, { type: "start_challenge" });
    expect(state.challengeTurns[0]?.body).toBe(viewpoint);
    expect(state.challengeTurns[1]?.body).toContain(viewpoint);
    state = reducer(state, { type: "set_reply_draft", draft: "我会用订单和并网数据验证。" });
    state = reducer(state, { type: "submit_reply", body: state.replyDraft });
    expect(state.challengeTurns.at(-1)?.body).toContain("订单和并网数据");
    state = reducer(state, { type: "add_evidence", evidence: searchEvidenceFixture });
    expect(state.evidence[0]).toMatchObject({ url: "https://www.iea.org/", publisher: expect.any(String), published_at: expect.any(String), user_note: expect.any(String), content_hash: expect.stringMatching(/^[0-9a-f]{64}$/) });
  });
});

describe("SearchEvidenceBundle validation and premise rendering", () => {
  it("rejects unsafe URLs and overlong fields while accepting a safe bundle", () => {
    const valid = { url: "https://example.com/report", title: "公开报告", publisher: "Example", published_at: "2026-08-09", excerpt: "可复核摘录", user_note: "fixture note", supports_or_challenges: "supports" as const, grade: "B" as const };
    expect(validateSearchEvidence(valid)).toEqual({});
    expect(validateSearchEvidence({ ...valid, url: "http://127.0.0.1/private" }).url).toContain("http(s)");
    expect(validateSearchEvidence({ ...valid, url: "http://2130706433/private" }).url).toContain("http(s)");
    expect(validateSearchEvidence({ ...valid, url: "http://127.1/private" }).url).toContain("http(s)");
    expect(validateSearchEvidence({ ...valid, url: "http://[::1]/private" }).url).toContain("http(s)");
    expect(validateSearchEvidence({ ...valid, url: "https://example.com:8443/report" }).url).toContain("http(s)");
    expect(validateSearchEvidence({ ...valid, publisher: "" }).publisher).toContain("发布者");
    expect(validateSearchEvidence({ ...valid, published_at: "2026-08-10" }).published_at).toContain("数据截止日");
    expect(validateSearchEvidence({ ...valid, excerpt: "x".repeat(4001) }).excerpt).toContain("4,000");
    expect(validateSearchEvidence({ ...valid, grade: "D", url: "", published_at: "" })).toEqual({});
    expect(validateSearchEvidence({ ...valid, grade: "E", url: "", published_at: "" })).toEqual({});
    expect(validateSearchEvidence({ ...valid, grade: "A", url: "" }).url).toContain("A–C");
    expect(validateSearchEvidence({ ...valid, grade: "B", url: "" }).url).toContain("A–C");
    expect(validateSearchEvidence({ ...valid, grade: "C", url: "" }).url).toContain("A–C");
  });

  it("binds identity, provenance, direction, grade, and reviewability into the content hash", () => {
    const item = { evidence_id: "e1", challenge_id: "c1", source_type: "primary" as const, url: "https://example.com", title: "t", publisher: "p", published_at: "2026-08-09", retrieved_at: "2026-08-10T00:00:00Z", excerpt: "e", user_note: "n", grade: "A" as const, supports_or_challenges: "supports" as const, independently_reviewable: true };
    expect(evidenceHash(item)).not.toBe(evidenceHash({ ...item, grade: "D" }));
    expect(evidenceHash(item)).not.toBe(evidenceHash({ ...item, supports_or_challenges: "challenges" }));
    expect(evidenceHash(item)).not.toBe(evidenceHash({ ...item, source_type: "commentary" }));
    expect(evidenceHash(item)).not.toBe(evidenceHash({ ...item, challenge_id: "c2" }));
  });

  it("keeps the API source-material hash separate and canonical", () => {
    const material = { url: "https://example.com/evidence", title: "Client search excerpt", published_at: "2026-08-01T00:00:00Z", retrieved_at: "2026-08-10T00:00:00Z", excerpt: "A client-submitted excerpt.", user_note: "Search fallback." };
    expect(sourceMaterialHash(material)).toBe("cd3d44755bda8101435b8d21841f2297c1a14bc9ce1427a71b3a60269869f317");
    expect(sourceMaterialHash({ ...material, url: "", published_at: null })).toBe(sourceMaterialHash({ ...material, url: null, published_at: null }));
    expect(evidenceHash({ evidence_id: "e1", challenge_id: "c1", source_type: "primary", url: material.url, title: material.title, publisher: "Example", published_at: material.published_at, retrieved_at: material.retrieved_at, excerpt: material.excerpt, user_note: material.user_note, grade: "A", supports_or_challenges: "supports", independently_reviewable: true })).not.toBe(sourceMaterialHash(material));
  });

  it("marks D/E and private/model evidence as not independently reviewable", () => {
    expect(privateEvidenceFixture.url).toBe("");
    expect(privateEvidenceFixture.independently_reviewable).toBe(false);
    expect(isEvidenceIndependentlyReviewable("D", "user_private", "https://example.com/private")).toBe(false);
    expect(isEvidenceIndependentlyReviewable("E", "model_inference", "https://example.com/inference")).toBe(false);
    expect(isEvidenceIndependentlyReviewable("B", "reliable_secondary", "https://example.com/report")).toBe(true);
    expect(isEvidenceIndependentlyReviewable("C", "commentary", "")).toBe(false);
    expect(researchExport({ ...initialState, evidence: [privateEvidenceFixture] }, branches, []).evidence).toEqual([expect.objectContaining({ url: "", grade: "D", independently_reviewable: false })]);
  });

  it("renders editable reply/evidence schema and the viewpoint premise", () => {
    const viewpoint = "亚洲电网投资受数据中心负荷驱动";
    const challengeMarkup = renderToStaticMarkup(createElement(ChallengePanel, { status: "complete", turns: [{ id: "t1", author: "challenger", time: "现在", body: "问题" }], replyDraft: "", evidence: [], rubric: initialState.rubric, onStart: () => undefined, onCancel: () => undefined, onRetry: () => undefined, onRecover: () => undefined, onReplyDraft: () => undefined, onSubmitReply: () => undefined, onEvidence: () => undefined, onConfirm: () => undefined }));
    expect(challengeMarkup).toContain("回答挑战");
    expect(challengeMarkup).toContain("SearchEvidenceBundle");
    expect(challengeMarkup).toContain("A–C 必填；D/E 可留空");
    expect(challengeMarkup).toContain("发布日期");
    const worldlineMarkup = renderToStaticMarkup(createElement(WorldlinePanel, { branches: makeBranches(viewpoint), selected: "main", onSelect: () => undefined, onCompute: () => undefined }));
    expect(worldlineMarkup).toContain(viewpoint);
  });
});

describe("FM worker and public delta", () => {
  it("keeps the completed FM audit state visible before portfolio navigation", () => {
    let state = reducer(initialState, { type: "fm_start" });
    state = reducer(state, { type: "fm_complete", result: computeFm(fmInput()) });
    expect(state.stage).toBe("fm");
    expect(state.fmStatus).toBe("complete");
    expect(state.fmProgress).toBe(100);
  });

  it("computes through the real worker contract and retains raw weights", () => {
    const messages: FmWorkerResponse[] = [];
    const port: FmWorkerPort = { onmessage: null, postMessage: (message) => messages.push(message) };
    const cleanup = mountFmWorker(port);
    port.onmessage?.({ data: { type: "compute", requestId: "test-1", input: fmInput() } });
    expect(messages[0]?.type).toBe("result");
    if (messages[0]?.type === "result") {
      expect(messages[0].result.rawClientResult.targetWeights.length).toBe(6);
      expect(messages[0].result.cutoff).toBe("2026-08-09");
    }
    cleanup();
  });

  it("caps and renormalises Public Default without mutating Raw", () => {
    const raw = [0.8, 0.1, 0.1];
    const publicised = publicWeights(raw);
    expect(raw).toEqual([0.8, 0.1, 0.1]);
    expect(publicised[0]).toBeLessThanOrEqual(PUBLIC_CONSTRAINTS.singleSecurityMax);
    expect(publicised.reduce((sum, weight) => sum + weight, 0)).toBeCloseTo(1);
  });

  it("enforces Public caps, eligibility, proxy reasons, and deterministic redistribution", () => {
    const rows = allocationRows(computeFm(fmInput()), 100000, "CNY", "public");
    const nonCash = rows.filter((row) => row.id !== "CASH");
    const highVolTotal = rows.filter((row) => row.volatility === "high").reduce((sum, row) => sum + row.publicWeight, 0);
    expect(rows.reduce((sum, row) => sum + row.publicWeight, 0)).toBeCloseTo(1);
    expect(nonCash.every((row) => row.publicWeight <= PUBLIC_CONSTRAINTS.singleSecurityMax + 1e-12)).toBe(true);
    expect(highVolTotal).toBeLessThanOrEqual(PUBLIC_CONSTRAINTS.highVolatilityClassMax + 1e-12);
    expect(rows.find((row) => row.id === "WORLD")?.publicWeight).toBe(0);
    expect(rows.find((row) => row.id === "WORLD")?.constraintReasons.some((reason) => reason.code === "stale_data" || reason.code === "low_liquidity")).toBe(true);
    expect(rows.find((row) => row.id === "GRID")?.constraintReasons.some((reason) => reason.code === "proxy_mapping")).toBe(true);
  });

  it("changes amounts by FX currency and keeps Raw/Public lot calculations independent", () => {
    const fmResult = computeFm(fmInput());
    const cny = allocationRows(fmResult, 100000, "CNY", "raw");
    const usd = allocationRows(fmResult, 100000, "USD", "raw");
    const eur = allocationRows(fmResult, 100000, "EUR", "raw");
    expect(cny[0]?.currency).toBe("CNY");
    expect(usd[0]?.currency).toBe("USD");
    expect(eur[0]?.currency).toBe("EUR");
    expect(cny[0]?.rawAmount).not.toBe(usd[0]?.rawAmount);
    expect(usd[0]?.rawAmount).not.toBe(eur[0]?.rawAmount);
    expect(FX_SNAPSHOT.label).toContain("not live");
    expect(FX_SNAPSHOT.asOf).toBe(MARKET_DATA_AS_OF);
    const raw = allocationRows(fmResult, 100000, "CNY", "raw");
    const publicRows = allocationRows(fmResult, 100000, "CNY", "public");
    expect(raw.some((row, index) => row.units !== publicRows[index]?.units)).toBe(true);
    expect(raw.reduce((sum, row) => sum + row.amount, 0)).not.toBe(publicRows.reduce((sum, row) => sum + row.amount, 0));
    expect(publicRows.every((row) => row.currency === "CNY")).toBe(true);
  });

  it("rejects future-dated assets from Public Default", () => {
    const futureAssets = ASSET_FIXTURES.map((asset, index) => index === 0 ? { ...asset, asOf: "2026-08-10" } : asset);
    const weights = publicWeights([0.8, 0.1, 0.1, 0, 0, 0], futureAssets);
    expect(weights[0]).toBe(0);
    expect(weights.reduce((sum, weight) => sum + weight, 0)).toBeCloseTo(1);
  });
});

describe("portfolio residual and export", () => {
  it("shows lot residual for small capital and exports JSON/CSV", () => {
    const rows = allocationRows(computeFm(fmInput()), 100);
    expect(rows.some((row) => row.residualReason)).toBe(true);
    const state = { ...initialState, viewpoint: "test thesis", capital: "100", evidence: [] };
    const json = researchExport(state, branches, rows);
    const csv = csvExport(rows);
    expect(json).toMatchObject({ labels: { historical: "historical_only", simulated: "simulated fixture" } });
    expect(csv.split("\n")[0]).toBe(CSV_HEADER.join(","));
    expect(csv.split("\n")[0]).toContain("currency");
    expect(csv.split("\n")[0]).toContain("raw_amount");
    expect(csv.split("\n")[0]).toContain("public_units");
    expect(csv).toContain("金额过小");
  });

  it("keeps no-FM allocation and export explicitly empty", () => {
    expect(allocationRows(null, 100000, "CNY", "raw")).toEqual([]);
    expect(allocationRows(null, 100000, "CNY", "public")).toEqual([]);
    const output = researchExport({ ...initialState, viewpoint: "未计算" }, branches, []);
    expect(output.portfolio).toEqual({ status: "not_computed", mode: "raw", currency: "CNY", allocations: [], note: expect.stringContaining("FM") });
    expect(JSON.stringify(output.portfolio)).not.toContain("weight");
    const markup = renderToStaticMarkup(createElement(PortfolioPanel, { mode: "raw", currency: "CNY", rows: [], capital: 100000, selectedBranch: "main", branches, stress: "main", onMode: () => undefined, onStress: () => undefined, onCompute: () => undefined, onNav: () => undefined, onExport: () => undefined, fmReady: false }));
    expect(markup).toContain("尚未计算 FM");
    expect(markup).not.toContain("GRID");
  });

  it("keeps the selected portfolio mode visually and semantically aligned", () => {
    const markup = renderToStaticMarkup(createElement(PortfolioPanel, { mode: "public", currency: "CNY", rows: allocationRows(computeFm(fmInput()), 100000, "CNY", "public"), capital: 100000, selectedBranch: "main", branches, stress: "main", onMode: () => undefined, onStress: () => undefined, onCompute: () => undefined, onNav: () => undefined, onExport: () => undefined, fmReady: true }));
    expect(markup).toContain('aria-pressed="true"');
    expect(markup).toContain("◉ Public Default");
    expect(markup).toContain("○ FM Raw");
    expect(markup).toContain("组合资产分配 · Public Default");
  });

  it("exports complete audit fields and actual local FM arrays", () => {
    const fmResult = computeFm(fmInput());
    const state = { ...initialState, viewpoint: "测试观点进入完整导出", capital: "100000", currency: "USD" as const, portfolioMode: "public" as const, challengeStatus: "complete" as const, challengeTurns: [{ id: "reply-1", author: "user" as const, time: "现在", body: "用户回复保留" }], evidence: [searchEvidenceFixture], fmResult };
    const rows = allocationRows(fmResult, 100000, "USD", "public");
    const output = researchExport(state, makeBranches(state.viewpoint), rows);
    expect(output).toMatchObject({ product: { name: "ForesightNine" }, method_version: expect.any(String), data_as_of: MARKET_DATA_AS_OF, thesis: { raw_text: state.viewpoint, base_currency: "USD" }, challenge: { status: "complete", transcript: state.challengeTurns }, labels: { historical: "historical_only", simulated: "simulated fixture", probability: "model_estimate_unvalidated" } });
    expect(output.evidence).toEqual([expect.objectContaining({ evidence_id: expect.any(String), challenge_id: expect.any(String), url: expect.any(String), title: expect.any(String), publisher: expect.any(String), published_at: expect.any(String), retrieved_at: expect.any(String), excerpt: expect.any(String), user_note: expect.any(String), content_hash: expect.any(String), source_material_hash: expect.any(String), grade: expect.any(String), source_type: expect.any(String), independently_reviewable: expect.any(Boolean) })]);
    expect(output.worldlines).toHaveLength(3);
    expect(output.portfolio).toEqual(expect.objectContaining({ currency: "USD", allocations: expect.any(Array), raw_residual_cash: expect.any(Number), public_residual_cash: expect.any(Number) }));
    expect(output.fm).toEqual(expect.objectContaining({ status: "computed", fixture: true, data_source: expect.stringContaining("client"), result: expect.objectContaining({ selectedFactorIds: expect.any(Array), metrics: expect.anything(), weights: expect.any(Object), forecasts: expect.any(Array), rawClientResult: expect.any(Object) }) }));
    const noFm = researchExport({ ...initialState, viewpoint: "未计算" }, branches, allocationRows(null, 100));
    expect(noFm.fm).toEqual(expect.objectContaining({ status: "not_computed", fixture: true, result: null }));
    expect(noFm.portfolio).toEqual(expect.objectContaining({ status: "not_computed", allocations: [] }));
  });
});

describe("NAV chart paths", () => {
  const renderNav = (selected: "weak" | "main" | "strong") => renderToStaticMarkup(createElement(NavPanel, { branches, selected, onSelect: () => undefined, stress: "main", onExport: () => undefined }));

  it("renders history, all three scenario lines, interval, labels, and today boundary", () => {
    const markup = renderNav("main");
    expect(markup).toContain("interval-band");
    expect(markup).toContain("interval-high");
    expect(markup).toContain("interval-low");
    expect(markup).toContain("series-history");
    expect(markup).toContain("series-weak");
    expect(markup).toContain("series-main");
    expect(markup).toContain("series-strong");
    expect(markup).toContain("today-line");
    expect(markup).toContain("弱情景 W−");
    expect(markup).toContain("主情景 W₀");
    expect(markup).toContain("强情景 W+");
    expect(markup).toContain("10–90% 区间");
  });

  it("changes emphasis without removing other scenario paths", () => {
    const weakMarkup = renderNav("weak");
    expect(weakMarkup).toContain("series-group-weak is-emphasized");
    expect(weakMarkup).toContain("series-group-main is-context");
    expect(weakMarkup).toContain("series-group-strong is-context");
    expect(weakMarkup).toContain("组合回撤：历史与三条模拟路径");
  });

  it("exports the page arrays and toggles drawdown visibility", () => {
    const nav = navExport({ ...initialState, selectedBranch: "main" }, branches);
    expect(nav).toEqual(expect.objectContaining({ historical: expect.any(Array), weak: expect.any(Array), main: expect.any(Array), strong: expect.any(Array), interval: expect.objectContaining({ low: expect.any(Array), high: expect.any(Array) }), drawdown: expect.objectContaining({ weak: expect.any(Array), main: expect.any(Array), strong: expect.any(Array) }), boundary: expect.objectContaining({ todayIndex: historical.length - 1 }) }));
    let state = reducer(initialState, { type: "toggle_drawdown" });
    expect(state.drawdownVisible).toBe(false);
    state = reducer(state, { type: "toggle_drawdown" });
    expect(state.drawdownVisible).toBe(true);
    const hidden = renderToStaticMarkup(createElement(NavPanel, { branches, selected: "main", onSelect: () => undefined, stress: "main", onExport: () => undefined, drawdownVisible: false, onToggleDrawdown: () => undefined }));
    expect(hidden).toContain("显示回撤");
    expect(hidden).toContain("回撤图已隐藏");
  });
});
