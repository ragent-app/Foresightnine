import { useMemo, useState } from "react";
import { Button, Field, Input, Select, StatusDot, Tag, Textarea } from "./Controls";
import { Header } from "./AppShell";
import { t, useLanguage } from "../i18n";
import { searchEvidenceCandidates } from "../search-client";
import type { ChallengeStatus, EvidenceDraft, EvidenceErrors, EvidenceView, RubricView, SearchCandidate, SearchCandidatesResult, TranscriptTurn } from "../types";
import { canConfirmNormally, canConfirmReserved, canSubmitReply, totalRubricScore } from "../state";
import { validateSearchEvidence } from "../validation";

interface ChallengeProps {
  readonly status: ChallengeStatus;
  readonly turns: readonly TranscriptTurn[];
  readonly replyDraft: string;
  readonly evidence: readonly EvidenceView[];
  readonly rubric: readonly RubricView[];
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

const statusCopy: Record<ChallengeStatus, string> = { idle: "等待开始", streaming: "审查中 · 正在接收回复", cancelled: "已取消生成", recovering: "连接中断，正在从上次事件恢复…", failed: "模型暂时不可用", complete: "审查完成 · 第 4 轮" };
const emptyEvidence: EvidenceDraft = { url: "", title: "", publisher: "", published_at: "2026-08-09", excerpt: "", user_note: "", supports_or_challenges: "supports", grade: "B" };

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

export function ChallengePanel({ status, turns, replyDraft, evidence, rubric, unresolvedIssuesAcknowledged, reservationNote, onStart, onCancel, onRetry, onRecover, onReplyDraft, onSubmitReply, onEvidence, onUnresolvedIssuesAcknowledged, onReservationNote, onConfirm }: ChallengeProps) {
  const { language } = useLanguage();
  const total = totalRubricScore(rubric);
  const [evidenceDraft, setEvidenceDraft] = useState<EvidenceDraft>(emptyEvidence);
  const [evidenceSubmitted, setEvidenceSubmitted] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResult, setSearchResult] = useState<SearchCandidatesResult | null>(null);
  const [searching, setSearching] = useState(false);
  const [selectionNote, setSelectionNote] = useState("");
  const evidenceErrors: EvidenceErrors = useMemo(() => evidenceSubmitted ? validateSearchEvidence(evidenceDraft) : {}, [evidenceDraft, evidenceSubmitted]);
  const searchCopy = language === "en" ? {
    title: "Search source candidates", query: "Search query", search: "Search", searching: "Searching…", optional: "Optional. Results are unverified and are never opened or imported automatically.", unavailable: "Search is unavailable. You can still submit a manual SearchEvidenceBundle.", empty: "No selectable public candidates were returned.", choose: "Use this candidate to prefill evidence", selected: "Candidate selected. Review and submit the normal evidence bundle to create evidence.", clear: "Clear selected candidate provenance",
  } : {
    title: "搜索来源候选", query: "搜索问题", search: "搜索", searching: "正在搜索…", optional: "可选。结果未经验证；不会自动打开链接或导入证据。", unavailable: "搜索不可用；你仍可手动提交 SearchEvidenceBundle。", empty: "没有可选择的公开候选来源。", choose: "选择此候选并预填证据", selected: "已选择候选。请核对后提交常规证据 bundle，才会创建证据。", clear: "清除已选候选的来源标记",
  };
  const replyAllowed = canSubmitReply(status, turns);
  const submitReply = (event: React.FormEvent<HTMLFormElement>): void => { event.preventDefault(); if (replyDraft.trim() && replyAllowed) onSubmitReply(replyDraft); };
  const submitEvidence = (event: React.FormEvent<HTMLFormElement>): void => { event.preventDefault(); setEvidenceSubmitted(true); if (Object.keys(validateSearchEvidence(evidenceDraft)).length === 0) onEvidence(evidenceDraft); };
  const updateEvidence = <K extends keyof EvidenceDraft>(field: K, value: EvidenceDraft[K]): void => { setEvidenceDraft((current) => ({ ...current, [field]: value })); };
  const submitSearch = (event: React.FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (!searchQuery.trim() || searching) return;
    setSearching(true); setSearchResult(null); setSelectionNote("");
    void searchEvidenceCandidates(searchQuery).then(setSearchResult).finally(() => setSearching(false));
  };
  const selectCandidate = (candidate: SearchCandidate): void => {
    setEvidenceDraft((current) => selectedCandidateDraft(current, candidate, language));
    setEvidenceSubmitted(false); setSelectionNote(searchCopy.selected);
  };
  const clearSelectedCandidate = (): void => {
    setEvidenceDraft((current) => {
      const { search_provider: _provider, search_result_hash: _hash, ...manualDraft } = current;
      return manualDraft;
    });
    setSelectionNote("");
  };
  return <section className="content-column challenge-view">
    <Header title={t(language, "反方审查")} action={<span className="operation-label"><StatusDot tone={status === "failed" ? "danger" : status === "complete" ? "sage" : "accent"} />{t(language, statusCopy[status])}</span>} />
    <div className="challenge-layout">
      <div className="transcript">
        {turns.length === 0 ? <div className="empty-state"><p>{t(language, "先提交一个观点，反方会检查因果链、时间边界与可证伪性。")}</p><Button variant="primary" onClick={onStart}>{t(language, "开始反方审查")}</Button></div> : turns.map((turn) => <article className={`turn turn-${turn.author}`} key={turn.id}><div className="turn-meta"><strong>{t(language, turn.author === "user" ? "你（正方）" : turn.author === "challenger" ? "反方（AI）" : "系统")}</strong><span>{t(language, turn.time)}</span></div><p>{turn.body}</p></article>)}
        {status === "streaming" ? <div className="streaming-line" role="status" aria-live="polite"><StatusDot />{t(language, "正在接收回复，可随时取消。")} <Button variant="quiet" onClick={onCancel}>{t(language, "取消")}</Button></div> : null}
        {status === "cancelled" ? <div className="notice notice-warning" role="status">{t(language, "已取消生成；已接收内容和证据仍保留。")} <Button variant="quiet" onClick={onRetry}>{t(language, "重试")}</Button></div> : null}
        {status === "recovering" ? <div className="notice notice-info" role="status">{t(language, "连接中断，正在从上次事件恢复…")} <Button variant="quiet" onClick={onRecover}>{t(language, "立即恢复")}</Button></div> : null}
        {status === "failed" ? <div className="notice notice-warning" role="alert">{t(language, "恢复失败。你可以重试，不会重复提交已确认内容。")} <Button variant="quiet" onClick={onRetry}>{t(language, "重试")}</Button></div> : null}
        <form className="composer" onSubmit={submitReply}>
          <label className="field"><span className="field-label">{t(language, "你的回复")}</span><Textarea aria-label={t(language, "回答挑战")} value={replyDraft} onChange={(event) => onReplyDraft(event.target.value)} maxLength={4000} placeholder={t(language, "回答反方问题，说明证据、条件或保留意见……")} /></label>
          <Button variant="primary" type="submit" disabled={!replyDraft.trim() || !replyAllowed}>{t(language, "提交回复")}</Button>
        </form>
      </div>
      <aside className="evidence-drawer" aria-label={t(language, "证据工作台")}><div className="drawer-heading"><h2>{t(language, "证据工作台")}</h2><span>{evidence.length} {t(language, "条")}</span></div>
        <section className="candidate-search" aria-label={searchCopy.title}>
          <h3>{searchCopy.title}</h3><p>{searchCopy.optional}</p>
          <form className="candidate-search-form" onSubmit={submitSearch}><Input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} maxLength={70} aria-label={searchCopy.query} placeholder={searchCopy.query} /><Button variant="secondary" type="submit" disabled={!searchQuery.trim() || searching}>{searching ? searchCopy.searching : searchCopy.search}</Button></form>
          {searchResult && !searchResult.project_search_available ? <p className="evidence-note" role="status"><StatusDot tone="warning" />{searchCopy.unavailable}</p> : null}
          {searchResult?.project_search_available && searchResult.candidates.length === 0 ? <p className="evidence-note" role="status">{searchCopy.empty}</p> : null}
          {searchResult?.candidates.map((candidate) => <article className="search-candidate" key={candidate.search_result_hash}><strong>{candidate.title}</strong><p>{candidate.publisher ?? candidate.search_provider}</p><p>{candidate.excerpt}</p><code>{candidate.url}</code><Button variant="quiet" type="button" onClick={() => selectCandidate(candidate)}>{searchCopy.choose}</Button></article>)}
          {selectionNote ? <p className="evidence-note" role="status">{selectionNote} <Button variant="quiet" type="button" onClick={clearSelectedCandidate}>{searchCopy.clear}</Button></p> : null}
        </section>
        <form className="evidence-form" onSubmit={submitEvidence} noValidate>
          <p className="evidence-schema-note">{t(language, "SearchEvidenceBundle：A–C 必须填写安全 URL；D/E 可留空并标记为不可独立复核。标题、发布者、摘录和用户说明仍会校验；离线 fixture 也使用同一字段结构。")}</p>
          <Field label={t(language, "URL（A–C 必填；D/E 可留空）")} error={evidenceErrors.url ? t(language, evidenceErrors.url) : undefined}><Input value={evidenceDraft.url} onChange={(event) => updateEvidence("url", event.target.value)} placeholder={t(language, "https://…（D/E 可留空）")} aria-invalid={Boolean(evidenceErrors.url)} /></Field>
          <Field label={t(language, "标题")} error={evidenceErrors.title ? t(language, evidenceErrors.title) : undefined}><Input value={evidenceDraft.title} onChange={(event) => updateEvidence("title", event.target.value)} maxLength={240} aria-invalid={Boolean(evidenceErrors.title)} /></Field>
          <Field label={t(language, "发布者")} error={evidenceErrors.publisher ? t(language, evidenceErrors.publisher) : undefined}><Input value={evidenceDraft.publisher} onChange={(event) => updateEvidence("publisher", event.target.value)} maxLength={160} aria-invalid={Boolean(evidenceErrors.publisher)} /></Field>
          <Field label={t(language, "发布日期（D/E 无 URL 可留空）")} error={evidenceErrors.published_at ? t(language, evidenceErrors.published_at) : undefined}><Input type="date" value={evidenceDraft.published_at.slice(0, 10)} onChange={(event) => updateEvidence("published_at", event.target.value)} aria-invalid={Boolean(evidenceErrors.published_at)} /></Field>
          <Field label={t(language, "摘录")} error={evidenceErrors.excerpt ? t(language, evidenceErrors.excerpt) : undefined}><Textarea value={evidenceDraft.excerpt} onChange={(event) => updateEvidence("excerpt", event.target.value)} maxLength={4000} aria-invalid={Boolean(evidenceErrors.excerpt)} /></Field>
          <Field label={t(language, "用户说明")} error={evidenceErrors.user_note ? t(language, evidenceErrors.user_note) : undefined}><Textarea value={evidenceDraft.user_note} onChange={(event) => updateEvidence("user_note", event.target.value)} maxLength={1000} aria-invalid={Boolean(evidenceErrors.user_note)} /></Field>
          <div className="evidence-form-row"><Field label={t(language, "方向")}><Select value={evidenceDraft.supports_or_challenges} onChange={(event) => updateEvidence("supports_or_challenges", event.target.value as EvidenceDraft["supports_or_challenges"])}><option value="supports">{t(language, "支持")}</option><option value="challenges">{t(language, "反驳")}</option><option value="context">{t(language, "背景")}</option></Select></Field><Field label={t(language, "等级")}><Select value={evidenceDraft.grade} onChange={(event) => updateEvidence("grade", event.target.value as EvidenceDraft["grade"])}>{["A", "B", "C", "D", "E"].map((grade) => <option key={grade}>{grade}</option>)}</Select></Field></div>
          <Button variant="secondary" type="submit">{t(language, "提交 SearchEvidenceBundle")}</Button>
        </form>
        {evidence.length === 0 ? <div className="evidence-empty"><p>{t(language, "搜索不可用时，提交客户端证据 bundle；不会把标题按钮当作导入。")}</p></div> : evidence.map((item) => <article className="evidence-item" key={item.evidence_id}><div className={`grade grade-${item.grade}`}>{item.grade}</div><div><h3>{item.title}</h3><p>{item.publisher} · {item.published_at}</p><p>{item.excerpt}</p><dl><dt>URL</dt><dd>{item.url}</dd><dt>content_hash</dt><dd>{item.content_hash}</dd><dt>{t(language, "用户说明")}</dt><dd>{item.user_note || "—"}</dd></dl><Tag tone={item.grade === "E" ? "warning" : item.grade === "C" ? "neutral" : "strong"}>{t(language, item.independently_reviewable ? "可独立复核" : "不可独立复核")}</Tag></div></article>)}
        <p className="evidence-note">{t(language, "A–E 表示来源与可核验性，不代表结论正确。")}</p>
      </aside>
    </div>
    <div className="rubric-panel"><div className="rubric-total"><span>{t(language, "审查评分")}</span><strong>{total}</strong><span>/ 40</span></div>{rubric.map((item) => <div className="rubric-cell" key={item.label}><span>{t(language, item.label)}</span><strong>{item.score}</strong><small>/ {item.max}</small></div>)}</div>
    <div className="reservation-controls"><label className="checkbox-field"><input type="checkbox" checked={unresolvedIssuesAcknowledged} onChange={(event) => onUnresolvedIssuesAcknowledged(event.target.checked)} /><span>{t(language, "我确认仍存在未解决问题，并同意保存这些保留事项")}</span></label><Field label={t(language, "保留意见说明（必填）")}><Textarea value={reservationNote} onChange={(event) => onReservationNote(event.target.value)} maxLength={2000} placeholder={t(language, "请写下尚未解决的问题或使用边界")} /></Field></div>
    <div className="challenge-actions"><Button variant="primary" onClick={() => onConfirm("confirmed")} disabled={!canConfirmNormally({ challengeStatus: status, challengeTurns: turns, rubric, evidence, language, unresolvedIssuesAcknowledged, reservationNote })}>{t(language, "继续生成世界线")}</Button><Button variant="caution" onClick={() => onConfirm("reserved")} disabled={!canConfirmReserved({ challengeStatus: status, challengeTurns: turns, rubric, evidence, language, unresolvedIssuesAcknowledged, reservationNote })}>{t(language, "带保留意见确认")}</Button></div>
    <p className="caution-copy">{t(language, "将保存未解决问题、AI 异议与私有证据声明。用户身份或业内经验不是公开证据；可作为私有前提单独标记。")}</p>
  </section>;
}
