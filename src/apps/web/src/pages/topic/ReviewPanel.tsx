import type { ResearchV2 } from "@foresightnine/contracts";
import { useId, useState } from "react";
import { ApiError } from "../../api/http";
import { CREATED_BY_LABEL, REVIEW_LABEL } from "../../lib/copy";
import { formatDateTime, formatQuantity } from "../../lib/format";
import { ROLE_LABEL } from "../../lib/research";
import { diffIsEmpty, diffVersions, draftProblem, effectiveReview, pendingClaimEdits, type ReviewState, type VersionDiff } from "../../lib/review";
import { Segmented } from "../../ui/controls";
import { Panel } from "../../ui/Panel";
import { describeError } from "../research/errors";
import { calculationTitle } from "./Conversation";
import { useTopicView } from "./context";

const REVIEW_OPTIONS: { value: ReviewState; label: string }[] = [
  { value: "draft", label: "草稿" },
  { value: "confirmed", label: "已确认" },
  { value: "reservation", label: "保留意见" },
];

const REVIEW_HELP: Record<ReviewState, string> = {
  draft: "研究仍在进行，结论还没有经过你确认。",
  confirmed: "你认可这一版的结论和依据。",
  reservation: "你对部分结论有保留，需要在复核备注中写明原因。",
};

export const CONFLICT_TEXT = "研究在你保存前已经更新。请刷新后重新应用修改。";

export function saveErrorText(error: unknown): string {
  return error instanceof ApiError && error.status === 409 ? CONFLICT_TEXT : describeError(error);
}

function roleText(claim: ResearchV2.ResearchClaim | null): string {
  if (!claim) return "";
  return ROLE_LABEL[claim.role === "counterargument" ? "counter" : "support"];
}

/** One-line summary of what a version changed, shown under each history row. */
export function diffSummary(diff: VersionDiff): string {
  const parts: string[] = [];
  if (diff.turnsAdded.length) parts.push(diff.turnsAdded.map((turn) => `新增第 ${turn.index} 轮`).join("、"));
  if (diff.sourcesAdded.length) parts.push(`新增 ${diff.sourcesAdded.length} 份资料`);
  const claims = [
    diff.claimsAdded.length ? `新增 ${diff.claimsAdded.length}` : "",
    diff.claimsChanged.length ? `修改 ${diff.claimsChanged.length}` : "",
    diff.claimsRemoved.length ? `删除 ${diff.claimsRemoved.length}` : "",
  ].filter(Boolean);
  if (claims.length) parts.push(`结论${claims.join(" / ")}`);
  const facts = diff.factsAdded.length + diff.factsChanged.length + diff.factsRemoved.length;
  if (facts) parts.push(`数字变化 ${facts}`);
  const calcs = diff.calculationsAdded.length + diff.calculationsChanged.length + diff.calculationsRemoved.length;
  if (calcs) parts.push(`计算变化 ${calcs}`);
  if (diff.review) parts.push(`复核：${REVIEW_LABEL[diff.review.before]} → ${REVIEW_LABEL[diff.review.after]}`);
  if (diff.summaryChanged) parts.push("修改了研究总结");
  if (diff.noteChanged) parts.push("修改了复核备注");
  return parts.length ? parts.join(" · ") : "没有内容变化";
}

function DiffDetail({ diff }: { diff: VersionDiff }) {
  const view = useTopicView();
  if (diffIsEmpty(diff)) return <p className="note">与上一版相比没有内容变化。</p>;
  const quantity = (value: string, unit: string, currency?: string | null) => {
    const q = formatQuantity(value, unit, currency ?? null);
    return `${q.value}${q.unit ? ` ${q.unit}` : ""}`;
  };
  return (
    <ul className="diff">
      {diff.review ? <li className="change"><span className="sign">~</span>复核状态：{REVIEW_LABEL[diff.review.before]} → {REVIEW_LABEL[diff.review.after]}</li> : null}
      {diff.summaryChanged ? <li className="change"><span className="sign">~</span>修改了研究总结</li> : null}
      {diff.noteChanged ? <li className="change"><span className="sign">~</span>修改了复核备注</li> : null}
      {diff.turnsAdded.map((turn) => <li key={turn.turn_id} className="add"><span className="sign">+</span>第 {turn.index} 轮：{turn.question}</li>)}
      {diff.sourcesAdded.map((id) => <li key={id} className="add"><span className="sign">+</span>资料：{view.sourceTitle(id)}</li>)}
      {diff.claimsAdded.map((change) => <li key={change.claimId} className="add"><span className="sign">+</span><span><b>{roleText(change.after)}</b> {change.after?.text}</span></li>)}
      {diff.claimsChanged.map((change) => (
        <li key={change.claimId} className="change">
          <span className="sign">~</span>
          <span>
            {change.before!.role !== change.after!.role ? <b>{roleText(change.before)} → {roleText(change.after)} </b> : <b>{roleText(change.after)} </b>}
            {change.before!.text !== change.after!.text ? <><del>{change.before!.text}</del> {change.after!.text}</> : change.after!.text}
          </span>
        </li>
      ))}
      {diff.claimsRemoved.map((change) => <li key={change.claimId} className="remove"><span className="sign">−</span><del>{change.before?.text}</del></li>)}
      {diff.factsAdded.map((fact) => <li key={fact.fact_id} className="add"><span className="sign">+</span>数字：{fact.label} {quantity(fact.value, fact.unit, fact.currency)}（{fact.period}）</li>)}
      {diff.factsChanged.map(({ before, after }) => (
        <li key={after.fact_id} className="change"><span className="sign">~</span>数字：{after.label} <del>{quantity(before.value, before.unit, before.currency)}</del> {quantity(after.value, after.unit, after.currency)}</li>
      ))}
      {diff.factsRemoved.map((fact) => <li key={fact.fact_id} className="remove"><span className="sign">−</span><del>数字：{fact.label}</del></li>)}
      {diff.calculationsAdded.map((calc) => <li key={calc.calculation_id} className="add"><span className="sign">+</span>计算：{calculationTitle(calc)} {quantity(calc.value, calc.unit, calc.currency)}</li>)}
      {diff.calculationsChanged.map(({ before, after }) => (
        <li key={after.calculation_id} className="change"><span className="sign">~</span>计算：{calculationTitle(after)} <del>{quantity(before.value, before.unit, before.currency)}</del> {quantity(after.value, after.unit, after.currency)}</li>
      ))}
      {diff.calculationsRemoved.map((calc) => <li key={calc.calculation_id} className="remove"><span className="sign">−</span><del>计算：{calculationTitle(calc)}</del></li>)}
    </ul>
  );
}

