import type { ResearchV2 } from "@foresightnine/contracts";
import { useCallback, useMemo, useState, type Ref } from "react";
import { Link } from "react-router-dom";
import type { ProgressEvent, RecoveryStatus } from "../../api/types";
import { formatInterimParts, formulaLabel, gapText, recoveryReasonLabel, turnReasonLabel, TURN_STATUS_LABEL, VERIFY_NOTE } from "../../lib/copy";
import { displayTurnNumber, formatClock, formatDuration, formatInteger, formatQuantity, turnStamp } from "../../lib/format";
import { answerSummary, claimRole, parseTurnEvents, partialSummary, ROLE_LABEL, stepRows, toolStepCount, turnCharts, turnContent, turnDurationMs } from "../../lib/research";
import { Disclosure } from "../../ui/controls";
import { Icon } from "../../ui/icons";
import { extractReferencedChartIds, MarkdownContent } from "../../ui/MarkdownContent";
import { useNow } from "../../ui/media";
import { Menu, type MenuEntry } from "../../ui/Menu";
import { CitationButton } from "./Citation";
import { ChartCard } from "./charts/ChartCard";
import { LiveDraftPreview, LiveEmptyOrStream } from "./LiveStreamView";
import { ProcessTimeline } from "./ProcessTimeline";
import { useTopicView, type LiveRun } from "./context";

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

export function calculationTitle(calc: ResearchV2.ResearchCalculation): string {
  const periods = [...new Set(calc.inputs.map((input) => input.period).filter(Boolean))];
  const name = formulaLabel(calc.formula_id);
  return periods.length > 0 ? `${name} · ${periods.join(" 对 ")}` : name;
}

export function usageMeta(turn: ResearchV2.ResearchTurn): string {
  const parts = [`${turn.usage.model_calls} 次模型调用`];
  if (turn.usage.search_calls > 0) parts.push(`${turn.usage.search_calls} 次搜索`);
  // partial_unknown: some call reported no usage, so the total includes reserved estimates for those calls.
  parts.push(turn.usage.usage_status === "partial_unknown"
    ? `约 ${formatInteger(turn.usage.total_tokens)} Token（部分调用用量未知，已按预留额度计入）`
    : `${formatInteger(turn.usage.total_tokens)} Token`);
  const ms = turnDurationMs(turn);
  if (ms !== null && ms >= 0) parts.push(formatDuration(ms));
  return parts.join(" · ");
}

