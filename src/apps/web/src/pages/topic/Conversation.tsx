import type { ResearchV2 } from "@foresightnine/contracts";
import type { RecoveryStatus } from "../../api/types";
import { gapText, recoveryReasonLabel, turnReasonLabel, TURN_STATUS_LABEL, VERIFY_NOTE } from "../../lib/copy";
import { displayTurnNumber, formatDuration, formatInteger, formatQuantity, turnStamp } from "../../lib/format";
import { answerSummary, claimRole, partialSummary, ROLE_LABEL, turnContent, turnDurationMs } from "../../lib/research";
import { Menu, type MenuEntry } from "../../ui/Menu";
import { CitationButton } from "./Citation";
import { useTopicView } from "./context";

export interface TurnActions {
  recovery: RecoveryStatus | undefined;
  checking: boolean;
  busy: boolean;
  locked: boolean;
  error: string | null;
  onContinue: () => void;
  onDeliver: () => void;
  onAskAgain: () => void;
  onFillGap: (() => void) | null;
  onAddSources: () => void;
  /**
   * False when the turn already ends with the model's own submission: re-delivering it
   * republishes the same content (362 acceptance), so it is not offered.
   */
  deliverable: boolean;
}

const FORMULA_LABEL: Record<string, string> = { growth_percent: "增长率", margin_percent: "利润率", difference: "差额" };

export function calculationTitle(calc: ResearchV2.ResearchCalculation): string {
  const periods = [...new Set(calc.inputs.map((input) => input.period).filter(Boolean))];
  const name = FORMULA_LABEL[calc.formula_id] ?? calc.formula_id;
  return periods.length > 0 ? `${name} · ${periods.join(" 对 ")}` : name;
}

function usageMeta(turn: ResearchV2.ResearchTurn): string {
  const parts = [`${turn.usage.model_calls} 次模型调用`];
  if (turn.usage.search_calls > 0) parts.push(`${turn.usage.search_calls} 次搜索`);
  parts.push(`${formatInteger(turn.usage.total_tokens)} Token`);
  const ms = turnDurationMs(turn);
  if (ms !== null && ms >= 0) parts.push(formatDuration(ms));
  return parts.join(" · ");
}

function paragraphs(text: string): string[] {
  return text.split(/\n\s*\n/).map((part) => part.trim()).filter(Boolean);
}

export function TurnStamp({ number, iso }: { number: number; iso: string }) {
  return <div className="stamp">{turnStamp(number, iso)}</div>;
}

export function AskBubble({ text, focusLabel }: { text: string; focusLabel?: string | null }) {
  return (
    <div className="ask">
      {focusLabel ? <span className="focus-ref">{focusLabel}</span> : null}
      {text}
    </div>
  );
}

/** Callout actions follow spec §4.3: one primary decision, the rest under “更多”. */
function CalloutActions({ actions }: { actions: TurnActions }) {
  const { recovery } = actions;
  const canContinue = recovery?.can_continue === true;
  const canDeliver = recovery?.can_deliver === true && actions.deliverable;
  const disabled = actions.busy || actions.locked;
  let primary: { label: string; onClick: () => void; disabled: boolean };
  if (actions.checking && !recovery) primary = { label: "继续研究", onClick: () => undefined, disabled: true };
  else if (canContinue) primary = { label: "继续研究", onClick: actions.onContinue, disabled };
  else if (canDeliver) primary = { label: "交付已有成果", onClick: actions.onDeliver, disabled };
  else primary = { label: "重新提问", onClick: actions.onAskAgain, disabled: actions.locked };

  const more: MenuEntry[] = [];
  if (canDeliver && canContinue) more.push({ key: "deliver", label: "交付已有成果", icon: "check", onSelect: actions.onDeliver, disabled });
  if (actions.onFillGap) more.push({ key: "gap", label: "补充缺口", icon: "chev", onSelect: actions.onFillGap, disabled: actions.locked });
  more.push({ key: "add", label: "添加资料", icon: "plus", onSelect: actions.onAddSources, disabled: actions.locked });
  if (primary.label !== "重新提问") more.push({ key: "ask", label: "重新提问", icon: "pen", onSelect: actions.onAskAgain, disabled: actions.locked });

  return (
    <>
      <div className="actions">
        <button type="button" className="btn primary" onClick={primary.onClick} disabled={primary.disabled}>
          {actions.busy ? "正在提交…" : primary.label}
        </button>
        {more.length > 0 ? <Menu label="更多" items={more} trigger="更多" triggerClassName="btn" align="start" /> : null}
      </div>
      {actions.error ? <span className="inline-error" role="alert">{actions.error}</span> : null}
    </>
  );
}

function GapCallout({ turn, actions, tone }: { turn: ResearchV2.ResearchTurn; actions: TurnActions | null; tone: "partial" | "completed" }) {
  const gaps = turn.gaps ?? [];
  if (tone === "completed" && gaps.length === 0) return null;
  const shown = gaps.slice(0, 2);
  const hidden = gaps.length - shown.length;
  return (
    <div className="callout" role="note">
      <span className="h">{tone === "partial" ? "未完成的部分" : "仍有缺口"}</span>
      {shown.length > 0 ? (
        <ul>
          {shown.map((gap, index) => <li key={`${gap.code}-${index}`}>{gapText(gap)}</li>)}
        </ul>
      ) : null}
      {hidden > 0 ? <span className="why">另有 {hidden} 项缺口，在研究记录里查看。</span> : null}
      {tone === "partial" ? <span className="why">停止原因：{turnReasonLabel(turn.reason, turn.stopping_note)}</span> : null}
      {tone === "partial" && actions ? <CalloutActions actions={actions} /> : null}
    </div>
  );
}

