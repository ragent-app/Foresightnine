import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ChallengePanel, selectedCandidateDraft } from "./components/ChallengePanel";
import { LanguageProvider } from "./i18n";
import { canConfirmNormally, canConfirmReserved, initialState, reducer, rubricDefaults, rubricDefaultsEn, totalRubricScore } from "./state";
import { privateEvidenceFixture, searchEvidenceFixture } from "./fixtures";
import type { ChallengeStatus, EvidenceDraft, RubricView, SearchCandidate, SearchEvidenceBundle, TranscriptTurn } from "./types";

type PanelProps = Parameters<typeof ChallengePanel>[0];
type Language = "zh" | "en";

const noop = () => undefined;
const ZH_REGION_LABELS = ["审查进度", "审查对话", "你的答复", "证据工作台", "决策门槛"] as const;
const EN_REGION_LABELS = ["Review progress", "Challenge conversation", "Your answer", "Evidence workspace", "Decision gate"] as const;
const REGION_IDS = ["challenge-progress-heading", "challenge-dialogue-heading", "challenge-reply-heading", "evidence-workspace-heading", "decision-thresholds-heading"] as const;
const BLOCKED_REASON_ZH = "决策门槛未满足";
const BLOCKED_REASON_EN = "Decision gate is blocked";

const challengeTurns: readonly TranscriptTurn[] = [
  { id: "user-1", author: "user", time: "现在", body: "电网投资会随数据中心需求增长而加速。" },
  { id: "challenger-1", author: "challenger", time: "现在", body: "哪些可观察证据会证明这条因果链正在发生？" },
];
const structuredChallengeTurns: readonly TranscriptTurn[] = [
  challengeTurns[0]!,
  {
    ...challengeTurns[1]!,
    body: "Flattened fallback must not be the primary presentation.",
    review: {
      objections: ["降息也可能意味着衰退，需求方向并不唯一。", "资本开支增长未必转化为盈利增长。"],
      missingEvidence: ["历史降息周期的行业盈利数据。"],
      questions: ["什么指标和阈值会在十八个月内证伪观点？", "当前估值已经反映多少增长预期？"],
    },
  },
];
const completedTurns: readonly TranscriptTurn[] = [
  ...challengeTurns,
  { id: "user-reply-3", author: "user", time: "现在", body: "我会用订单、并网和现金流数据验证。" },
];
const englishCompletedTurns: readonly TranscriptTurn[] = [
  { id: "user-1", author: "user", time: "Now", body: "Grid investment accelerates with data-centre demand." },
  { id: "challenger-1", author: "challenger", time: "Now", body: "Which observable evidence would prove the causal chain?" },
  { id: "user-reply-3", author: "user", time: "Now", body: "I will validate it with orders, interconnection and cash-flow data." },
];
const readyRubric: readonly RubricView[] = rubricDefaults.map((item) => ({ ...item, score: item.max }));
const readyRubricEn: readonly RubricView[] = rubricDefaultsEn.map((item) => ({ ...item, score: item.max }));

const defaultPanelProps: PanelProps = {
  status: "idle",
  turns: [],
  replyDraft: "",
  evidence: [],
  rubric: rubricDefaults,
  evidenceSubmitting: false,
  unresolvedIssuesAcknowledged: false,
  reservationNote: "",
  onStart: noop,
  onCancel: noop,
  onRetry: noop,
  onRecover: noop,
  onReplyDraft: noop,
  onSubmitReply: noop,
  onEvidence: noop,
  onUnresolvedIssuesAcknowledged: noop,
  onReservationNote: noop,
  onConfirm: noop,
};

function renderPanel(overrides: Partial<PanelProps> = {}, language: Language = "zh"): string {
  return renderToStaticMarkup(createElement(
    LanguageProvider,
    { language, setLanguage: noop },
    createElement(ChallengePanel, { ...defaultPanelProps, ...overrides }),
  ));
}

function escapedRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function expectButtonState(markup: string, label: string, disabled: boolean): void {
  const match = markup.match(new RegExp(`<button\\b[^>]*>${escapedRegExp(label)}</button>`));
  if (match === null) throw new Error(`button not found: ${label}`);
  expect(match[0].includes('disabled=""'), label).toBe(disabled);
}

function expectNamedRegions(markup: string, labels: readonly string[]): void {
  labels.forEach((label, index) => {
    const id = REGION_IDS[index];
    if (id === undefined) throw new Error(`missing region id for ${label}`);
    const hasDirectLabel = markup.includes(`aria-label="${label}"`);
    const hasLabelledBy = markup.includes(`aria-labelledby="${id}"`) && markup.includes(`id="${id}">${label}`);
    expect(hasDirectLabel || hasLabelledBy, label).toBe(true);
  });
}