/** Spec §4.7 复核与版本. Edits go to the shared draft, so closing the panel keeps them. */
export function ReviewPanel({ onClose }: { onClose: () => void }) {
  const view = useTopicView();
  const ids = useId();
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const versions = view.topic.versions;
  const current = view.version;
  const effective = effectiveReview(current, view.draft);
  const problem = draftProblem(current, view.draft);
  const claimEdits = pendingClaimEdits(current, view.draft).length;

  const save = async () => {
    setError(null);
    try {
      await view.saveDraft();
      view.notify(`已保存为 v${versions.length + 1}。`);
    } catch (saveError) {
      setError(saveErrorText(saveError));
    }
  };

  return (
    <Panel
      title="复核与版本"
      onClose={onClose}
      footerClassName="split"
      footer={
        <>
          <button type="button" className="btn plain" onClick={() => { view.setDraft(() => ({})); setError(null); }} disabled={!view.dirty || view.saving}>放弃修改</button>
          <button type="button" className="btn primary" onClick={() => void save()} disabled={!view.dirty || problem !== null || view.saving || view.locked}
            title={view.locked ? "本轮研究进行中，结束后再保存" : undefined}>
            {view.saving ? "正在保存…" : "保存为新版本"}
          </button>
        </>
      }
    >
      <div className="field">
        <span className="lt">v{versions.length} 的复核状态</span>
        <Segmented label="复核状态" options={REVIEW_OPTIONS} value={effective.review} onChange={(review) => view.setDraft((draft) => ({ ...draft, review }))} />
        <span className="help">{REVIEW_HELP[effective.review]}</span>
      </div>
      <div className="field">
        <label htmlFor={`${ids}-summary`}>你的研究总结</label>
        <textarea id={`${ids}-summary`} className="tall" value={effective.summary} maxLength={100000}
          onChange={(event) => { const summary = event.target.value; view.setDraft((draft) => ({ ...draft, summary })); }}
          placeholder="用你自己的话写下目前的判断、依据和还要观察的事项" />
        <span className="help">研究 Agent 不会改写这段内容。</span>
      </div>
      <div className="field">
        <label htmlFor={`${ids}-note`}>复核备注{effective.review === "reservation" ? "（必填）" : ""}</label>
        <textarea id={`${ids}-note`} value={effective.note} maxLength={10000} aria-invalid={effective.review === "reservation" && !effective.note.trim() ? true : undefined}
          onChange={(event) => { const note = event.target.value; view.setDraft((draft) => ({ ...draft, note })); }}
          placeholder={effective.review === "reservation" ? "写明保留的原因，例如哪条结论的依据不足" : "可选"} />
      </div>
      {claimEdits > 0 ? (
        <p className="callout-lite warn" role="note">
          {claimEdits} 条结论的修改还没有保存。
          <button type="button" className="link" onClick={() => view.openPanel({ kind: "record", tab: "claims" })}>查看</button>
        </p>
      ) : null}
      {view.dirty && problem ? <span className="inline-error" role="alert">{problem}</span> : null}
      {error ? <span className="inline-error" role="alert">{error}</span> : null}

      <div>
        <div className="gtitle">版本历史 · {versions.length}</div>
        <div className="list">
          {[...versions].map((version, index) => ({ version, number: index + 1, previous: index > 0 ? versions[index - 1]! : null })).reverse().map(({ version, number, previous }) => {
            const diff = previous ? diffVersions(previous, version) : null;
            const expanded = open === version.version_id;
            return (
              <div key={version.version_id} className="item">
                <div className="line">
                  <span className="grow">
                    <b>v{number}</b>{number === versions.length ? <span className="role light neutral">当前</span> : null}
                    <span className="meta"> · {formatDateTime(version.created_at)} · {CREATED_BY_LABEL[version.created_by ?? "unknown"] ?? "未知"} · {REVIEW_LABEL[version.review] ?? version.review}</span>
                  </span>
                  {diff ? (
                    <button type="button" className="link" aria-expanded={expanded} onClick={() => setOpen(expanded ? null : version.version_id)}>{expanded ? "收起" : "对比"}</button>
                  ) : null}
                </div>
                <span className="meta">{diff ? diffSummary(diff) : "研究创建"}</span>
                {version.note ? <span className="meta">备注：{version.note}</span> : null}
                {expanded && diff ? <DiffDetail diff={diff} /> : null}
              </div>
            );
          })}
        </div>
        <p className="note inset">“对比”显示这一版相对上一版的变化。每次保存、研究或添加资料都会生成新版本，旧版本保持不变。</p>
      </div>
    </Panel>
  );
}
