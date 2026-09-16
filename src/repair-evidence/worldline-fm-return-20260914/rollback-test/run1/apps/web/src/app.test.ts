import { describe, expect, it, vi } from "vitest";
import { assertProbabilityAssessment, assertWorldlineDates } from "@foresightnine/domain";
import { computeFm, mountFmWorker, type FmWorkerPort, type FmWorkerResponse } from "@foresightnine/fm-engine";
import { ASSET_FIXTURES, FX_SNAPSHOT, MARKET_DATA_AS_OF, PUBLIC_CONSTRAINTS, branches, evidenceHash, fmInput, isEvidenceIndependentlyReviewable, makeBranches, privateEvidenceFixture, searchEvidenceFixture, sourceMaterialHash } from "./fixtures";
import { allocationRows, canConfirmNormally, canConfirmReserved, canEnterStage, canSubmitReply, initialState, publicWeights, reducer, rubricDefaults, rubricDefaultsEn, safeStageFor, totalRubricScore } from "./state";
import { authoritativeChallengeFetchOptions, cancelAcceptedPendingReceipt, createGenerationGuard, operationReplayCursor, recoveryActionFor, replayPendingReceipt } from "./App";
import { CSV_HEADER, csvExport, navExport, researchExport } from "./exports";
import { isSafeEvidenceUrl, validateSearchEvidence, validateThesis } from "./validation";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NavPanel } from "./components/NavPanel";
import { ChallengePanel } from "./components/ChallengePanel";
import { PortfolioPanel } from "./components/PortfolioPanel";
import { WorldlinePanel } from "./components/WorldlinePanel";
import { ThesisInput } from "./components/ThesisInput";
import { LanguageProvider, t } from "./i18n";
import { SocialLoginPanel } from "./components/SocialLoginPanel";
import { historical } from "./nav-data";

const FRESH_FIXTURE = { runtimeAsOf: "2026-08-10T00:00:00Z", providerStatus: "fresh" as const, fxProviderStatus: "fresh" as const, fxAsOf: "2026-08-10", fxCnyPerUnit: { CNY: 1, USD: 7.18, EUR: 7.82 } };

describe("authoritative challenge result fetch", () => {
  it("binds the SSE context operation id to the complete-result request", () => {
    const signal = new AbortController().signal;
    expect(authoritativeChallengeFetchOptions({ operation_id: "operation/current" }, signal)).toEqual({ operationId: "operation/current", signal });
  });
});

describe("Web thesis validation", () => {
  it("rejects empty, overlong, and non-positive inputs", () => {
    expect(validateThesis({ viewpoint: "", capital: "0", currency: "CNY" })).toEqual({ viewpoint: "请先写下你的观点。", capital: "请输入大于 0 的可投资金额。" });
    expect(validateThesis({ viewpoint: "a".repeat(2001), capital: "1000", currency: "CNY" }).viewpoint).toContain("2,000");
    expect(validateThesis({ viewpoint: "valid", capital: "1,000", currency: "CNY" })).toEqual({});
  });

  it("rejects trailing-dot spellings of loopback and local hosts", () => {
    for (const url of [
      "https://localhost./report",
      "https://service.localhost./report",
      "https://127.0.0.1./loopback",
      "https://127.1./loopback",
      "https://10.0.0.7./private",
      "https://[::1]./loopback",
    ]) expect(isSafeEvidenceUrl(url)).toBe(false);
    expect(isSafeEvidenceUrl("https://example.com./report")).toBe(true);
  });
});

