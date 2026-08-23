import { useMemo, useState } from "react";
import { Button, Field, Input, Select, StatusDot, Tag, Textarea } from "./Controls";
import { Header } from "./AppShell";
import type { ChallengeStatus, EvidenceDraft, EvidenceErrors, EvidenceView, RubricView, TranscriptTurn } from "../types";
import { canConfirmNormally, totalRubricScore } from "../state";
import { validateSearchEvidence } from "../validation";

interface ChallengeProps {
  readonly status: ChallengeStatus;
  readonly turns: readonly TranscriptTurn[];
  readonly replyDraft: string;
  readonly evidence: readonly EvidenceView[];
  readonly rubric: readonly RubricView[];
  readonly onStart: () => void;
  readonly onCancel: () => void;
  readonly onRetry: () => void;
  readonly onRecover: () => void;
  readonly onReplyDraft: (draft: string) => void;
  readonly onSubmitReply: (body: string) => void;
  readonly onEvidence: (draft: EvidenceDraft) => void;
  readonly onConfirm: (kind: "confirmed" | "reserved") => void;
}

const statusCopy: Record<ChallengeStatus, string> = { idle: "等待开始", streaming: "审查中 · 正在接收回复", cancelled: "已取消生成", recovering: "连接中断，正在从上次事件恢复…", failed: "模型暂时不可用", complete: "审查完成 · 第 4 轮" };
const emptyEvidence: EvidenceDraft = { url: "", title: "", publisher: "", published_at: "2026-08-09", excerpt: "", user_note: "", supports_or_challenges: "supports", grade: "B" };