function expectProgressRail(markup: string): void {
  const progressRail = markup.match(/<ol[^>]*class="challenge-steps"[^>]*>[\s\S]*?<\/ol>/);
  const progressbar = markup.match(/<[^>]*role="progressbar"[^>]*>/);
  if (progressRail === null || progressbar === null) throw new Error("ChallengePanel must expose an accessible progress rail");
  expect(progressRail[0].match(/<li\b/g)?.length).toBe(4);
  expect(progressbar[0]).toMatch(/aria-valuenow="\d+"/);
  expect(progressbar[0]).toMatch(/aria-valuemax="4"/);
}

function confirmationState(overrides: Partial<typeof initialState> = {}) {
  return {
    challengeStatus: "complete" as const,
    challengeTurns: completedTurns,
    rubric: readyRubric,
    evidence: [searchEvidenceFixture],
    language: "zh" as const,
    unresolvedIssuesAcknowledged: false,
    reservationNote: "",
    ...overrides,
  };
}

describe("ChallengePanel information architecture", () => {
  it("exposes named Chinese regions, a real progress affordance, and an explicit next action", () => {
    const markup = renderPanel({ replyDraft: "尚未开始的草稿" });

    expectNamedRegions(markup, ZH_REGION_LABELS);
    expectProgressRail(markup);
    expect(markup).toMatch(/aria-label="下一步(?:操作)?"/);
    expect(markup).toContain("等待开始");
  });

  it("keeps the idle state closed while making the start action obvious", () => {
    const markup = renderPanel({ replyDraft: "不能在未开始时提交" });

    expect(markup).toContain("等待开始");
    expect(markup).toContain("先提交一个观点");
    expectButtonState(markup, "开始反方审查", false);
    expectButtonState(markup, "提交回复", true);
    expectButtonState(markup, "继续生成世界线", true);
    expectButtonState(markup, "带保留意见确认", true);
  });

  it("makes the receiving phase of streaming progress and cancellation visible", () => {
    const markup = renderPanel({ status: "streaming", turns: [{ id: "api-user-pending", author: "user", time: "现在", body: "电网投资会随数据中心需求增长而加速。" }], replyDraft: "不能在审查员回复前提交" });

    expect(markup).toContain("审查中 · 正在接收回复");
    expect(markup).toContain("正在接收回复，可随时取消。");
    expect(markup).toContain('aria-live="polite"');
    expectButtonState(markup, "取消审查", false);
    expectButtonState(markup, "提交回复", true);
    expectButtonState(markup, "继续生成世界线", true);
    expectButtonState(markup, "带保留意见确认", true);
  });

  it("makes the reply phase of streaming actionable after a challenger turn", () => {
    const markup = renderPanel({ status: "streaming", turns: challengeTurns, replyDraft: "我会提供验证条件。" });

    expect(markup).toContain("等待你的答复");
    expect(markup).toContain('aria-label="下一步"');
    expect(markup).toContain("回答审查员 ↓");
    expectButtonState(markup, "提交回复", false);
    expectButtonState(markup, "继续生成世界线", true);
    expectButtonState(markup, "带保留意见确认", true);
  });

  it("renders objections, evidence gaps, and answerable questions as separate scannable lists", () => {
    const markup = renderPanel({ status: "complete", turns: structuredChallengeTurns });

    expect(markup).toContain("关键反驳");
    expect(markup).toContain("需要你回答");
    expect(markup).toContain("待补证据");
    expect(markup.match(/class="review-list/g)).toHaveLength(3);
    expect(markup).not.toContain("Flattened fallback must not be the primary presentation.");
  });

  it("keeps the ten-item rubric available without forcing it into the primary reading path", () => {
    const markup = renderPanel({ status: "complete", turns: completedTurns });

    expect(markup).toContain('<details class="rubric-disclosure">');
    expect(markup).toContain("查看 10 项评分详情");
    expect(markup).not.toContain('<details class="rubric-disclosure" open="">');
  });

  it("locks the composer while one API reply is in flight", () => {
    const readyState = { ...initialState, stage: "challenge" as const, challengeStatus: "streaming" as const, challengeTurns, replyDraft: "我会提供验证条件。" };
    const submitting = reducer(readyState, { type: "begin_reply_submit", body: readyState.replyDraft });
    const duplicate = reducer(submitting, { type: "begin_reply_submit", body: readyState.replyDraft });
    expect(submitting.challengeStatus).toBe("submitting_reply");
    expect(duplicate).toEqual(submitting);

    const markup = renderPanel({ status: submitting.challengeStatus, turns: challengeTurns, replyDraft: readyState.replyDraft });
    expect(markup).toContain("正在提交答复");
    expect(markup).toContain("此请求完成前不会重复发送");
    expect(markup).toMatch(/<textarea[^>]*aria-label="回答挑战"[^>]*disabled=""/);
    expectButtonState(markup, "正在提交答复", true);
    expectButtonState(markup, "取消审查", false);
  });

  it("prevents evidence submission while a reviewer operation is still running", () => {
    const markup = renderPanel({ status: "submitting_reply", turns: structuredChallengeTurns, replyDraft: "正在发送的答复" });

    expectButtonState(markup, "添加证据", true);
    expect(markup).toContain("审查请求完成后即可提交证据");
  });

  it("keeps evidence locked until the user has answered the first reviewer turn", () => {
    const markup = renderPanel({ status: "complete", turns: structuredChallengeTurns });

    expectButtonState(markup, "添加证据", true);
    expect(markup).toContain("请先回答至少一轮反方问题，再提交证据");
  });

  it("shows and locks a distinct evidence-submission state", () => {
    const markup = renderPanel({
      status: "complete",
      turns: completedTurns,
      ...({ evidenceSubmitting: true } as unknown as Partial<PanelProps>),
    });

    expectButtonState(markup, "证据提交中…", true);
    expect(markup).toContain("证据正在验证并更新审查，请稍候");
  });

  it("keeps a cancelled challenge recoverable and preserves the reply gate", () => {
    const markup = renderPanel({ status: "cancelled", turns: completedTurns, replyDraft: "保留的草稿" });

    expect(markup).toContain("已取消生成");
    expect(markup).toContain("已接收内容和证据仍保留");
    expectButtonState(markup, "重试", false);
    expectButtonState(markup, "提交回复", true);
    expectButtonState(markup, "继续生成世界线", true);
    expectButtonState(markup, "带保留意见确认", true);
  });

  it.each([
    ["failed", "模型暂时不可用", "恢复失败。你可以重试", "重试", "alert"],
    ["recovering", "连接中断，正在从上次事件恢复", "连接中断，正在从上次事件恢复", "立即恢复", "status"],
  ] as const)("renders %s as a recoverable state with a closed decision gate", (status, header, notice, action, role) => {
    const markup = renderPanel({ status: status as ChallengeStatus, turns: challengeTurns, replyDraft: "恢复期间的草稿" });

    expect(markup).toContain(header);
    expect(markup).toContain(notice);
    expect(markup).toContain(`role="${role}"`);
    expectButtonState(markup, action, false);
    expectButtonState(markup, "继续生成世界线", true);
    expectButtonState(markup, "带保留意见确认", true);
  });
});

describe("ChallengePanel decision gate", () => {
  it("explains why a completed but under-specified review cannot progress", () => {
    const markup = renderPanel({ status: "complete", turns: completedTurns, replyDraft: "" });

    expect(markup).toContain("审查完成 · 第 4 轮");
    expect(markup).toMatch(/aria-label="决策门槛"|aria-labelledby="decision-thresholds-heading"/);
    expect(markup).toContain("仍需完成");
    expect(markup).toContain("将评分提高到 30/40");
    expect(markup).toContain("可独立复核");
    expectButtonState(markup, "继续生成世界线", true);
    expectButtonState(markup, "带保留意见确认", true);
  });

  it("enables only the confirmation path whose authoritative gate is satisfied", () => {
    expect(totalRubricScore(readyRubric)).toBe(40);
    expect(canConfirmNormally(confirmationState())).toBe(true);

    const normalMarkup = renderPanel({ status: "complete", turns: completedTurns, rubric: readyRubric, evidence: [searchEvidenceFixture] });
    expectButtonState(normalMarkup, "继续生成世界线", false);
    expectButtonState(normalMarkup, "带保留意见确认", true);
    expect(normalMarkup).not.toContain(BLOCKED_REASON_ZH);

    const reservedState = confirmationState({ rubric: rubricDefaults, evidence: [privateEvidenceFixture], unresolvedIssuesAcknowledged: true, reservationNote: "公开订单仍需后续验证。" });
    expect(canConfirmReserved(reservedState)).toBe(true);
    const reservedMarkup = renderPanel({ status: "complete", turns: completedTurns, rubric: rubricDefaults, evidence: [privateEvidenceFixture], unresolvedIssuesAcknowledged: true, reservationNote: "公开订单仍需后续验证。" });
    expectButtonState(reservedMarkup, "继续生成世界线", true);
    expectButtonState(reservedMarkup, "带保留意见确认", false);
  });
});

describe("ChallengePanel bilingual contract", () => {
  it("renders the same information architecture in English instead of adding Chinese-only affordances", () => {
    const markup = renderPanel({ status: "complete", turns: englishCompletedTurns, rubric: readyRubricEn, replyDraft: "" }, "en");

    expectNamedRegions(markup, EN_REGION_LABELS);
    expectProgressRail(markup);
    expect(markup).toContain('aria-label="Next action"');
    expect(markup).toContain("Review complete · round 4");
    expect(markup).toContain(BLOCKED_REASON_EN);
    expect(markup).toContain("Still needed");
    expect(markup).toContain("Raise the rubric total to 30/40");
    expect(markup).toContain("independently reviewable");
    expectButtonState(markup, "Continue to worldlines", true);
    expectButtonState(markup, "Confirm with reservations", true);

    for (const label of ZH_REGION_LABELS) expect(markup).not.toContain(label);
    expect(markup).not.toContain("等待开始");
    expect(markup).not.toContain("继续生成世界线");
  });
});

describe("ChallengePanel evidence boundary", () => {
  const candidate: SearchCandidate = {
    search_result_hash: "a".repeat(64),
    search_provider: "project-search",
    title: "Grid investment filing",
    url: "https://example.com/filing",
    excerpt: "A dated public excerpt.",
    publisher: "Example regulator",
    published_at: "2026-08-01T00:00:00Z",
    retrieved_at: "2026-08-09T00:00:00Z",
    independently_reviewable: true,
  };
  const emptyDraft: EvidenceDraft = { url: "", title: "", publisher: "", published_at: "", excerpt: "", user_note: "", supports_or_challenges: "supports", grade: "B" };

  it("keeps a search candidate unverified until an explicit SearchEvidenceBundle is submitted", () => {
    const selected = selectedCandidateDraft(emptyDraft, candidate, "en");

    expect(selected).toMatchObject({ url: candidate.url, title: candidate.title, search_provider: candidate.search_provider, search_result_hash: candidate.search_result_hash, grade: "C", supports_or_challenges: "context" });
    expect(selected.user_note).toContain("unverified");
    expect(selected).not.toHaveProperty("content_hash");
    expect(selected).not.toHaveProperty("independently_reviewable");

    const candidateOnlyState = reducer({ ...initialState, stage: "challenge", challengeStatus: "complete", challengeTurns: completedTurns, rubric: readyRubric }, { type: "set_reply_draft", draft: "candidate provenance is only a draft" });
    expect(candidateOnlyState.evidence).toEqual([]);
    const explicitlySubmitted = reducer(candidateOnlyState, { type: "add_evidence", evidence: searchEvidenceFixture });
    expect(explicitlySubmitted.evidence).toEqual([searchEvidenceFixture]);
  });

  it("preserves SearchEvidenceBundle safety fields and refuses unsafe or private evidence for normal confirmation", () => {
    const bundle: SearchEvidenceBundle = searchEvidenceFixture;
    expect(bundle).toMatchObject({ evidence_id: expect.any(String), challenge_id: expect.any(String), source_type: "reliable_secondary", url: "https://www.iea.org/", title: expect.any(String), publisher: expect.any(String), published_at: expect.any(String), retrieved_at: expect.any(String), excerpt: expect.any(String), user_note: expect.any(String), content_hash: expect.stringMatching(/^[0-9a-f]{64}$/), grade: "B", supports_or_challenges: "supports", independently_reviewable: true });
    expect(privateEvidenceFixture).toMatchObject({ source_type: "user_private", url: "", grade: "D", independently_reviewable: false });

    const unsafeEvidence = { ...searchEvidenceFixture, evidence_id: "unsafe-evidence", url: "http://127.0.0.1/private", independently_reviewable: true };
    expect(canConfirmNormally(confirmationState({ evidence: [unsafeEvidence] }))).toBe(false);
    expect(canConfirmNormally(confirmationState({ evidence: [privateEvidenceFixture] }))).toBe(false);
    expect(canConfirmReserved(confirmationState({ rubric: rubricDefaults, evidence: [privateEvidenceFixture], unresolvedIssuesAcknowledged: true, reservationNote: "私有访谈仍需公开证据交叉验证。" }))).toBe(true);
  });
});