describe("bilingual presentation", () => {
  it("translates export notices emitted by the runtime", () => {
    const notices = [
      "JSON 已导出；当前观点、挑战回复和证据均保留。",
      "CSV 已导出；这是 allocation-only 快照，完整审计字段请使用 JSON。",
      "路径数据已导出，并标记了当前 premise、完整 NAV 数组与历史 / 模拟边界。",
      "方法说明已导出。",
    ];
    for (const notice of notices) {
      expect(t("zh", notice)).toBe(notice);
      expect(t("en", notice)).not.toBe(notice);
    }
  });

  it("changes product copy to English while preserving user-entered thesis text", () => {
    const thesis = "AI data-centre power demand may accelerate grid investment";
    const markup = renderToStaticMarkup(createElement(LanguageProvider, { language: "en", setLanguage: () => undefined }, createElement(ThesisInput, { viewpoint: thesis, capital: "100000", currency: "USD", draftRestored: false, onViewpoint: () => undefined, onCapital: () => undefined, onCurrency: () => undefined, onStart: () => undefined, onReset: () => undefined })));
    expect(markup).toContain("Turn a thesis into auditable worldlines");
    expect(markup).toContain("Investable capital");
    expect(markup).toContain(thesis);
    expect(t("en", `「${thesis}」按部分路径兑现，设备与基础设施投资逐步验证。`)).toContain(thesis);
    expect(reducer(initialState, { type: "set_language", language: "en" }).language).toBe("en");
  });

  it("rebuilds the English rubric when resetting or starting a challenge", () => {
    const en = { ...initialState, language: "en" as const, viewpoint: "An English thesis with a testable premise", capital: "100000" };
    expect(reducer(en, { type: "reset" }).rubric).toEqual(rubricDefaultsEn);
    expect(reducer(en, { type: "start_challenge" }).rubric).toEqual(rubricDefaultsEn);
    expect(reducer({ ...en, stage: "challenge", rubric: rubricDefaults }, { type: "set_viewpoint", viewpoint: "A fresh English thesis" }).rubric).toEqual(rubricDefaultsEn);
    expect(reducer(en, { type: "reset" }).rubric.every((item) => !/[\u4e00-\u9fff]/.test(`${item.label} ${item.rationale}`))).toBe(true);
  });

  it("translates the complete challenge decision surface", () => {
    expect(["审查评分", "继续生成世界线", "带保留意见确认", "支持", "反驳", "背景", "。"].map((copy) => t("en", copy))).toEqual([
      "Challenge score",
      "Continue to worldlines",
      "Confirm with reservations",
      "Supports",
      "Challenges",
      "Context",
      ".",
    ]);
  });

  it("renders Google and GitHub as accurately unavailable until the server supplies OAuth readiness", () => {
    const markup = renderToStaticMarkup(createElement(LanguageProvider, { language: "en", setLanguage: () => undefined }, createElement(SocialLoginPanel)));
    expect(markup).toContain("Anonymous research");
    expect(markup).toContain("Google");
    expect(markup).toContain("GitHub");
    expect(markup).toContain("Not configured");
    expect(markup).toContain("disabled=\"\"");
  });
});

describe("worldline fixture semantics", () => {
  it("keeps conditional probabilities exhaustive and uses auditable event/reveal dates", () => {
    const generated = makeBranches("可观察的电网投资观点");
    expect(generated.reduce((sum, branch) => sum + branch.assessment.conditional_probability, 0)).toBe(1);
    for (const branch of generated) {
      assertProbabilityAssessment(branch.assessment);
      assertWorldlineDates(branch.worldline);
      expect(branch.worldline.event_reveal_schedule.map((reveal) => reveal.event_id)).toEqual(branch.worldline.nodes.map((node) => node.node_id));
      expect(branch.worldline.event_reveal_schedule.every((reveal) => reveal.event_date === branch.worldline.nodes.find((node) => node.node_id === reveal.event_id)?.event_date)).toBe(true);
      expect(branch.worldline.horizon_end).toBe("2028-08-31");
      expect(branch.worldline.nodes.find((node) => node.event_date === "2028-08-10") && branch.worldline.event_reveal_schedule.find((reveal) => reveal.event_date === "2028-08-10")?.reveal_date).toBe("2028-08-31");
      expect(branch.worldline.event_reveal_schedule.some((reveal) => reveal.event_date !== reveal.reveal_date && reveal.reveal_date >= reveal.event_date)).toBe(true);
    }
  });
});