export function ChallengePanel({ status, turns, replyDraft, evidence, rubric, onStart, onCancel, onRetry, onRecover, onReplyDraft, onSubmitReply, onEvidence, onConfirm }: ChallengeProps) {
  const total = totalRubricScore(rubric);
  const [evidenceDraft, setEvidenceDraft] = useState<EvidenceDraft>(emptyEvidence);
  const [evidenceSubmitted, setEvidenceSubmitted] = useState(false);
  const evidenceErrors: EvidenceErrors = useMemo(() => evidenceSubmitted ? validateSearchEvidence(evidenceDraft) : {}, [evidenceDraft, evidenceSubmitted]);
  const submitReply = (event: React.FormEvent<HTMLFormElement>): void => { event.preventDefault(); if (replyDraft.trim()) onSubmitReply(replyDraft); };
  const submitEvidence = (event: React.FormEvent<HTMLFormElement>): void => { event.preventDefault(); setEvidenceSubmitted(true); if (Object.keys(validateSearchEvidence(evidenceDraft)).length === 0) onEvidence(evidenceDraft); };
  const updateEvidence = <K extends keyof EvidenceDraft>(field: K, value: EvidenceDraft[K]): void => { setEvidenceDraft((current) => ({ ...current, [field]: value })); };
  return <section className="content-column challenge-view">
    <Header title="反方审查" action={<span className="operation-label"><StatusDot tone={status === "failed" ? "danger" : status === "complete" ? "sage" : "accent"} />{statusCopy[status]}</span>} />
    <div className="challenge-layout">
      <div className="transcript">
        {turns.length === 0 ? <div className="empty-state"><p>先提交一个观点，反方会检查因果链、时间边界与可证伪性。</p><Button variant="primary" onClick={onStart}>开始反方审查</Button></div> : turns.map((turn) => <article className={`turn turn-${turn.author}`} key={turn.id}><div className="turn-meta"><strong>{turn.author === "user" ? "你（正方）" : turn.author === "challenger" ? "反方（AI）" : "系统"}</strong><span>{turn.time}</span></div><p>{turn.body}</p></article>)}
        {status === "streaming" ? <div className="streaming-line" role="status" aria-live="polite"><StatusDot />正在接收回复，可随时取消。 <Button variant="quiet" onClick={onCancel}>取消</Button></div> : null}
        {status === "cancelled" ? <div className="notice notice-warning" role="status">已取消生成；已接收内容和证据仍保留。 <Button variant="quiet" onClick={onRetry}>重试</Button></div> : null}
        {status === "recovering" ? <div className="notice notice-info" role="status">连接中断，正在从上次事件恢复… <Button variant="quiet" onClick={onRecover}>立即恢复</Button></div> : null}
        {status === "failed" ? <div className="notice notice-warning" role="alert">恢复失败。你可以重试，不会重复提交已确认内容。 <Button variant="quiet" onClick={onRetry}>重试</Button></div> : null}
        <form className="composer" onSubmit={submitReply}>
          <label className="field"><span className="field-label">你的回复</span><Textarea aria-label="回答挑战" value={replyDraft} onChange={(event) => onReplyDraft(event.target.value)} maxLength={4000} placeholder="回答反方问题，说明证据、条件或保留意见……" /></label>
          <Button variant="primary" type="submit" disabled={!replyDraft.trim()}>提交回复</Button>
        </form>
      </div>
      <aside className="evidence-drawer" aria-label="证据工作台"><div className="drawer-heading"><h2>证据工作台</h2><span>{evidence.length} 条</span></div>
        <form className="evidence-form" onSubmit={submitEvidence} noValidate>
          <p className="evidence-schema-note">SearchEvidenceBundle：A–C 必须填写安全 URL；D/E 可留空并标记为不可独立复核。标题、发布者、摘录和用户说明仍会校验；离线 fixture 也使用同一字段结构。</p>
          <Field label="URL（A–C 必填；D/E 可留空）" error={evidenceErrors.url}><Input value={evidenceDraft.url} onChange={(event) => updateEvidence("url", event.target.value)} placeholder="https://…（D/E 可留空）" aria-invalid={Boolean(evidenceErrors.url)} /></Field>
          <Field label="标题" error={evidenceErrors.title}><Input value={evidenceDraft.title} onChange={(event) => updateEvidence("title", event.target.value)} maxLength={240} aria-invalid={Boolean(evidenceErrors.title)} /></Field>
          <Field label="发布者" error={evidenceErrors.publisher}><Input value={evidenceDraft.publisher} onChange={(event) => updateEvidence("publisher", event.target.value)} maxLength={160} aria-invalid={Boolean(evidenceErrors.publisher)} /></Field>
          <Field label="发布日期（D/E 无 URL 可留空）" error={evidenceErrors.published_at}><Input type="date" value={evidenceDraft.published_at.slice(0, 10)} onChange={(event) => updateEvidence("published_at", event.target.value)} aria-invalid={Boolean(evidenceErrors.published_at)} /></Field>
          <Field label="摘录" error={evidenceErrors.excerpt}><Textarea value={evidenceDraft.excerpt} onChange={(event) => updateEvidence("excerpt", event.target.value)} maxLength={4000} aria-invalid={Boolean(evidenceErrors.excerpt)} /></Field>
          <Field label="用户说明" error={evidenceErrors.user_note}><Textarea value={evidenceDraft.user_note} onChange={(event) => updateEvidence("user_note", event.target.value)} maxLength={1000} aria-invalid={Boolean(evidenceErrors.user_note)} /></Field>
          <div className="evidence-form-row"><Field label="方向"><Select value={evidenceDraft.supports_or_challenges} onChange={(event) => updateEvidence("supports_or_challenges", event.target.value as EvidenceDraft["supports_or_challenges"])}><option value="supports">支持</option><option value="challenges">反驳</option><option value="context">背景</option></Select></Field><Field label="等级"><Select value={evidenceDraft.grade} onChange={(event) => updateEvidence("grade", event.target.value as EvidenceDraft["grade"])}>{["A", "B", "C", "D", "E"].map((grade) => <option key={grade}>{grade}</option>)}</Select></Field></div>
          <Button variant="secondary" type="submit">提交 SearchEvidenceBundle</Button>
        </form>
        {evidence.length === 0 ? <div className="evidence-empty"><p>搜索不可用时，提交客户端证据 bundle；不会把标题按钮当作导入。</p></div> : evidence.map((item) => <article className="evidence-item" key={item.evidence_id}><div className={`grade grade-${item.grade}`}>{item.grade}</div><div><h3>{item.title}</h3><p>{item.publisher} · {item.published_at}</p><p>{item.excerpt}</p><dl><dt>URL</dt><dd>{item.url}</dd><dt>content_hash</dt><dd>{item.content_hash}</dd><dt>用户说明</dt><dd>{item.user_note || "—"}</dd></dl><Tag tone={item.grade === "E" ? "warning" : item.grade === "C" ? "neutral" : "strong"}>{item.independently_reviewable ? "可独立复核" : "不可独立复核"}</Tag></div></article>)}
        <p className="evidence-note">A–E 表示来源与可核验性，不代表结论正确。</p>
      </aside>
    </div>
    <div className="rubric-panel"><div className="rubric-total"><span>审查评分</span><strong>{total}</strong><span>/ 40</span></div>{rubric.map((item) => <div className="rubric-cell" key={item.label}><span>{item.label}</span><strong>{item.score}</strong><small>/ {item.max}</small></div>)}</div>
    <div className="challenge-actions"><Button variant="primary" onClick={() => onConfirm("confirmed")} disabled={!canConfirmNormally({ challengeStatus: status, rubric, evidence })}>继续生成世界线</Button><Button variant="caution" onClick={() => onConfirm("reserved")}>带保留意见确认</Button></div>
    <p className="caution-copy">将保存未解决问题、AI 异议与私有证据声明。用户身份或业内经验不是公开证据；可作为私有前提单独标记。</p>
  </section>;
}
