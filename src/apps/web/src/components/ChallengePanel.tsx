import { useMemo, useState } from "react";
import { Button, Field, Input, Select, StatusDot, Tag, Textarea } from "./Controls";
import { Header } from "./AppShell";
import { t, useLanguage } from "../i18n";
import { searchEvidenceCandidates } from "../search-client";
import type { ChallengeStatus, EvidenceDraft, EvidenceErrors, EvidenceView, RubricView, SearchCandidate, SearchCandidatesResult, TranscriptTurn } from "../types";
import { canConfirmNormally, canConfirmReserved, canSubmitReply, totalRubricScore } from "../state";
import { isSafeEvidenceUrl, validateSearchEvidence } from "../validation";

interface ChallengeProps {
  readonly status: ChallengeStatus;
  readonly turns: readonly TranscriptTurn[];
  readonly replyDraft: string;
  readonly evidence: readonly EvidenceView[];
  readonly rubric: readonly RubricView[];
  readonly evidenceSubmitting?: boolean;
  readonly unresolvedIssuesAcknowledged: boolean;
  readonly reservationNote: string;
  readonly onStart: () => void;
  readonly onCancel: () => void;
  readonly onRetry: () => void;
  readonly onRecover: () => void;
  readonly onReplyDraft: (draft: string) => void;
  readonly onSubmitReply: (body: string) => void;
  readonly onEvidence: (draft: EvidenceDraft) => void;
  readonly onUnresolvedIssuesAcknowledged: (acknowledged: boolean) => void;
  readonly onReservationNote: (note: string) => void;
  readonly onConfirm: (kind: "confirmed" | "reserved") => void;
}

const statusCopy: Record<ChallengeStatus, string> = { idle: "等待开始", streaming: "审查中 · 正在接收回复", submitting_reply: "正在提交答复", cancelled: "已取消生成", recovering: "连接中断，正在从上次事件恢复…", failed: "模型暂时不可用", complete: "审查完成 · 第 4 轮" };
const emptyEvidence: EvidenceDraft = { url: "", title: "", publisher: "", published_at: "2026-08-09", excerpt: "", user_note: "", supports_or_challenges: "supports", grade: "B" };

type EvidenceMode = "search" | "manual";

/** Candidate discovery stays separate from evidence creation. This is called
 * only by the person's explicit candidate-selection action. */
export function selectedCandidateDraft(current: EvidenceDraft, candidate: SearchCandidate, language: "zh" | "en"): EvidenceDraft {
  return {
    ...current,
    url: candidate.url,
    title: candidate.title,
    publisher: candidate.publisher ?? candidate.search_provider,
    published_at: candidate.published_at?.slice(0, 10) ?? "",
    excerpt: candidate.excerpt,
    user_note: language === "en" ? "User selected this unverified search candidate; review before treating it as evidence." : "用户选择了这个未经验证的搜索候选；应先核对再作为证据。",
    // Candidate metadata is unverified discovery data, so selecting it never
    // silently promotes it to primary/reliable evidence or a directional fact.
    supports_or_challenges: "context",
    grade: "C",
    search_provider: candidate.search_provider,
    search_result_hash: candidate.search_result_hash,
  };
}

function TurnContent({ turn, language }: { readonly turn: TranscriptTurn; readonly language: "zh" | "en" }) {
  const review = turn.author === "challenger" ? turn.review : undefined;
  if (!review || [...review.objections, ...review.missingEvidence, ...review.questions].length === 0) return <p>{turn.body}</p>;
  const sections = language === "en" ? [
    { key: "questions", label: "Questions to answer", items: review.questions, tone: "questions" },
    { key: "objections", label: "Core objections", items: review.objections, tone: "objections" },
    { key: "evidence", label: "Evidence gaps", items: review.missingEvidence, tone: "evidence" },
  ] : [
    { key: "questions", label: "需要你回答", items: review.questions, tone: "questions" },
    { key: "objections", label: "关键反驳", items: review.objections, tone: "objections" },
    { key: "evidence", label: "待补证据", items: review.missingEvidence, tone: "evidence" },
  ];
  return <div className="challenge-brief">{sections.filter((section) => section.items.length > 0).map((section) => <section className={`review-section review-${section.tone}`} aria-label={section.label} key={section.key}><div className="review-section-heading"><h3>{section.label}</h3><span>{section.items.length}</span></div><ol className="review-list">{section.items.map((item, index) => <li key={`${section.key}-${index}`}><span>{index + 1}</span><p>{item}</p></li>)}</ol></section>)}</div>;
}