describe("challenge recovery state", () => {
  it("fails closed for idle or unstarted reply submissions in the reducer and UI", () => {
    expect(canSubmitReply("idle", [])).toBe(false);
    expect(reducer(initialState, { type: "submit_reply", body: "forged reply" })).toEqual(initialState);

    const forged = { ...initialState, stage: "challenge" as const, challengeStatus: "idle" as const, challengeTurns: [{ id: "user-1", author: "user" as const, time: "现在", body: "Forged" }, { id: "challenger-1", author: "challenger" as const, time: "现在", body: "Forged" }] };
    expect(reducer(forged, { type: "submit_reply", body: "still forged" })).toEqual(forged);

    const markup = renderToStaticMarkup(createElement(ChallengePanel, { status: "idle", turns: [], replyDraft: "forged reply", evidence: [], rubric: initialState.rubric, unresolvedIssuesAcknowledged: false, reservationNote: "", onStart: () => undefined, onCancel: () => undefined, onRetry: () => undefined, onRecover: () => undefined, onReplyDraft: () => undefined, onSubmitReply: () => undefined, onEvidence: () => undefined, onUnresolvedIssuesAcknowledged: () => undefined, onReservationNote: () => undefined, onConfirm: () => undefined }));
    expect(markup).toMatch(/<button[^>]*disabled=""[^>]*>提交回复<\/button>/);
  });

  it("allows one reply after an authoritative model operation completes, then exposes confirmation", () => {
    const initialTurns = [
      { id: "user-1", author: "user" as const, time: "now", body: "Initial thesis" },
      { id: "challenger-1", author: "challenger" as const, time: "now", body: "Counterargument" },
    ];
    expect(canSubmitReply("complete", initialTurns)).toBe(true);
    const repliedTurns = [...initialTurns, { id: "user-2", author: "user" as const, time: "now", body: "Evidence-based answer" }];
    expect(canSubmitReply("complete", repliedTurns)).toBe(false);
  });

  it("fully resets a completed and confirmed workflow when the viewpoint changes", () => {
    const oldViewpoint = "旧观点：电网设备投资会加速";
    let state = reducer({ ...initialState, viewpoint: oldViewpoint, capital: "250000", currency: "USD" }, { type: "start_challenge" });
    state = reducer(state, { type: "challenge_complete" });
    state = reducer(state, { type: "submit_reply", body: "公开订单数据将验证这条路径。" });
    state = reducer(state, { type: "add_evidence", evidence: searchEvidenceFixture });
    state = reducer(state, { type: "confirm", confirmation: "confirmed" });
    state = reducer(state, { type: "select_branch", branch: "strong" });
    state = reducer(state, { type: "fm_start" });
    state = reducer(state, { type: "fm_progress", progress: 65, step: "计算中" });
    state = reducer(state, { type: "fm_complete", result: computeFm(fmInput()) });
    state = reducer(state, { type: "set_mode", mode: "public" });
    state = reducer(state, { type: "set_stress", scenario: "weak" });
    state = reducer(state, { type: "export_notice", notice: "已导出" });

    const next = reducer(state, { type: "set_viewpoint", viewpoint: "新观点：储能投资会改变资产传导" });
    expect(next).toMatchObject({
      stage: "thesis",
      language: state.language,
      viewpoint: "新观点：储能投资会改变资产传导",
      capital: "250000",
      currency: "USD",
      challengeStatus: "idle",
      challengeTurns: [],
      replyDraft: "",
      rubric: rubricDefaults,
      evidence: [],
      confirmation: "pending",
      unresolvedIssuesAcknowledged: false,
      reservationNote: "",
      selectedBranch: "main",
      fmStatus: "idle",
      fmProgress: 0,
      fmStep: "等待本地计算",
      fmResult: null,
      portfolioMode: "raw",
      stressScenario: "main",
      exportNotice: "",
    });
  });

  it("fully resets a pending partial workflow and starts the new challenge cleanly", () => {
    const oldViewpoint = "旧观点：需求增长会带动订单";
    let state = reducer({ ...initialState, viewpoint: oldViewpoint, capital: "90000", currency: "EUR" }, { type: "start_challenge" });
    state = reducer(state, { type: "set_reply_draft", draft: "旧回复不应被复用" });
    state = reducer(state, { type: "add_evidence", evidence: searchEvidenceFixture });
    state = reducer(state, { type: "set_unresolved_issues_acknowledged", acknowledged: true });
    state = reducer(state, { type: "set_reservation_note", note: "旧保留意见" });
    state = reducer(state, { type: "select_branch", branch: "weak" });
    state = reducer(state, { type: "fm_start" });

    const next = reducer(state, { type: "set_viewpoint", viewpoint: "新观点：融资条件会决定传导强度" });
    expect(next.challengeStatus).toBe("idle");
    expect(next.challengeTurns).toEqual([]);
    expect(next.evidence).toEqual([]);
    expect(next.fmResult).toBeNull();
    expect(next.selectedBranch).toBe("main");
    expect(next.stage).toBe("thesis");
    expect(next.capital).toBe("90000");
    expect(next.currency).toBe("EUR");

    const started = reducer(next, { type: "start_challenge" });
    expect(started.challengeStatus).toBe("streaming");
    expect(started.challengeTurns[0]?.body).toBe("新观点：融资条件会决定传导强度");
    expect(started.challengeTurns.some((turn) => turn.body.includes("旧观点") || turn.body.includes("旧回复"))).toBe(false);
  });

  it("does not reuse downstream state when restarting a challenge for the same viewpoint", () => {
    const viewpoint = "同一观点重新审查也必须从零开始";
    let state = reducer({ ...initialState, viewpoint }, { type: "start_challenge" });
    state = reducer(state, { type: "challenge_complete" });
    state = reducer(state, { type: "submit_reply", body: "旧回复" });
    state = reducer(state, { type: "add_evidence", evidence: searchEvidenceFixture });
    state = reducer(state, { type: "select_branch", branch: "strong" });
    state = reducer(state, { type: "fm_start" });
    const restarted = reducer(state, { type: "start_challenge", viewpoint });
    expect(restarted).toMatchObject({ stage: "challenge", challengeStatus: "streaming", viewpoint, replyDraft: "", evidence: [], selectedBranch: "main", fmStatus: "idle", fmProgress: 0, fmResult: null, confirmation: "pending" });
    expect(restarted.challengeTurns[0]?.body).toBe(viewpoint);
    expect(restarted.rubric).toEqual(rubricDefaults);
    expect(restarted.challengeTurns.some((turn) => turn.body === "旧回复")).toBe(false);
  });

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

  it("rejects forged completion flags, transcripts, evidence flags, and direct route jumps", () => {
    const started = reducer({ ...initialState, viewpoint: "一个用于安全门回归的有效观点" }, { type: "start_challenge" });
    const prematurelyComplete = reducer(started, { type: "challenge_complete" });
    expect(prematurelyComplete.challengeStatus).toBe("streaming");
    const completed = reducer(prematurelyComplete, { type: "submit_reply", body: "我会用公开订单数据验证。" });
    expect(completed.challengeStatus).toBe("complete");
    expect(canConfirmNormally({ ...completed, rubric: rubricDefaults.map((item) => ({ ...item, score: 4 })), evidence: [{ ...searchEvidenceFixture, independently_reviewable: false }] })).toBe(true);
    expect(canConfirmReserved({ ...completed, unresolvedIssuesAcknowledged: true, reservationNote: "仍有未解决的传导风险。", challengeTurns: completed.challengeTurns.filter((turn) => turn.author !== "user" || turn.id === "user-1") })).toBe(false);
    const forged = { ...completed, stage: "portfolio" as const, confirmation: "confirmed" as const, challengeTurns: completed.challengeTurns.filter((turn) => turn.author !== "user" || turn.id === "user-1"), rubric: rubricDefaults.map((item) => ({ ...item, score: 4 })), evidence: [searchEvidenceFixture], fmStatus: "complete" as const, fmResult: computeFm(fmInput()) };
    expect(reducer(forged, { type: "set_stage", stage: "portfolio" }).stage).toBe("challenge");
    expect(researchExport(forged, branches, []).challenge).toMatchObject({ confirmation: "pending", unresolved_issues_acknowledged: false, reservation_note: "" });
  });

  it("retains transcript across cancel/retry and reaches confirmation", () => {
    let state = reducer({ ...initialState, viewpoint: "一个用于回归测试的有效观点" }, { type: "start_challenge" });
    expect(state.challengeStatus).toBe("streaming");
    expect(state.challengeTurns.length).toBe(2);
    state = reducer(state, { type: "challenge_cancel" });
    expect(state.challengeStatus).toBe("cancelled");
    expect(state.challengeTurns.at(-1)?.body).toContain("仍保留");
    state = reducer(state, { type: "challenge_recover" });
    state = reducer(state, { type: "challenge_complete" });
    expect(canConfirmReserved(state)).toBe(false);
    state = reducer(state, { type: "submit_reply", body: "我会继续观察订单和现金流。" });
    state = reducer(state, { type: "confirm", confirmation: "reserved" });
    expect(state.stage).toBe("challenge");
    state = reducer(state, { type: "set_unresolved_issues_acknowledged", acknowledged: true });
    state = reducer(state, { type: "set_reservation_note", note: "未验证的需求传导仍需观察订单与现金流。" });
    expect(canConfirmReserved(state)).toBe(true);
    state = reducer(state, { type: "confirm", confirmation: "reserved" });
    expect(state.stage).toBe("worldline");
    expect(state.confirmation).toBe("reserved");
  });

  it("keeps terminal recovery state intact while selecting a replacement operation instead of workflow reset", () => {
    const started = reducer({ ...initialState, viewpoint: "终态恢复不能抹去研究状态" }, { type: "start_challenge" });
    const withDraft = reducer(reducer(started, { type: "set_reply_draft", draft: "保留的答复草稿" }), { type: "add_evidence", evidence: searchEvidenceFixture });
    const cancelled = reducer(withDraft, { type: "challenge_cancel" });
    const recovered = reducer(cancelled, { type: "challenge_recover" });
    expect(recovered).toMatchObject({ viewpoint: withDraft.viewpoint, replyDraft: "保留的答复草稿", evidence: [searchEvidenceFixture], rubric: withDraft.rubric, challengeTurns: cancelled.challengeTurns });
    expect(recoveryActionFor("running")).toBe("resume");
    expect(recoveryActionFor("cancelled")).toBe("restart");
    expect(recoveryActionFor("failed")).toBe("restart");
    expect(recoveryActionFor("completed")).toBe("replay");
  });

  it("compensates a cancel requested before the async operation receipt arrives", async () => {
    const cancelOperation = vi.fn(async () => ({ operation: { status: "cancelled" } }));
    const pending = { cancelRequested: true, cancelIdempotencyKey: "cancel-idempotency-delayed", submissionIdempotencyKey: "submission-idempotency-delayed" };
    const receipt = { operation: { operation_id: "operation:delayed", cancel_token: "cancel-token-delayed" } };

    await expect(cancelAcceptedPendingReceipt({ cancelOperation } as never, pending, receipt as never)).resolves.toMatchObject({ status: "cancelled" });
    expect(cancelOperation).toHaveBeenCalledWith(
      "operation:delayed", "cancel-token-delayed", { idempotencyKey: "cancel-idempotency-delayed" },
    );

    pending.cancelRequested = false;
    await expect(cancelAcceptedPendingReceipt({ cancelOperation } as never, pending, receipt as never)).resolves.toBeNull();
    expect(cancelOperation).toHaveBeenCalledTimes(1);

    pending.cancelRequested = true;
    const nonTerminalCancel = vi.fn(async () => ({ operation: { status: "running" } }));
    await expect(cancelAcceptedPendingReceipt({ cancelOperation: nonTerminalCancel } as never, pending, receipt as never)).rejects.toThrow("terminal operation state");
  });

  it("replays a lost async receipt with the original submission idempotency key", async () => {
    const pending = { submissionIdempotencyKey: "submission-idempotency-lost-receipt" };
    const receipt = { operation: { operation_id: "operation:replayed" } };
    const submit = vi.fn()
      .mockRejectedValueOnce(new Error("response lost after acceptance"))
      .mockResolvedValueOnce(receipt);

    await expect(replayPendingReceipt(pending, submit)).rejects.toThrow("response lost after acceptance");
    await expect(replayPendingReceipt(pending, submit)).resolves.toBe(receipt);
    expect(submit).toHaveBeenNthCalledWith(1, "submission-idempotency-lost-receipt");
    expect(submit).toHaveBeenNthCalledWith(2, "submission-idempotency-lost-receipt");
  });

  it("rewinds a terminal replay receipt so the terminal SSE frame is observed", () => {
    expect(operationReplayCursor({ operation_id: "operation:done", last_event_id: "operation:done:2", status: "completed" })).toBe("operation:done:0");
    expect(operationReplayCursor({ operation_id: "operation:failed", last_event_id: "operation:failed:2", status: "failed" })).toBe("operation:failed:0");
    expect(operationReplayCursor({ operation_id: "operation:live", last_event_id: "operation:live:1", status: "running" })).toBe("operation:live:1");
  });

  it("moves a failed recovery into an explicit retryable failed state", () => {
    const started = reducer({ ...initialState, viewpoint: "恢复失败必须如实显示" }, { type: "start_challenge" });
    const recovering = reducer(reducer(started, { type: "challenge_cancel" }), { type: "challenge_recover" });
    const failed = reducer(recovering, { type: "api_error", notice: "模型供应商暂时不可用。" });
    expect(failed.challengeStatus).toBe("failed");
    expect(failed.apiError).toBe("模型供应商暂时不可用。");
    expect(failed.challengeTurns).toEqual(recovering.challengeTurns);
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
    expect(sourceMaterialHash(material)).toBe("7f334c646f625da01253b95ec362f62384cace4820d8697a324183aceec36e7e");
    expect(sourceMaterialHash({ ...material, published_at: "2026-08-01T00:00:00.000Z", retrieved_at: "2026-08-10T00:00:00.000000Z" })).toBe(sourceMaterialHash(material));
    expect(sourceMaterialHash({ ...material, url: "", published_at: null })).toBe(sourceMaterialHash({ ...material, url: null, published_at: null }));
    expect(sourceMaterialHash({ ...material, search_provider: "provider-a", search_result_hash: "a".repeat(64) })).not.toBe(sourceMaterialHash(material));
    expect(evidenceHash({ evidence_id: "e1", challenge_id: "c1", source_type: "primary", url: material.url, title: material.title, publisher: "Example", published_at: material.published_at, retrieved_at: material.retrieved_at, excerpt: material.excerpt, user_note: material.user_note, grade: "A", supports_or_challenges: "supports", independently_reviewable: true })).not.toBe(sourceMaterialHash(material));
  });

  it("exports the API source-material hash with search provenance intact", () => {
    const item = { ...searchEvidenceFixture, search_provider: "provider-a", search_result_hash: "a".repeat(64) };
    const exported = (researchExport({ ...initialState, evidence: [item] }, branches, []) as { evidence: Array<Record<string, unknown>> }).evidence[0];
    expect(exported?.challenge_id).toBe(item.challenge_id);
    expect(exported?.source_material_hash).toBe(sourceMaterialHash({
      url: item.url, title: item.title, published_at: item.published_at, retrieved_at: item.retrieved_at,
      excerpt: item.excerpt, user_note: item.user_note, search_provider: item.search_provider, search_result_hash: item.search_result_hash,
    }));
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
    const challengeMarkup = renderToStaticMarkup(createElement(ChallengePanel, { status: "complete", turns: [{ id: "t1", author: "challenger", time: "现在", body: "问题" }], replyDraft: "", evidence: [], rubric: initialState.rubric, unresolvedIssuesAcknowledged: false, reservationNote: "", onStart: () => undefined, onCancel: () => undefined, onRetry: () => undefined, onRecover: () => undefined, onReplyDraft: () => undefined, onSubmitReply: () => undefined, onEvidence: () => undefined, onUnresolvedIssuesAcknowledged: () => undefined, onReservationNote: () => undefined, onConfirm: () => undefined }));
    expect(challengeMarkup).toContain("回答挑战");
    expect(challengeMarkup).toContain("SearchEvidenceBundle");
    expect(challengeMarkup).toContain("A–C 必填；D/E 可留空");
    expect(challengeMarkup).toContain("发布日期");
    const worldlineMarkup = renderToStaticMarkup(createElement(WorldlinePanel, { branches: makeBranches(viewpoint), selected: "main", onSelect: () => undefined, onCompute: () => undefined }));
    expect(worldlineMarkup).toContain(viewpoint);
  });

  it("renders the selected branch assessment and its actual first post-baseline reveal", () => {
    const weak = makeBranches("A branch-specific premise").find((branch) => branch.kind === "weak")!;
    const markup = renderToStaticMarkup(createElement(WorldlinePanel, { branches: makeBranches("A branch-specific premise"), selected: "weak", onSelect: () => undefined, onCompute: () => undefined }));
    expect(markup).toContain("弱情景 W−");
    expect(markup).toContain(`${(weak.assessment.premise_probability_range.minimum * 100).toFixed(2)}–${(weak.assessment.premise_probability_range.maximum * 100).toFixed(2)}%`);
    expect(markup).toContain(`${(weak.assessment.conditional_probability_range.minimum * 100).toFixed(2)}–${(weak.assessment.conditional_probability_range.maximum * 100).toFixed(2)}%`);
    expect(markup).toContain(`${(weak.assessment.joint_probability_range.minimum * 100).toFixed(2)}–${(weak.assessment.joint_probability_range.maximum * 100).toFixed(2)}%`);
    expect(markup).toContain("2027-02-28");
    expect(markup).not.toContain("主世界线 P(W₀ | H)");
  });

  it("generates an English challenger body while preserving the entered premise", () => {
    const premise = "Data-centre demand accelerates grid investment";
    const state = reducer({ ...initialState, language: "en", viewpoint: premise }, { type: "start_challenge" });
    expect(state.challengeTurns[1]?.body).toContain("Strongest counterargument");
    expect(state.challengeTurns[1]?.body).toContain(premise);
    expect(state.challengeTurns[1]?.body).not.toContain("最强反方");
  });
});