function PartialNotesBlock({ notes }: { notes: string }) {
  const trimmed = notes.trim();
  if (!trimmed) return null;
  const label = "过程与缺口说明（未核实线索与建议，非正式结论）";
  return (
    <Disclosure summary={label} defaultOpen>
      <p className="note">以下说明来自模型补充，未经原文位置或数据核对，不作为正式结论。</p>
      <div className="callout-lite ok" role="note" aria-label={label}>
        <div className="field">
          <MarkdownContent content={trimmed} variant="notes" />
        </div>
      </div>
    </Disclosure>
  );
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

const BLOCKED_CONTINUATION_REASONS = new Set<string>(["repeated_truncation", "repeated_timeout"]);

/** Callout actions follow spec §4.3: one primary decision, the rest under “更多”. */
function CalloutActions({ actions }: { actions: TurnActions }) {
  const { recovery } = actions;
  const continuationBlocked = recovery?.reason ? BLOCKED_CONTINUATION_REASONS.has(recovery.reason) : false;
  const canContinue = recovery?.can_continue === true && !continuationBlocked;
  const canDeliver = recovery?.can_deliver === true && actions.deliverable && !continuationBlocked;
  const disabled = actions.busy || actions.locked;
  let primary: { label: string; onClick: () => void; disabled: boolean };
  if (actions.checking && !recovery) primary = { label: "正在检查恢复状态…", onClick: () => undefined, disabled: true };
  else if (canContinue) primary = { label: "继续研究", onClick: actions.onContinue, disabled };
  else if (canDeliver) primary = { label: "交付已有成果", onClick: actions.onDeliver, disabled };
  else primary = { label: "重新提问", onClick: actions.onAskAgain, disabled: actions.locked };

  const more: MenuEntry[] = [];
  if (canDeliver && canContinue) more.push({ key: "deliver", label: "交付已有成果", icon: "check", onSelect: actions.onDeliver, disabled });
  if (actions.onFillGap && !continuationBlocked) more.push({ key: "gap", label: "补充缺口", icon: "chev", onSelect: actions.onFillGap, disabled: actions.locked });
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

export function GapCallout({ turn, actions, tone }: { turn: ResearchV2.ResearchTurn; actions: TurnActions | null; tone: "partial" | "completed" }) {
  const gaps = turn.gaps ?? [];
  if (tone === "completed" && gaps.length === 0) return null;
  const shown = gaps.slice(0, 2);
  const hidden = gaps.length - shown.length;
  const needsMarketKey = gaps.some((gap) => gap.code === "market_data_key_required");
  const recoveryReason = tone === "partial" && actions?.recovery?.reason
    ? recoveryReasonLabel(actions.recovery.reason)
    : null;
  return (
    <div className="callout" role="note">
      <span className="h">{tone === "partial" ? "未完成的部分" : "仍有缺口"}</span>
      {shown.length > 0 ? (
        <ul>
          {shown.map((gap, index) => <li key={`${gap.code}-${index}`}>{gapText(gap)}</li>)}
        </ul>
      ) : null}
      {hidden > 0 ? <span className="why">另有 {hidden} 项缺口，在研究记录里查看。</span> : null}
      {needsMarketKey ? (
        <span className="why" data-hint="market-data-key">
          想要行情数据？<Link className="link" to="/settings?tab=data">前往设置，添加自己的 Twelve Data key</Link>
        </span>
      ) : null}
      {tone === "partial" ? <span className="why">停止原因：{turnReasonLabel(turn.reason, turn.stopping_note)}</span> : null}
      {recoveryReason ? <span className="why">恢复检查：{recoveryReason}</span> : null}
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
              <MarkdownContent content={claim.text} variant="inline" />
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
  const reportText = (turn.report ?? "").trim();
  const partialNotes = partial ? (turn.partial_notes ?? "").trim() : "";
  const events = useMemo(() => parseTurnEvents(turn.events), [turn.events]);
  const rows = useMemo(() => stepRows(events), [events]);
  const toolSteps = useMemo(() => toolStepCount(events), [events]);
  const [processOpen, setProcessOpen] = useState(false);

  const chartsById = useMemo(() => {
    const map = new Map<string, ResearchV2.ResearchChart>();
    for (const chart of view.content.charts ?? []) {
      map.set(chart.chart_id, chart);
    }
    return map;
  }, [view.content.charts]);

  const turnChartList = useMemo(() => turnCharts(view.content, turn), [view.content, turn]);
  const inlineChartIds = useMemo(() => extractReferencedChartIds(reportText), [reportText]);
  const trailingCharts = useMemo(
    () => turnChartList.filter((chart) => !inlineChartIds.has(chart.chart_id)),
    [turnChartList, inlineChartIds],
  );
  const renderChart = useCallback(
    (chart: ResearchV2.ResearchChart) => <ChartCard chart={chart} />,
    [],
  );

  let processSummary = "";
  if (rows.length > 0) {
    const parts = [
      toolSteps > 0
        ? `${partial ? "已执行" : "已完成"} ${toolSteps} 步研究过程`
        : `已记录 ${rows.length} 条研究过程`,
    ];
    if (turn.usage.search_calls > 0) parts.push(`${turn.usage.search_calls} 次搜索`);
    if (turn.usage.model_calls > 0) parts.push(`${turn.usage.model_calls} 次调用`);
    processSummary = parts.join(" · ");
  }

  return (
    <div className="answer">
      <div className="status">
        <span className={`role ${partial ? "review" : "support"}`}>{TURN_STATUS_LABEL[turn.status] ?? turn.status}</span>
        <span className="meta">{usageMeta(turn)}</span>
      </div>
      {rows.length > 0 ? (
        <div className="turn-process">
          <button
            type="button"
            className="turn-process-toggle"
            aria-expanded={processOpen}
            onClick={() => setProcessOpen((open) => !open)}
          >
            <span>{processSummary}</span>
            <Icon name={processOpen ? "up" : "down"} size={14} />
          </button>
          {processOpen ? (
            <div className="turn-process-body">
              <ProcessTimeline events={events} />
              <div className="turn-process-foot">
                <button type="button" className="link" onClick={() => view.openPanel({ kind: "record", tab: "steps", turnKey: turn.turn_id })}>
                  在研究记录中查看 ›
                </button>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
      {reportText ? (
        <div className="report">
          <MarkdownContent
            content={reportText}
            variant="report"
            chartsById={chartsById}
            renderChart={renderChart}
          />
        </div>
      ) : partial ? (
        <p>{partialSummary({ facts: items.facts.length, calculations: items.calculations.length, claims: items.claims.length })}</p>
      ) : null}
      {trailingCharts.length > 0 ? (
        <div className="turn-charts">
          {trailingCharts.map((chart) => (
            <ChartCard key={chart.chart_id} chart={chart} />
          ))}
        </div>
      ) : null}
      <StatLine calculations={items.calculations} turnId={turn.turn_id} />
      <ClaimList claims={items.claims} />
      {items.claims.length > 0 ? <p className="note">{VERIFY_NOTE}</p> : null}
      <GapCallout turn={turn} actions={actions} tone={partial ? "partial" : "completed"} />
      {partial && partialNotes ? <PartialNotesBlock notes={partialNotes} /> : null}
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
export function InterruptedTurn({ number, startedAt, question, status, actions, events = [] }: {
  number: number; startedAt: string; question: string; status: "failed" | "cancelled"; actions: TurnActions; events?: ProgressEvent[];
}) {
  const reason = actions.recovery?.reason ? recoveryReasonLabel(actions.recovery.reason) : null;
  return (
    <section className="turn" aria-label={`第 ${number} 轮`}>
      <TurnStamp number={number} iso={startedAt} />
      {question ? <AskBubble text={question} /> : null}
      <div className="answer">
        <div className="status"><span className="role error">{status === "cancelled" ? "已取消" : "运行中断"}</span></div>
        {events.length > 0 ? <ProcessTimeline events={events} initialTailLimit={3} /> : null}
        <div className="callout error" role="note">
          <span className="h">{status === "cancelled" ? "这一轮已取消，没有保存新的版本。" : "这一轮没有完成，也没有保存新的版本。"}</span>
          {reason ? <span className="why">恢复检查：{reason}</span> : null}
          <CalloutActions actions={actions} />
        </div>
      </div>
    </section>
  );
}

export function LiveTurn({ number, startedAt, question, live = null, sectionRef, onDraftGrow, continuing = null }: {
  number: number;
  startedAt: string;
  question: string;
  live?: LiveRun | null;
  sectionRef?: Ref<HTMLElement>;
  onDraftGrow?: (() => void) | undefined;
  continuing?: "continue" | "deliver" | null | undefined;
}) {
  const now = useNow(true);
  const [expanded, setExpanded] = useState(true);
  const progress = live?.progress ?? null;
  const status = progress?.operation_status ?? live?.operation.status ?? "pending";
  const isPending = status === "pending";
  const startIso = progress?.started_at ?? live?.operation.created_at ?? startedAt;
  const startedMs = Date.parse(startIso);
  const elapsed = Number.isNaN(startedMs) ? "" : formatClock(Math.max(0, now - startedMs));
  const usage = progress?.usage;
  const metaParts: string[] = [];
  if (elapsed) metaParts.push(`已用 ${elapsed}`);
  if (usage && usage.model_calls > 0) metaParts.push(`${usage.model_calls} 次调用`);
  if (usage && usage.search_calls > 0) metaParts.push(`${usage.search_calls} 次搜索`);
  metaParts.push(...formatInterimParts(progress?.interim));
  const events = live?.events ?? [];
  const current = progress?.current ?? null;
  const hasTimeline = events.length > 0 || current !== null;
  const isContinue = continuing === "continue" || continuing === "deliver";
  const statusTitle = continuing === "deliver"
    ? (isPending ? "排队中 · 交付已有成果" : "正在交付已有成果")
    : continuing === "continue"
      ? (isPending ? "排队中 · 继续研究" : "正在继续研究")
      : (isPending ? "排队中" : "正在研究");

  return (
    <section
      ref={sectionRef}
      className={isContinue ? "turn turn-continuation" : "turn"}
      aria-label={isContinue ? `第 ${number} 轮继续研究` : `第 ${number} 轮`}
      aria-busy="true"
    >
      {!isContinue ? <TurnStamp number={number} iso={startedAt} /> : null}
      {!isContinue && question ? <AskBubble text={question} /> : null}
      <div className="answer live-turn">
        <div className="live-turn-head">
          <span className="dot running" aria-hidden="true" />
          <span className="live-turn-title">{statusTitle}</span>
          {metaParts.length > 0 ? <span className="meta live-turn-meta">{metaParts.join(" · ")}</span> : null}
          <button
            type="button"
            className="link live-turn-toggle"
            aria-expanded={expanded}
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? "收起" : "展开"}
            <Icon name={expanded ? "up" : "down"} size={14} />
          </button>
        </div>
        {expanded ? (
          hasTimeline ? (
            <ProcessTimeline events={events} current={current} live stream={live?.stream} />
          ) : (
            <LiveEmptyOrStream
              stream={live?.stream}
              idleText={isPending ? "任务已提交，等待调度开始…" : "正在准备研究上下文…"}
            />
          )
        ) : null}
        <LiveDraftPreview stream={live?.stream} onGrow={onDraftGrow} />
      </div>
    </section>
  );
}

export const PendingTurn = LiveTurn;