export function ChallengePanel({ status, turns, replyDraft, evidence, rubric, evidenceSubmitting = false, unresolvedIssuesAcknowledged, reservationNote, onStart, onCancel, onRetry, onRecover, onReplyDraft, onSubmitReply, onEvidence, onUnresolvedIssuesAcknowledged, onReservationNote, onConfirm }: ChallengeProps) {
  const { language } = useLanguage();
  const total = totalRubricScore(rubric);
  const [evidenceDraft, setEvidenceDraft] = useState<EvidenceDraft>(emptyEvidence);
  const [evidenceSubmitted, setEvidenceSubmitted] = useState(false);
  const [evidenceMode, setEvidenceMode] = useState<EvidenceMode>("manual");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResult, setSearchResult] = useState<SearchCandidatesResult | null>(null);
  const [searching, setSearching] = useState(false);
  const [selectionNote, setSelectionNote] = useState("");
  const evidenceErrors: EvidenceErrors = useMemo(() => evidenceSubmitted ? validateSearchEvidence(evidenceDraft) : {}, [evidenceDraft, evidenceSubmitted]);
  const replyAllowed = canSubmitReply(status, turns);
  const latestChallengerIndex = turns.reduce((latest, turn, index) => turn.author === "challenger" ? index : latest, -1);
  const hasChallengerResponse = latestChallengerIndex >= 0;
  const hasReplyAfterLatestChallenge = latestChallengerIndex >= 0 && turns.slice(latestChallengerIndex + 1).some((turn) => turn.author === "user");
  const confirmationState = { challengeStatus: status, challengeTurns: turns, rubric, evidence, language, unresolvedIssuesAcknowledged, reservationNote };
  const normalAllowed = canConfirmNormally(confirmationState);
  const reservedAllowed = canConfirmReserved(confirmationState);
  const copy = language === "en" ? {
    progress: "Review progress", dialogue: "Challenge conversation", reply: "Your answer", evidence: "Evidence workspace", gates: "Decision gate",
    stepDialogue: "Read", stepReply: "Respond", stepEvidence: "Evidence", stepGates: "Decide",
    next: "Next action", start: "Start the review", wait: "Wait for the reviewer", answer: "Answer the reviewer", recover: "Recover the review", retry: "Retry the review", normal: "Continue normally", reserve: "Confirm with reservations",
    startDetail: "Start the challenge to receive the reviewer’s first question.", waitDetail: "The reviewer is responding. You can cancel and retain received content.", answerDetail: "Answer the latest reviewer question before making a decision.", recoverDetail: "The review stopped. Recover or retry without losing received dialogue or evidence.", normalDetail: "All normal confirmation gates are satisfied.", reserveDetail: "Review the remaining gates, then acknowledge and document any unresolved issues.",
    available: "Available now", unavailable: "Not available yet", awaiting: "Awaiting your response", submittingStatus: "Submitting your response", submittingTitle: "Submitting your response", submittingDetail: "Your response is being sent. The composer is locked until this request completes; you can cancel the review.", submittingFeedback: "Submitting your response. Duplicate submissions are blocked until this request completes.", characters: "characters", remaining: "remaining", replyHint: "A reply becomes available after a challenger question. Your draft stays here until you submit it.",
    searchTab: "Search candidates", manualTab: "Add evidence manually", evidenceCount: "evidence items", selectedProvenance: "Selected candidate provenance", source: "Search provider", candidateHash: "Candidate hash", searchResults: "Search results are discovery data. Selecting a candidate only pre-fills this bundle; it never opens a URL or imports evidence.",
    rubricScore: "Rubric score", threshold: "Normal confirmation requires 30/40, at least 3/4 in core criteria, and one safe independently reviewable A–C source.", reservedThreshold: "Reserved confirmation requires a completed dialogue, valid evidence, acknowledgement, and a reservation note.", unmet: "Still needed", completedDialogue: "Complete the reviewer dialogue.", rubricTotal: "Raise the rubric total to 30/40.", coreRubric: "Raise each core rubric criterion to at least 3/4.", publicEvidence: "Add one safe, independently reviewable A–C evidence item.", evidenceValidity: "Review evidence fields and grades before confirming.", acknowledge: "Acknowledge unresolved issues.", note: "Add a reservation note.", allMet: "This route is ready.",
    cancel: "Cancel review", retryButton: "Retry", recoverButton: "Recover now", reviewStatus: "Review status", candidateUnavailable: "Search is unavailable. You can still submit a manual SearchEvidenceBundle.", candidateEmpty: "No selectable public candidates were returned.", searchTitle: "Search source candidates", query: "Search query", search: "Search", searching: "Searching…", choose: "Use this candidate to prefill evidence", selected: "Candidate selected. Review and submit the normal evidence bundle to create evidence.", clear: "Clear selected candidate provenance", optional: "Optional. Results are unverified and are never opened or imported automatically.",
  } : {
    progress: "审查进度", dialogue: "审查对话", reply: "你的答复", evidence: "证据工作台", gates: "决策门槛",
    stepDialogue: "阅读", stepReply: "答复", stepEvidence: "证据", stepGates: "决策",
    next: "下一步", start: "开始审查", wait: "等待审查员", answer: "回答审查员", recover: "恢复审查", retry: "重试审查", normal: "正常继续", reserve: "带保留意见确认",
    startDetail: "开始审查后会收到审查员的第一个问题。", waitDetail: "审查员正在回复。你可以取消，已接收内容会保留。", answerDetail: "请回答最新的审查员问题，再作出决策。", recoverDetail: "审查已停止。恢复或重试不会丢失已接收的对话或证据。", normalDetail: "正常确认的所有门槛均已满足。", reserveDetail: "查看剩余门槛，然后确认并记录未解决问题。",
    available: "现在可用", unavailable: "暂不可用", awaiting: "等待你的答复", submittingStatus: "正在提交答复", submittingTitle: "正在提交答复", submittingDetail: "答复正在发送。此请求完成前编辑器已锁定；你可以取消审查。", submittingFeedback: "正在提交答复。此请求完成前不会重复发送。", characters: "个字符", remaining: "剩余", replyHint: "审查员提出问题后才能提交答复。草稿会保留在这里，直到你提交。",
    searchTab: "搜索候选来源", manualTab: "手动添加证据", evidenceCount: "条证据", selectedProvenance: "已选候选的来源信息", source: "搜索提供方", candidateHash: "候选哈希", searchResults: "搜索结果仅用于发现来源。选择候选只会预填此 bundle；不会打开 URL 或导入证据。",
    rubricScore: "审查评分", threshold: "正常确认需要 30/40、核心准则均至少 3/4，以及一条安全且可独立复核的 A–C 级证据。", reservedThreshold: "带保留意见确认需要完成对话、有效证据、确认未解决问题和保留意见说明。", unmet: "仍需完成", completedDialogue: "完成与审查员的对话。", rubricTotal: "将评分提高到 30/40。", coreRubric: "将每项核心准则提高到至少 3/4。", publicEvidence: "添加一条安全、可独立复核的 A–C 级证据。", evidenceValidity: "确认前请检查证据字段和等级。", acknowledge: "确认仍有未解决问题。", note: "填写保留意见说明。", allMet: "此路径已就绪。",
    cancel: "取消审查", retryButton: "重试", recoverButton: "立即恢复", reviewStatus: "审查状态", candidateUnavailable: "搜索不可用；你仍可手动提交 SearchEvidenceBundle。", candidateEmpty: "没有可选择的公开候选来源。", searchTitle: "搜索来源候选", query: "搜索问题", search: "搜索", searching: "正在搜索…", choose: "选择此候选并预填证据", selected: "已选择候选。请核对后提交常规证据 bundle，才会创建证据。", clear: "清除已选候选的来源标记", optional: "可选。结果未经验证；不会自动打开链接或导入证据。",
  };
  const replySubmitting = status === "submitting_reply";
  const reviewOperationBusy = status === "streaming" || status === "submitting_reply" || status === "recovering" || evidenceSubmitting;
  // The API deliberately creates the first reviewer result in CHALLENGING and
  // only opens the evidence command after the user has answered at least one
  // reviewer turn.  The transport status `complete` means the current request
  // finished; it is not the same as the server lifecycle being evidence-ready.
  const evidenceLifecycleReady = status === "complete" && turns.filter((turn) => turn.author === "user").length >= 2;
  const visibleStatus = replySubmitting ? copy.submittingStatus : replyAllowed ? copy.awaiting : t(language, statusCopy[status]);
  const blockedReason = language === "en" ? "Decision gate is blocked" : "决策门槛未满足";
  const expectedGrade: Readonly<Record<EvidenceView["source_type"], EvidenceView["grade"]>> = { primary: "A", reliable_secondary: "B", commentary: "C", user_private: "D", model_inference: "E" };
  const validEvidence = evidence.every((item) => (item.grade === "D" || item.grade === "E" || isSafeEvidenceUrl(item.url.trim())) && expectedGrade[item.source_type] === item.grade);
  const publicEvidence = evidence.some((item) => ["A", "B", "C"].includes(item.grade) && item.independently_reviewable && expectedGrade[item.source_type] === item.grade && isSafeEvidenceUrl(item.url.trim()));
  const coreIndices = [0, 1, 3, 6, 8];
  const normalUnmet = [
    ...(status === "complete" && turns.some((turn) => turn.author === "user") ? [] : [copy.completedDialogue]),
    ...(total >= 30 ? [] : [copy.rubricTotal]),
    ...(coreIndices.every((index) => (rubric[index]?.score ?? 0) >= 3) ? [] : [copy.coreRubric]),
    ...(validEvidence ? [] : [copy.evidenceValidity]),
    ...(publicEvidence ? [] : [copy.publicEvidence]),
  ];
  const reservedUnmet = [
    ...(status === "complete" && turns.some((turn) => turn.author === "user") ? [] : [copy.completedDialogue]),
    ...(validEvidence ? [] : [copy.evidenceValidity]),
    ...(unresolvedIssuesAcknowledged ? [] : [copy.acknowledge]),
    ...(reservationNote.trim() ? [] : [copy.note]),
  ];
  const nextAction = status === "idle" || turns.length === 0
    ? { title: copy.start, detail: copy.startDetail, button: copy.start, action: onStart, disabled: false }
    : status === "cancelled" || status === "failed"
      ? { title: status === "failed" ? copy.retry : copy.recover, detail: copy.recoverDetail, button: status === "failed" ? copy.retryButton : copy.recoverButton, action: status === "failed" ? onRetry : onRecover, disabled: false }
      : replySubmitting
      ? { title: copy.submittingTitle, detail: copy.submittingDetail, button: copy.cancel, action: onCancel, disabled: false }
      : status === "recovering" || !replyAllowed && status === "streaming"
        ? { title: copy.wait, detail: copy.waitDetail, button: copy.cancel, action: onCancel, disabled: false }
        : replyAllowed
          ? { title: copy.answer, detail: copy.answerDetail, button: copy.answer, action: undefined, disabled: false }
          : normalAllowed
            ? { title: copy.normal, detail: copy.normalDetail, button: copy.normal, action: () => onConfirm("confirmed"), disabled: false }
            : { title: copy.reserve, detail: copy.reserveDetail, button: copy.reserve, action: undefined, disabled: false };
  const submitReply = (event: React.FormEvent<HTMLFormElement>): void => { event.preventDefault(); if (replyDraft.trim() && replyAllowed) onSubmitReply(replyDraft); };
  const submitEvidence = (event: React.FormEvent<HTMLFormElement>): void => { event.preventDefault(); if (reviewOperationBusy || !evidenceLifecycleReady) return; setEvidenceSubmitted(true); if (Object.keys(validateSearchEvidence(evidenceDraft)).length === 0) onEvidence(evidenceDraft); };
  const updateEvidence = <K extends keyof EvidenceDraft>(field: K, value: EvidenceDraft[K]): void => { setEvidenceDraft((current) => ({ ...current, [field]: value })); };
  const submitSearch = (event: React.FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (!searchQuery.trim() || searching) return;
    setSearching(true); setSearchResult(null); setSelectionNote("");
    void searchEvidenceCandidates(searchQuery).then(setSearchResult).finally(() => setSearching(false));
  };
  const selectCandidate = (candidate: SearchCandidate): void => {
    setEvidenceDraft((current) => selectedCandidateDraft(current, candidate, language));
    setEvidenceSubmitted(false); setSelectionNote(copy.selected); setEvidenceMode("manual");
  };
  const clearSelectedCandidate = (): void => {
    setEvidenceDraft((current) => {
      const { search_provider: _provider, search_result_hash: _hash, ...manualDraft } = current;
      return manualDraft;
    });
    setSelectionNote("");
  };
  const completedProgressSteps = [hasChallengerResponse, hasReplyAfterLatestChallenge, evidence.length > 0, normalAllowed || reservedAllowed].filter(Boolean).length;
  const progress = [
    { label: copy.stepDialogue, complete: hasChallengerResponse },
    { label: copy.stepReply, complete: hasReplyAfterLatestChallenge },
    { label: copy.stepEvidence, complete: evidence.length > 0 },
    { label: copy.stepGates, complete: normalAllowed || reservedAllowed },
  ];

  return <section className="content-column challenge-view">
    <Header title={t(language, "反方审查")} action={<span className="operation-label"><StatusDot tone={status === "failed" ? "danger" : status === "complete" ? "sage" : "accent"} />{visibleStatus}</span>} />
    <section className="challenge-overview" aria-labelledby="challenge-progress-heading" aria-label={copy.progress}>
      <div><p className="eyebrow" id="challenge-progress-heading">{copy.progress}</p><ol className="challenge-steps" role="progressbar" aria-label={copy.progress} aria-valuemin={0} aria-valuemax={4} aria-valuenow={completedProgressSteps}>{progress.map((step, index) => <li className={step.complete ? "is-complete" : ""} key={step.label}><span>{index + 1}</span><strong>{step.label}</strong></li>)}</ol></div>
      <aside className="next-action-card" aria-label={copy.next}><p className="eyebrow">{copy.next}</p><h2>{nextAction.title}</h2><p>{nextAction.detail}</p>{nextAction.action ? <Button variant="primary" type="button" onClick={nextAction.action} disabled={nextAction.disabled}>{nextAction.button}</Button> : <a href={replyAllowed ? "#challenge-reply" : "#decision-thresholds"}>{nextAction.button} ↓</a>}</aside>
    </section>
    <section className="review-status-card" aria-label={copy.reviewStatus}>
      <div><StatusDot tone={status === "failed" ? "danger" : status === "cancelled" ? "warning" : status === "complete" ? "sage" : "accent"} /><strong>{visibleStatus}</strong></div>
      <div className="review-status-actions">{status === "streaming" && !hasChallengerResponse ? <Button variant="quiet" onClick={onCancel}>{copy.cancel}</Button> : null}{replySubmitting ? <Button variant="quiet" onClick={onCancel}>{copy.cancel}</Button> : null}{status === "cancelled" ? <><Button variant="secondary" onClick={onRecover}>{copy.recoverButton}</Button><Button variant="quiet" onClick={onRetry}>{copy.retryButton}</Button></> : null}{status === "recovering" ? <Button variant="quiet" onClick={onRecover}>{copy.recoverButton}</Button> : null}{status === "failed" ? <Button variant="secondary" onClick={onRetry}>{copy.retryButton}</Button> : null}</div>
    </section>
    <div className="challenge-layout">
      <div className="challenge-main">
        <section className="transcript" aria-labelledby="challenge-dialogue-heading" aria-label={copy.dialogue}>
          <div className="section-heading"><div><p className="eyebrow">01</p><h2 id="challenge-dialogue-heading">{copy.dialogue}</h2></div><span>{turns.length} {language === "en" ? "turns" : "轮"}</span></div>
          {turns.length === 0 ? <div className="empty-state"><p>{t(language, "先提交一个观点，反方会检查因果链、时间边界与可证伪性。")}</p><Button variant="primary" onClick={onStart}>{t(language, "开始反方审查")}</Button></div> : <div className="turn-list">{turns.map((turn) => <article className={`turn turn-${turn.author}`} key={turn.id}><div className="turn-meta"><strong>{t(language, turn.author === "user" ? "你（正方）" : turn.author === "challenger" ? "反方（AI）" : "系统")}</strong><span>{t(language, turn.time)}</span></div><TurnContent turn={turn} language={language} /></article>)}</div>}
          {status === "streaming" && !hasChallengerResponse ? <div className="streaming-line" role="status" aria-live="polite"><StatusDot />{t(language, "正在接收回复，可随时取消。")}</div> : null}
          {status === "streaming" && hasChallengerResponse ? <div hidden aria-hidden="true">{t(language, "审查中 · 正在接收回复")}<span aria-live="polite">{t(language, "正在接收回复，可随时取消。")}</span><Button variant="quiet" onClick={onCancel}>{copy.cancel}</Button></div> : null}
          {status === "cancelled" ? <div className="notice notice-warning" role="status">{t(language, "已取消生成；已接收内容和证据仍保留。")}</div> : null}
          {status === "recovering" ? <div className="notice notice-info" role="status">{t(language, "连接中断，正在从上次事件恢复…")}</div> : null}
          {status === "failed" ? <div className="notice notice-warning" role="alert">{t(language, "恢复失败。你可以重试，不会重复提交已确认内容。")}</div> : null}
        </section>
        <form className="composer" id="challenge-reply" onSubmit={submitReply} aria-labelledby="challenge-reply-heading" aria-label={copy.reply}>
          <div className="composer-heading"><div><p className="eyebrow">02</p><h2 id="challenge-reply-heading">{copy.reply}</h2></div><span className={replyAllowed ? "availability is-available" : "availability"}>{replySubmitting ? copy.submittingStatus : replyAllowed ? copy.available : copy.unavailable}</span></div>
          <label className="field"><span className="field-label">{t(language, "你的回复")}</span><Textarea aria-label={t(language, "回答挑战")} value={replyDraft} onChange={(event) => onReplyDraft(event.target.value)} maxLength={4000} placeholder={t(language, "回答反方问题，说明证据、条件或保留意见……")} disabled={!replyAllowed} /></label>
          <div className="composer-footer">{replySubmitting ? <p className="composer-submit-status" role="status" aria-live="polite">{copy.submittingFeedback}</p> : <p>{copy.replyHint}</p>}<span>{replyDraft.length}/4000 {copy.characters} · {4000 - replyDraft.length} {copy.remaining}</span><Button variant="primary" type="submit" disabled={!replyDraft.trim() || !replyAllowed}>{replySubmitting ? copy.submittingStatus : t(language, "提交回复")}</Button></div>
        </form>
      </div>
      <aside className="evidence-drawer" aria-labelledby="evidence-workspace-heading"><div className="drawer-heading"><div><p className="eyebrow">03</p><h2 id="evidence-workspace-heading">{copy.evidence}</h2></div><span>{evidence.length} {copy.evidenceCount}</span></div>
        <div className="evidence-tabs" role="tablist" aria-label={copy.evidence}><button className={evidenceMode === "search" ? "is-active" : ""} type="button" role="tab" aria-selected={evidenceMode === "search"} aria-controls="evidence-search-panel" onClick={() => setEvidenceMode("search")}>{copy.searchTab}</button><button className={evidenceMode === "manual" ? "is-active" : ""} type="button" role="tab" aria-selected={evidenceMode === "manual"} aria-controls="evidence-manual-panel" onClick={() => setEvidenceMode("manual")}>{copy.manualTab}</button></div>
        {evidenceMode === "search" ? <section className="candidate-search" id="evidence-search-panel" role="tabpanel" aria-label={copy.searchTitle}>
          <h3>{copy.searchTitle}</h3><p>{copy.optional}</p>
          <form className="candidate-search-form" onSubmit={submitSearch}><Input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} maxLength={70} aria-label={copy.query} placeholder={copy.query} /><Button variant="secondary" type="submit" disabled={!searchQuery.trim() || searching}>{searching ? copy.searching : copy.search}</Button></form>
          {searchResult && !searchResult.project_search_available ? <p className="evidence-note" role="status"><StatusDot tone="warning" />{copy.candidateUnavailable}</p> : null}
          {searchResult?.project_search_available && searchResult.candidates.length === 0 ? <p className="evidence-note" role="status">{copy.candidateEmpty}</p> : null}
          {searchResult?.candidates.map((candidate) => <article className="search-candidate" key={candidate.search_result_hash}><strong>{candidate.title}</strong><p>{candidate.publisher ?? candidate.search_provider}</p><p>{candidate.excerpt}</p><code>{candidate.url}</code><p className="candidate-provenance">{copy.source}: {candidate.search_provider} · {copy.candidateHash}: {candidate.search_result_hash}</p><Button variant="quiet" type="button" onClick={() => selectCandidate(candidate)}>{copy.choose}</Button></article>)}
          <p className="evidence-note">{copy.searchResults}</p>
        </section> : null}
        {evidenceMode === "manual" ? <form className="evidence-form" id="evidence-manual-panel" role="tabpanel" onSubmit={submitEvidence} noValidate>
          <p className="evidence-schema-note">{t(language, "SearchEvidenceBundle：A–C 必须填写安全 URL；D/E 可留空并标记为不可独立复核。标题、发布者、摘录和用户说明仍会校验；离线 fixture 也使用同一字段结构。")}</p>
          {selectionNote ? <div className="selection-provenance" role="status"><strong>{copy.selectedProvenance}</strong><p>{selectionNote}</p>{evidenceDraft.search_provider ? <p>{copy.source}: {evidenceDraft.search_provider} · {copy.candidateHash}: {evidenceDraft.search_result_hash}</p> : null}<Button variant="quiet" type="button" onClick={clearSelectedCandidate}>{copy.clear}</Button></div> : null}
          <Field label={t(language, "URL（A–C 必填；D/E 可留空）")} error={evidenceErrors.url ? t(language, evidenceErrors.url) : undefined}><Input value={evidenceDraft.url} onChange={(event) => updateEvidence("url", event.target.value)} placeholder={t(language, "https://…（D/E 可留空）")} aria-invalid={Boolean(evidenceErrors.url)} /></Field>
          <Field label={t(language, "标题")} error={evidenceErrors.title ? t(language, evidenceErrors.title) : undefined}><Input value={evidenceDraft.title} onChange={(event) => updateEvidence("title", event.target.value)} maxLength={240} aria-invalid={Boolean(evidenceErrors.title)} /></Field>
          <Field label={t(language, "发布者")} error={evidenceErrors.publisher ? t(language, evidenceErrors.publisher) : undefined}><Input value={evidenceDraft.publisher} onChange={(event) => updateEvidence("publisher", event.target.value)} maxLength={160} aria-invalid={Boolean(evidenceErrors.publisher)} /></Field>
          <Field label={t(language, "发布日期（D/E 无 URL 可留空）")} error={evidenceErrors.published_at ? t(language, evidenceErrors.published_at) : undefined}><Input type="date" value={evidenceDraft.published_at.slice(0, 10)} onChange={(event) => updateEvidence("published_at", event.target.value)} aria-invalid={Boolean(evidenceErrors.published_at)} /></Field>
          <Field label={t(language, "摘录")} error={evidenceErrors.excerpt ? t(language, evidenceErrors.excerpt) : undefined}><Textarea value={evidenceDraft.excerpt} onChange={(event) => updateEvidence("excerpt", event.target.value)} maxLength={4000} aria-invalid={Boolean(evidenceErrors.excerpt)} /></Field>
          <Field label={t(language, "用户说明")} error={evidenceErrors.user_note ? t(language, evidenceErrors.user_note) : undefined}><Textarea value={evidenceDraft.user_note} onChange={(event) => updateEvidence("user_note", event.target.value)} maxLength={1000} aria-invalid={Boolean(evidenceErrors.user_note)} /></Field>
          <div className="evidence-form-row"><Field label={t(language, "方向")}><Select value={evidenceDraft.supports_or_challenges} onChange={(event) => updateEvidence("supports_or_challenges", event.target.value as EvidenceDraft["supports_or_challenges"])}><option value="supports">{t(language, "支持")}</option><option value="challenges">{t(language, "反驳")}</option><option value="context">{t(language, "背景")}</option></Select></Field><Field label={t(language, "等级")}><Select value={evidenceDraft.grade} onChange={(event) => updateEvidence("grade", event.target.value as EvidenceDraft["grade"])}>{["A", "B", "C", "D", "E"].map((grade) => <option key={grade}>{grade}</option>)}</Select></Field></div>
          <Button variant="secondary" type="submit" disabled={reviewOperationBusy || !evidenceLifecycleReady}>{evidenceSubmitting ? (language === "en" ? "Submitting evidence…" : "证据提交中…") : t(language, "提交 SearchEvidenceBundle")}</Button>
          {reviewOperationBusy ? <p className="evidence-submit-wait" role="status">{evidenceSubmitting ? (language === "en" ? "The evidence is being validated and the review is being updated. Please wait." : "证据正在验证并更新审查，请稍候。") : (language === "en" ? "Evidence can be submitted when the current review request finishes." : "审查请求完成后即可提交证据。")}</p> : !evidenceLifecycleReady ? <p className="evidence-submit-wait" role="status">{language === "en" ? "Answer at least one reviewer turn before submitting evidence." : "请先回答至少一轮反方问题，再提交证据。"}</p> : null}
        </form> : null}
        {evidence.length === 0 ? <div className="evidence-empty"><p>{t(language, "搜索不可用时，提交客户端证据 bundle；不会把标题按钮当作导入。")}</p></div> : <div className="evidence-list">{evidence.map((item) => <article className="evidence-item" key={item.evidence_id}><div className={`grade grade-${item.grade}`}>{item.grade}</div><div><h3>{item.title}</h3><p>{item.publisher} · {item.published_at}</p><p>{item.excerpt}</p><dl><dt>URL</dt><dd>{item.url}</dd><dt>content_hash</dt><dd>{item.content_hash}</dd>{item.search_provider ? <><dt>{copy.source}</dt><dd>{item.search_provider} · {item.search_result_hash}</dd></> : null}<dt>{t(language, "用户说明")}</dt><dd>{item.user_note || "—"}</dd></dl><Tag tone={item.grade === "E" ? "warning" : item.grade === "C" ? "neutral" : "strong"}>{t(language, item.independently_reviewable ? "可独立复核" : "不可独立复核")}</Tag></div></article>)}</div>}
        <p className="evidence-note">{t(language, "来源等级表示来源与可核验性，不代表结论正确。")}</p>
      </aside>
    </div>
    <section className="decision-panel" id="decision-thresholds" aria-labelledby="decision-thresholds-heading" aria-label={copy.gates}><div className="section-heading"><div><p className="eyebrow">04</p><h2 id="decision-thresholds-heading">{copy.gates}</h2></div><div className="rubric-total"><span>{copy.rubricScore}</span><strong>{total}</strong><span>/ 40</span></div></div>
      {!normalAllowed ? <p className="decision-blocked" role="status">{blockedReason}</p> : null}
      <p className="decision-intro">{copy.threshold}</p>
      <p className="threshold-guide">{language === "en" ? "If the score is below the threshold: Raise the rubric total to 30/40." : "若评分低于门槛：将评分提高到 30/40。"}</p>
      <details className="rubric-disclosure"><summary><span>{language === "en" ? "View 10 scoring criteria" : "查看 10 项评分详情"}</span><small>{language === "en" ? "Expand rubric" : "展开评分依据"}</small></summary><div className="rubric-panel">{rubric.map((item) => <article className="rubric-cell" key={item.label}><span>{t(language, item.label)}</span><strong>{item.score}</strong><small>/ {item.max}</small><p>{t(language, item.rationale)}</p></article>)}</div></details>
      <div className="gate-grid"><article className={normalAllowed ? "gate-card is-ready" : "gate-card"}><h3>{copy.normal}</h3><p>{copy.threshold}</p><GateList title={copy.unmet} items={normalUnmet} ready={copy.allMet} /></article><article className={reservedAllowed ? "gate-card is-ready" : "gate-card"}><h3>{copy.reserve}</h3><p>{copy.reservedThreshold}</p><GateList title={copy.unmet} items={reservedUnmet} ready={copy.allMet} /></article></div>
      <div className="reservation-controls"><label className="checkbox-field"><input type="checkbox" checked={unresolvedIssuesAcknowledged} onChange={(event) => onUnresolvedIssuesAcknowledged(event.target.checked)} /><span>{t(language, "我确认仍存在未解决问题，并同意保存这些保留事项")}</span></label><Field label={t(language, "保留意见说明（必填）")}><Textarea value={reservationNote} onChange={(event) => onReservationNote(event.target.value)} maxLength={2000} placeholder={t(language, "请写下尚未解决的问题或使用边界")} /></Field></div>
      <div className="challenge-actions"><div><Button variant="primary" onClick={() => onConfirm("confirmed")} disabled={!normalAllowed}>{t(language, "继续生成世界线")}</Button>{!normalAllowed ? <p>{normalUnmet[0]}</p> : null}</div><div><Button variant="caution" onClick={() => onConfirm("reserved")} disabled={!reservedAllowed}>{t(language, "带保留意见确认")}</Button>{!reservedAllowed ? <p>{reservedUnmet[0]}</p> : null}</div></div>
    </section>
    <p className="caution-copy">{t(language, "将保存未解决问题、AI 异议与私有证据声明。用户身份或业内经验不是公开证据；可作为私有前提单独标记。")}</p>
  </section>;
}

function GateList({ title, items, ready }: { readonly title: string; readonly items: readonly string[]; readonly ready: string }) {
  return items.length === 0 ? <p className="gate-ready"><StatusDot tone="sage" />{ready}</p> : <div className="gate-unmet"><strong>{title}</strong><div className="gate-unmet-items">{items.map((item) => <p key={item}>{item}</p>)}</div></div>;
}