describe("FM worker and public delta", () => {
  it("keeps the completed FM audit state visible before portfolio navigation", () => {
    const readyRubric = rubricDefaults.map((item) => ({ ...item, score: 3 }));
    let state = reducer({ ...initialState, stage: "worldline", viewpoint: "一个通过确认门槛的测试观点", challengeStatus: "complete", challengeTurns: [{ id: "system-1", author: "system", time: "现在", body: "挑战已启动" }, { id: "challenger-1", author: "challenger", time: "现在", body: "反方问题" }, { id: "user-1", author: "user", time: "现在", body: "用户回复" }], rubric: readyRubric, evidence: [searchEvidenceFixture], confirmation: "confirmed" }, { type: "fm_start" });
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
    const publicised = publicWeights(raw, undefined, FRESH_FIXTURE);
    expect(raw).toEqual([0.8, 0.1, 0.1]);
    expect(publicised[0]).toBeLessThanOrEqual(PUBLIC_CONSTRAINTS.singleSecurityMax);
    expect(publicised.reduce((sum, weight) => sum + weight, 0)).toBeCloseTo(1);
  });

  it("enforces Public caps, eligibility, proxy reasons, and deterministic redistribution", () => {
    const rows = allocationRows(computeFm(fmInput()), 100000, "CNY", "public", FRESH_FIXTURE);
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
    const weights = publicWeights([0.8, 0.1, 0.1, 0, 0, 0], futureAssets, { ...FRESH_FIXTURE, runtimeAsOf: "2026-08-09T00:00:00Z", fxAsOf: "2026-08-09" });
    expect(weights[0]).toBe(0);
    expect(weights.reduce((sum, weight) => sum + weight, 0)).toBeCloseTo(1);
  });

  it("fails closed for direct stage manipulation and FM starts", () => {
    const ready = { ...initialState, viewpoint: "一个用于 gate 回归的有效观点", capital: "100000" };
    expect(reducer(initialState, { type: "set_stage", stage: "worldline" }).stage).toBe("thesis");
    expect(reducer(initialState, { type: "fm_start" }).fmStatus).toBe("idle");

    let state = reducer(ready, { type: "start_challenge" });
    expect(state.stage).toBe("challenge");
    expect(reducer(state, { type: "set_stage", stage: "worldline" }).stage).toBe("challenge");
    state = reducer(state, { type: "challenge_complete" });
    state = reducer(state, { type: "submit_reply", body: "我会使用公开订单数据验证这条路径。" });
    state = reducer(state, { type: "add_evidence", evidence: searchEvidenceFixture });
    state = reducer(state, { type: "confirm", confirmation: "confirmed" });
    expect(state.stage).toBe("worldline");
    expect(reducer(state, { type: "set_stage", stage: "portfolio" }).stage).toBe("worldline");
    expect(safeStageFor({ ...state, stage: "portfolio", fmStatus: "idle", fmResult: null })).toBe("worldline");
    expect(canEnterStage(state, "fm")).toBe(true);
    expect(reducer(state, { type: "fm_start" }).stage).toBe("fm");
  });

  it("invalidates stale timer or worker results with a generation token", () => {
    const guard = createGenerationGuard();
    const first = guard.current();
    const second = guard.invalidate();
    expect(guard.isCurrent(first)).toBe(false);
    expect(guard.isCurrent(second)).toBe(true);
    const third = guard.invalidate();
    expect(guard.isCurrent(second)).toBe(false);
    expect(guard.isCurrent(third)).toBe(true);
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
    let state = reducer({ ...initialState, viewpoint: "测试观点进入完整导出", capital: "100000" }, { type: "start_challenge" });
    state = reducer(state, { type: "challenge_complete" });
    state = reducer(state, { type: "submit_reply", body: "用户回复保留" });
    state = reducer(state, { type: "add_evidence", evidence: searchEvidenceFixture });
    state = reducer(state, { type: "confirm", confirmation: "confirmed" });
    state = reducer(state, { type: "set_currency", currency: "USD" });
    state = reducer(state, { type: "fm_start" });
    state = reducer(state, { type: "fm_complete", result: fmResult });
    state = reducer(state, { type: "set_mode", mode: "public" });
    const rows = allocationRows(fmResult, 100000, "USD", "public");
    const output = researchExport(state, makeBranches(state.viewpoint), rows);
    expect(output).toMatchObject({ product: { name: "ForesightNine" }, method_version: expect.any(String), data_as_of: MARKET_DATA_AS_OF, thesis: { raw_text: state.viewpoint, base_currency: "USD" }, challenge: { status: "complete", confirmation: "confirmed", transcript: state.challengeTurns }, labels: { historical: "historical_only", simulated: "simulated fixture", probability: "model_estimate_unvalidated" } });
    expect(output.evidence).toEqual([expect.objectContaining({ evidence_id: expect.any(String), challenge_id: expect.any(String), url: expect.any(String), title: expect.any(String), publisher: expect.any(String), published_at: expect.any(String), retrieved_at: expect.any(String), excerpt: expect.any(String), user_note: expect.any(String), content_hash: expect.any(String), source_material_hash: expect.any(String), grade: expect.any(String), source_type: expect.any(String), independently_reviewable: true })]);
    expect(output.worldlines).toHaveLength(3);
    expect(output.portfolio).toEqual(expect.objectContaining({ currency: "USD", allocations: expect.any(Array), raw_residual_cash: expect.any(Number), public_residual_cash: expect.any(Number) }));
    expect(output.fm).toEqual(expect.objectContaining({ status: "computed", fixture: true, data_source: expect.stringContaining("client"), result: expect.objectContaining({ selectedFactorIds: expect.any(Array), metrics: null, weights: expect.any(Object), forecasts: expect.any(Array), rawClientResult: expect.any(Object) }) }));

    const idleWorldline = { ...state, stage: "worldline" as const, fmStatus: "idle" as const, fmResult: null };
    const idleWorldlineOutput = researchExport(idleWorldline, makeBranches(state.viewpoint), []);
    expect(idleWorldlineOutput.worldlines).toHaveLength(3);
    expect(idleWorldlineOutput.nav).toMatchObject({ status: "not_available", worldlines: [] });

    const forgedNav = { ...idleWorldline, stage: "nav" as const };
    const forgedNavOutput = researchExport(forgedNav, makeBranches(state.viewpoint), []);
    expect(forgedNavOutput.worldlines).toEqual([]);
    expect(forgedNavOutput.nav).toMatchObject({ status: "not_available", worldlines: [] });
    expect(forgedNavOutput.portfolio).toMatchObject({ status: "not_available", allocations: [] });

    const noFm = researchExport({ ...initialState, viewpoint: "未计算" }, branches, allocationRows(null, 100));
    expect(noFm.fm).toEqual(expect.objectContaining({ status: "not_computed", fixture: true, result: null }));
    expect(noFm.portfolio).toEqual(expect.objectContaining({ status: "not_computed", allocations: [] }));
  });

  it("fails closed for forged completion, rubric, evidence, and FM results", () => {
    const forged = {
      ...initialState,
      stage: "portfolio" as const,
      viewpoint: "伪造下游结果不应公开",
      challengeStatus: "complete" as const,
      confirmation: "confirmed" as const,
      rubric: rubricDefaults.map((item) => ({ ...item, score: item.max })),
      evidence: [{ ...searchEvidenceFixture, independently_reviewable: true }],
      fmStatus: "complete" as const,
      fmResult: computeFm(fmInput()),
    };
    const output = researchExport(forged, branches, allocationRows(forged.fmResult, 100000));
    expect(output.worldlines).toEqual([]);
    expect(output.nav).toMatchObject({ status: "not_available", worldlines: [] });
    expect(output.portfolio).toMatchObject({ status: "not_available", allocations: [] });
    expect(output.fm).toMatchObject({ status: "not_available", result: null });
    expect(JSON.stringify(output.fm)).not.toContain("selectedFactorIds");
  });

  it("does not export a malformed or stale client FM result", () => {
    let state = reducer({ ...initialState, viewpoint: "FM shape gate 的有效观点" }, { type: "start_challenge" });
    state = reducer(state, { type: "challenge_complete" });
    state = reducer(state, { type: "submit_reply", body: "用户确认挑战回复" });
    state = reducer(state, { type: "add_evidence", evidence: searchEvidenceFixture });
    state = reducer(state, { type: "confirm", confirmation: "confirmed" });
    state = reducer(state, { type: "fm_start" });
    state = reducer(state, { type: "fm_complete", result: computeFm(fmInput()) });
    const result = state.fmResult!;
    const malformed = { ...state, fmResult: { ...result, forecasts: new Float64Array(1) } };
    const output = researchExport(malformed, makeBranches(state.viewpoint), allocationRows(result, 100000));
    expect(output.worldlines).toHaveLength(3);
    expect(output.fm).toMatchObject({ status: "not_available", result: null });
    expect(output.portfolio).toMatchObject({ status: "not_available", allocations: [] });

    const emptyVolatility = { ...state, fmResult: { ...result, forecastScale: { ...result.forecastScale, crossSectionalVolatility: new Float64Array() } } };
    expect(researchExport(emptyVolatility, makeBranches(state.viewpoint), allocationRows(result, 100000)).fm).toMatchObject({ status: "not_available", result: null });

    const infiniteVolatility = { ...state, fmResult: { ...result, forecastScale: { ...result.forecastScale, crossSectionalVolatility: Float64Array.from([Number.POSITIVE_INFINITY]) } } };
    expect(researchExport(infiniteVolatility, makeBranches(state.viewpoint), allocationRows(result, 100000)).fm).toMatchObject({ status: "not_available", result: null });

    const wrongVolatilityContainer = { ...state, fmResult: { ...result, forecastScale: { ...result.forecastScale, crossSectionalVolatility: [0.1] as unknown as Float64Array } } };
    expect(researchExport(wrongVolatilityContainer, makeBranches(state.viewpoint), allocationRows(result, 100000)).fm).toMatchObject({ status: "not_available", result: null });

    const malformedMetrics = { ...state, fmResult: { ...result, metrics: { icSeries: Float64Array.from([Number.POSITIVE_INFINITY]), icMean: 0, icir: 0 } } };
    expect(researchExport(malformedMetrics, makeBranches(state.viewpoint), allocationRows(result, 100000)).fm).toMatchObject({ status: "not_available", result: null });
  });

  it.each([
    ["confirmed", { unresolvedIssuesAcknowledged: false, reservationNote: "" }],
    ["reserved", { unresolvedIssuesAcknowledged: true, reservationNote: "保留意见仍需后续验证。" }],
  ] as const)("requires a real transcript before exporting a %s confirmation", (confirmation, reservation) => {
    const state = {
      ...initialState,
      stage: "worldline" as const,
      viewpoint: "缺少 transcript 的状态",
      challengeStatus: "complete" as const,
      confirmation,
      rubric: rubricDefaults.map((item) => ({ ...item, score: item.max })),
      evidence: confirmation === "reserved" ? [privateEvidenceFixture] : [searchEvidenceFixture],
      ...reservation,
    };
    const output = researchExport(state, branches, []);
    expect(output.worldlines).toEqual([]);
    expect(output.nav).toMatchObject({ status: "not_available" });
    expect(output.fm).toMatchObject({ status: "not_computed", result: null });
    expect(output.portfolio).toMatchObject({ allocations: [] });
    expect(["not_available", "not_computed"]).toContain((output.portfolio as { status: string }).status);
  });

  it("derives reviewability from source, grade, and safe public URL", () => {
    const state = {
      ...initialState,
      evidence: [
        { ...searchEvidenceFixture, independently_reviewable: false },
        { ...privateEvidenceFixture, independently_reviewable: true, url: "https://example.com/private" },
        { ...privateEvidenceFixture, evidence_id: "evidence-model-no-url", source_type: "model_inference" as const, grade: "E" as const, independently_reviewable: true },
        { ...searchEvidenceFixture, evidence_id: "evidence-no-url", url: "", independently_reviewable: true },
        { ...searchEvidenceFixture, evidence_id: "evidence-unsafe-url", url: "http://127.0.0.1/private", independently_reviewable: true },
      ],
    };
    const evidence = researchExport(state, branches, []).evidence as Array<Record<string, unknown>>;
    expect(evidence.map((item) => item.independently_reviewable)).toEqual([true, false, false, false, false]);
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