function StatLine({ calculations, turnId }: { calculations: readonly ResearchV2.ResearchCalculation[]; turnId: string }) {
  const view = useTopicView();
  if (calculations.length === 0) return null;
  return (
    <div className="stats">
      {calculations.slice(0, 3).map((calc) => {
        const quantity = formatQuantity(calc.value, calc.unit, calc.currency);
        return (
          <button key={calc.calculation_id} type="button" className="stat" onClick={() => view.openPanel({ kind: "record", tab: "numbers", turnKey: turnId })}>
            <b>{quantity.value}{quantity.unit ? <small className="meta"> {quantity.unit}</small> : null}</b>
            <span>{calculationTitle(calc)}</span>
          </button>
        );
      })}
    </div>
  );
}

function SummaryLinks({ turn }: { turn: ResearchV2.ResearchTurn }) {
  const view = useTopicView();
  const parts = answerSummary(turn, view.content);
  if (parts.length === 0) return null;
  return (
    <div className="summary">
      {parts.map((part, index) => (
        <span key={part.tab} className="summary-part">
          {index > 0 ? <span className="sep" aria-hidden="true">· </span> : null}
          <button type="button" className="link" onClick={() => view.openPanel({ kind: "record", tab: part.tab, turnKey: turn.turn_id })}>{part.label}</button>
        </span>
      ))}
      <span aria-hidden="true">›</span>
    </div>
  );
}

function ClaimList({ claims }: { claims: readonly ResearchV2.ResearchClaim[] }) {
  const view = useTopicView();
  if (claims.length === 0) return null;
  return (
    <ul className="claims">
      {claims.map((claim) => {
        const role = claimRole(claim);
        const claimId = claim.claim_id ?? "";
        return (
          <li key={claimId || claim.text} id={claimId ? `claim-${claimId}` : undefined}>
            <span className={`role ${role}`}>{ROLE_LABEL[role]}</span>
            <span>
              {claim.text}
              {(claim.citations ?? []).map((citation, index) => (
                <CitationButton key={index} n={view.citations.numberFor(citation)} citation={citation} claimId={claimId} citationIndex={index} />
              ))}
              {claim.edited_by_user ? <span className="edited">你修改过</span> : null}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export function AnswerBlock({ turn, actions }: { turn: ResearchV2.ResearchTurn; actions: TurnActions | null }) {
  const view = useTopicView();
  const items = turnContent(view.content, turn);
  const partial = turn.status === "partial";
  const report = paragraphs(turn.report ?? "");
  return (
    <div className="answer">
      <div className="status">
        <span className={`role ${partial ? "review" : "support"}`}>{TURN_STATUS_LABEL[turn.status] ?? turn.status}</span>
        <span className="meta">{usageMeta(turn)}</span>
      </div>
      {report.length > 0 ? (
        <div className="report">{report.map((text, index) => <p key={index}>{text}</p>)}</div>
      ) : partial ? (
        <p>{partialSummary({ facts: items.facts.length, calculations: items.calculations.length, claims: items.claims.length })}</p>
      ) : null}
      <StatLine calculations={items.calculations} turnId={turn.turn_id} />
      <ClaimList claims={items.claims} />
      {items.claims.length > 0 ? <p className="note">{VERIFY_NOTE}</p> : null}
      <GapCallout turn={turn} actions={actions} tone={partial ? "partial" : "completed"} />
      <SummaryLinks turn={turn} />
    </div>
  );
}

export function TurnView({ turn, actions }: { turn: ResearchV2.ResearchTurn; actions: TurnActions | null }) {
  const view = useTopicView();
  const number = displayTurnNumber(turn);
  const focus = turn.focus_citation;
  let focusLabel: string | null = null;
  if (focus) {
    const claim = view.content.claims?.find((item) => item.claim_id === focus.claim_id);
    const citation = claim?.citations?.[focus.citation_index];
    focusLabel = citation ? `深挖引用 ${view.citations.numberFor(citation)}` : "深挖引用";
  }
  return (
    <section className="turn" aria-label={`第 ${number} 轮`}>
      <TurnStamp number={number} iso={turn.started_at} />
      <AskBubble text={turn.question} focusLabel={focusLabel} />
      <AnswerBlock turn={turn} actions={actions} />
    </section>
  );
}

/** A run that stopped before saving any turn (spec §4.3 回答：中断或已取消). */
export function InterruptedTurn({ number, startedAt, question, status, actions }: {
  number: number; startedAt: string; question: string; status: "failed" | "cancelled"; actions: TurnActions;
}) {
  const reason = actions.recovery?.reason ? recoveryReasonLabel(actions.recovery.reason) : null;
  return (
    <section className="turn" aria-label={`第 ${number} 轮`}>
      <TurnStamp number={number} iso={startedAt} />
      {question ? <AskBubble text={question} /> : null}
      <div className="answer">
        <div className="status"><span className="role error">{status === "cancelled" ? "已取消" : "运行中断"}</span></div>
        <div className="callout error" role="note">
          <span className="h">{status === "cancelled" ? "这一轮已取消，没有保存新的版本。" : "这一轮没有完成，也没有保存新的版本。"}</span>
          {reason ? <span className="why">恢复检查：{reason}</span> : null}
          <CalloutActions actions={actions} />
        </div>
      </div>
    </section>
  );
}

export function PendingTurn({ number, startedAt, question }: { number: number; startedAt: string; question: string }) {
  return (
    <section className="turn" aria-label={`第 ${number} 轮`} aria-busy="true">
      <TurnStamp number={number} iso={startedAt} />
      <AskBubble text={question} />
    </section>
  );
}
