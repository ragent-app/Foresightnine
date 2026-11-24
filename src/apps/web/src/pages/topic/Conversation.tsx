import type { ResearchV2 } from "@foresightnine/contracts";
import { useCallback, useMemo, useState, type ReactNode, type Ref } from "react";
import { Link } from "react-router-dom";
import type { CurrentActivity, ProgressEvent, RecoveryStatus } from "../../api/types";
import { formatInterimParts, formulaLabel, gapText, modelPhaseLabel, recoveryReasonLabel, reviewReasonText, toolLabel, turnReasonLabel, VERIFY_NOTE } from "../../lib/copy";
import { displayTurnNumber, formatClock, formatDuration, formatInteger, formatQuantity, turnStamp } from "../../lib/format";
import { answerSummary, claimRole, parseAttributions, parseTurnEvents, partialSummary, ROLE_LABEL, stepRows, toolStepCount, turnCharts, turnContent, turnDurationMs } from "../../lib/research";
import { Disclosure } from "../../ui/controls";
import { Icon } from "../../ui/icons";
import { ClaimRefContext, extractReferencedChartIds, extractReferencedTableIds, MarkdownContent, type ClaimRefs } from "../../ui/MarkdownContent";
import { useNow } from "../../ui/media";
import { Menu, type MenuEntry } from "../../ui/Menu";
import { showToast } from "../../ui/toast";
import { answerMarkdown, markdownToPlain } from "../../lib/answerCopy";
import { copyText } from "../../lib/clipboard";
import { CitationButton } from "./Citation";
import { AttributionLine, EvidenceViews, evidenceAlert, hasEvidenceViews } from "./EvidenceViews";
import { answerBody, cellText, columnHeader, conclusionText, draftNote, keyCalculations, shortPeriod, tableLayout } from "../../lib/answerBody";
import { ChartCard } from "./charts/ChartCard";
import { LiveActiveStream, LiveDraftPreview } from "./LiveStreamView";
import { latestFinds, liveStages } from "../../lib/liveStages";
import { QuestionParts } from "./QuestionParts";
import { ProcessTimeline } from "./ProcessTimeline";
import { LIVE_TURN, useTopicView, type LiveRun } from "./context";

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
  const periods = [...new Set(calc.inputs.map((input) => input.period).filter((p): p is string => Boolean(p)).map(shortPeriod))];
  const name = formulaLabel(calc.formula_id);
  // Dated periods read as a span, earliest first; other labels keep the formula's input order.
  const dated = periods.every((period) => /^\d{4}-\d{2}-\d{2}$/.test(period));
  if (periods.length === 0) return name;
  return dated ? `${name} · ${[...periods].sort().join(" → ")}` : `${name} · ${periods.join(" 对 ")}`;
}

export function usageMeta(turn: ResearchV2.ResearchTurn): string {
  const parts = [`${turn.usage.model_calls} 次模型调用`];
  if (turn.usage.search_calls > 0) parts.push(`${turn.usage.search_calls} 次搜索`);
  // partial_unknown: some call reported no usage, so the total includes reserved estimates for those calls.
  if (turn.usage.reported_tokens != null) {
    parts.push(`已回执 ${formatInteger(turn.usage.reported_tokens)} Token`);
    if (turn.usage.unknown_usage_calls) {
      parts.push(`${turn.usage.unknown_usage_calls} 次用量未知`);
      if (turn.usage.reserved_estimate_tokens != null) parts.push(`预留估算 ${formatInteger(turn.usage.reserved_estimate_tokens)} Token`);
    }
  } else parts.push(turn.usage.usage_status === "partial_unknown"
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

async function copyWithToast(text: string, done: string): Promise<boolean> {
  const ok = await copyText(text);
  showToast(ok ? done : "无法访问剪贴板", { tone: ok ? "info" : "error" });
  return ok;
}

/** The question bubble; copy (and, for the latest turn, edit) appear on hover or focus. */
export function AskBubble({ text, focusLabel, tools }: { text: string; focusLabel?: string | null; tools?: ReactNode }) {
  return (
    <div className="ask-wrap">
      <div className="ask">
        {focusLabel ? <span className="focus-ref">{focusLabel}</span> : null}
        {text}
      </div>
      <div className="ask-tools">
        {tools}
        <button type="button" className="icon-btn tool" aria-label="复制问题" title="复制问题" onClick={() => void copyWithToast(text, "已复制问题")}>
          <Icon name="copy" size={15} />
        </button>
      </div>
    </div>
  );
}

/** Edit-and-resend in place of the bubble: Ctrl/⌘+Enter sends, Esc cancels. */
export function AskEditor({ initial, onDone }: { initial: string; onDone: (question: string | null) => void }) {
  const [value, setValue] = useState(initial);
  const ready = value.trim().length > 0;
  return (
    <div className="ask-wrap ask-editing">
      <div className="ask-edit">
        <textarea
          aria-label="编辑问题"
          value={value}
          autoFocus
          rows={3}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") { event.preventDefault(); onDone(null); }
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && !event.nativeEvent.isComposing && ready) { event.preventDefault(); onDone(value.trim()); }
          }}
        />
        <div className="actions-row">
          <button type="button" className="btn plain" onClick={() => onDone(null)}>取消</button>
          <button type="button" className="btn primary" disabled={!ready} onClick={() => onDone(value.trim())}>发送</button>
        </div>
      </div>
    </div>
  );
}

/** Extra answer actions supplied by the page (regenerate, fork); copying is always available. */
export interface AnswerTools {
  primary?: ReactNode;
  menu?: MenuEntry[];
}

function missingParts(turn: ResearchV2.ResearchTurn): string[] {
  return (turn.question_parts ?? [])
    .filter((part) => part.status !== "answered")
    .map((part) => {
      const reason = reviewReasonText(part.reason, part.status);
      if (reason) return `${part.question}：${reason}`;
      return part.status === "partial" ? `${part.question}：只回答了一部分` : part.question;
    });
}

/**
 * What is still missing comes first, in plain words; the technical stop reason only shows when the
 * run was cut short (a model that submitted its result is not a reason the user can act on).
 */
export function GapCallout({ turn, actions, tone }: { turn: ResearchV2.ResearchTurn; actions: TurnActions | null; tone: "partial" | "completed" }) {
  const gaps = turn.gaps ?? [];
  const parts = tone === "partial" ? missingParts(turn) : [];
  const needsMarketKey = gaps.some((gap) => gap.code === "market_data_key_required");
  if (turn.lifecycle_status === "completed" || turn.lifecycle_status === "stopped") {
    const descriptions = gaps.map((gap) => gap.code === "model_reported" ? gap.message : gapText(gap));
    const first = gaps.findIndex((gap) => gap.code === "model_reported" && gap.message.trim());
    const headline = descriptions[first] ?? parts[0] ?? descriptions[0];
    const remaining = [...new Set([...descriptions, ...parts])].filter((line) => line && line !== headline);
    if (!headline && !turn.partial_notes?.trim()) return null;
    return <div className="meta" role="note" aria-label="结论适用范围">
      {headline && <p>{headline}</p>}
      {needsMarketKey && <Link className="link" to="/settings?tab=data">前往设置，添加自己的 Twelve Data key</Link>}
      {(remaining.length > 0 || turn.partial_notes?.trim()) && <details>
        <summary>查看依据与限制</summary>
        {remaining.length > 0 && <ul>{remaining.map((line) => <li key={line}>{line}</li>)}</ul>}
        {turn.partial_notes?.trim() && <PartialNotesBlock notes={turn.partial_notes} />}
      </details>}
    </div>;
  }
  if (tone === "completed" && gaps.length === 0) return null;
  const lines = [...parts, ...gaps.map((gap) => gapText(gap))];
  const shown = lines.slice(0, 3);
  const hidden = lines.length - shown.length;
  const explained = turn.reason !== "submitted" || Boolean(turn.stopping_note?.trim());
  const stopReason = tone === "partial" && explained ? turnReasonLabel(turn.reason, turn.stopping_note) : null;
  const recoveryReason = tone === "partial" && actions?.recovery?.reason
    ? recoveryReasonLabel(actions.recovery.reason)
    : null;
  return (
    <div className="callout" role="note">
      <span className="h">
        <span>{tone === "partial" ? "研究限制" : "仍有缺口"}</span>
        {parts.length > 0 ? <span className="h-count">还差 {parts.length} 项</span> : null}
      </span>
      {shown.length > 0 ? (
        <ul>
          {shown.map((line, index) => <li key={index}>{line}</li>)}
        </ul>
      ) : null}
      {hidden > 0 ? <span className="why">另有 {hidden} 项，在研究记录里查看。</span> : null}
      {needsMarketKey ? (
        <span className="why" data-hint="market-data-key">
          想要行情数据？<Link className="link" to="/settings?tab=data">前往设置，添加自己的 Twelve Data key</Link>
        </span>
      ) : null}
      {stopReason ? <span className="why">{stopReason}</span> : null}
      {recoveryReason ? <span className="why">{recoveryReason}</span> : null}
      {tone === "partial" && actions ? <div className="actions"><button type="button" className="btn" disabled={actions.locked} onClick={actions.onAskAgain}>重新提问</button><Menu label="更多" trigger="更多" triggerClassName="btn" items={[{ key: "add", label: "添加资料", icon: "plus", onSelect: actions.onAddSources, disabled: actions.locked }]} /></div> : null}

    </div>
  );
}

function StatLine({ calculations, turnId }: { calculations: readonly ResearchV2.ResearchCalculation[]; turnId: string }) {
  const view = useTopicView();
  if (calculations.length === 0) return null;
  return (
    <div className="stats">
      {calculations.map((calc) => {
        const quantity = formatQuantity(calc.value, calc.unit, calc.currency);
        return (
          <button key={calc.calculation_id} type="button" className="stat" onClick={() => view.openPanel({ kind: "record", tab: "numbers", turnKey: turnId })}>
            <b>{quantity.value}{quantity.unit ? <small> {quantity.unit}</small> : null}</b>
            <span>{calculationTitle(calc)}</span>
          </button>
        );
      })}
    </div>
  );
}

/** Usage and per-attempt accounting; shown in the record panel, not under the answer. */
export function TurnUsage({ turn }: { turn: ResearchV2.ResearchTurn }) {
  return (
    <div className="turn-usage">
      <p className="meta">{usageMeta(turn)}</p>
      {turn.attempts?.length ? (
        <Disclosure summary="每次执行的用量和成果">
          <ol>{turn.attempts.map((attempt, index) => (
            <li key={attempt.operation_id}>
              {index === 0 ? "首次执行" : `续跑 ${index}`} · {attempt.model_calls} 次模型调用 · {attempt.search_calls} 次搜索
              {attempt.elapsed_seconds != null && ` · ${formatDuration(attempt.elapsed_seconds * 1000)}`}
              {attempt.reported_tokens != null && ` · 已回执 ${formatInteger(attempt.reported_tokens)} Token`}
              {Boolean(attempt.unknown_usage_calls) && ` · ${attempt.unknown_usage_calls} 次用量未知`}
              {attempt.reserved_estimate_tokens != null && attempt.reserved_estimate_tokens > 0 && ` · 预留估算 ${formatInteger(attempt.reserved_estimate_tokens)} Token`}
              <div>新增事实 {attempt.new_facts} 条、计算 {attempt.new_calculations} 项、主张 {attempt.new_claims} 条，新回答 {attempt.answered_parts} 项。{turnReasonLabel(attempt.reason)}</div>
            </li>
          ))}</ol>
          <p className="note">实际费用未取得供应商账单。</p>
        </Disclosure>
      ) : null}
    </div>
  );
}

function ClaimList({ claims, showRole, className = "claims" }: { claims: readonly ResearchV2.ResearchClaim[]; showRole: boolean; className?: string }) {
  const view = useTopicView();
  if (claims.length === 0) return null;
  return (
    <ul className={className}>
      {claims.map((claim) => {
        const role = claimRole(claim);
        const claimId = claim.claim_id ?? "";
        return (
          <li key={claimId || claim.text} id={claimId ? `claim-${claimId}` : undefined}>
            {showRole ? <span className={`role ${role}`}>{ROLE_LABEL[role]}</span> : null}
            <span>
              <MarkdownContent content={claim.text} variant="inline" />
              {(claim.citations ?? []).map((citation, index) => (
                <CitationButton key={index} n={view.citations.numberFor(citation)} citation={citation} claimId={claimId} citationIndex={index} />
              ))}
              {claim.reasoning ? <span className="note">推断依据 · {claim.reasoning}</span> : null}
              {claim.qualification ? <span className="note">适用条件 · {claim.qualification}</span> : null}
              {claim.edited_by_user ? <span className="edited">你修改过</span> : null}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

const LIST_START = /^(\s*[-*+]\s|\s*\d+[.)]\s|\s*>|\s*\||\s*```|\s*~~~|\s*\[\[(chart|table):)/;

/**
 * Splits the report's opening from the rest, so the direct answer is set as the lead with the key
 * numbers right under it. A paragraph the writer marked as the conclusion near the top becomes the
 * lead (answer first); otherwise the leading headings and the first prose paragraph do.
 */
export function splitLead(report: string): { lead: string; rest: string } {
  const lines = report.replace(/\r\n?/g, "\n").split("\n");
  let index = 0;
  while (index < lines.length && (lines[index]!.trim() === "" || /^#{1,6}\s/.test(lines[index]!))) index += 1;
  // Paragraph ranges [from, to) after the leading headings; stop at the first fence so code is never split.
  const ranges: [number, number][] = [];
  for (let i = index; i < lines.length && ranges.length < 6;) {
    if (lines[i]!.trim() === "") { i += 1; continue; }
    if (/^\s*(```|~~~)/.test(lines[i]!)) break;
    const from = i;
    while (i < lines.length && lines[i]!.trim() !== "") i += 1;
    ranges.push([from, i]);
  }
  const text = ([from, to]: [number, number]) => lines.slice(from, to).join("\n");
  const prose = (range: [number, number]) => !lines.slice(range[0], range[1]).some((line) => LIST_START.test(line) || /^#{1,6}\s/.test(line));
  const marked = ranges.findIndex((range) => prose(range) && CONCLUSION_MARK.test(text(range)));
  const chosen = marked >= 0 ? ranges[marked]! : ranges[0] && prose(ranges[0]) ? ranges[0] : null;
  if (!chosen) return { lead: "", rest: report };
  const headings = lines.slice(0, index).join("\n").trim();
  const lead = [headings, text(chosen)].filter(Boolean).join("\n\n");
  const rest = [...lines.slice(index, chosen[0]), ...lines.slice(chosen[1])].join("\n").trim();
  return { lead, rest };
}

const CONCLUSION_MARK = /^\s*(?:\*\*|__)?\s*(?:核心判断|结论|总结|核心结论|一句话结论|Bottom line|Conclusion|Summary)\s*(?:\*\*|__)?\s*[:：]/i;

function FootToggle({ label, open, onToggle }: { label: string; open: boolean; onToggle: () => void }) {
  return (
    <button type="button" className="foot-btn" aria-expanded={open} onClick={onToggle}>
      {label}
      <Icon name={open ? "up" : "down"} size={14} />
    </button>
  );
}

/**
 * Spec v6 answer order: what is missing (partial), the answer's lead, key numbers, the report,
 * counter-evidence, then one quiet row for evidence, parts, process and record links.
 */
export function AnswerBlock({ turn, actions, tools, waiting = false }: { turn: ResearchV2.ResearchTurn; actions: TurnActions | null; tools?: AnswerTools | undefined; waiting?: boolean }) {
  const view = useTopicView();
  const items = turnContent(view.content, turn);
  const partial = turn.status === "partial";
  const lifecycle = turn.lifecycle_status;
  const body = answerBody(turn);
  const reportText = body?.text ?? "";
  const isDraft = body?.kind === "draft";
  // A draft supersedes the reviewed fragments the run kept as notes.
  const partialNotes = partial && !isDraft ? (turn.partial_notes ?? "").trim() : "";
  const events = useMemo(() => parseTurnEvents(turn.events), [turn.events]);
  const rows = useMemo(() => stepRows(events), [events]);
  const toolSteps = useMemo(() => toolStepCount(events), [events]);
  const [processOpen, setProcessOpen] = useState(false);
  const parts = turn.question_parts ?? [];
  const [partsOpen, setPartsOpen] = useState(partial && !reportText);
  const [checksOpen, setChecksOpen] = useState(false);
  const alert = useMemo(() => evidenceAlert(turn), [turn]);
  const attributions = useMemo(() => parseAttributions(turn.attributions), [turn.attributions]);
  const { lead, rest } = useMemo(() => splitLead(reportText), [reportText]);

  const supporting = items.claims.filter((claim) => claimRole(claim) === "support");
  const elevated = items.claims.filter((claim) => claimRole(claim) !== "support");
  const evidenceOpen = view.evidence.get(turn.turn_id) ?? !reportText;

  const claimRefs = useMemo<ClaimRefs>(() => ({
    label: (claimId) => {
      const claim = view.content.claims?.find((item) => item.claim_id === claimId);
      const numbers = [...new Set((claim?.citations ?? []).map((citation) => view.citations.numberFor(citation)))];
      return numbers.length > 0 ? numbers.join(",") : null;
    },
    open: (claimId) => view.gotoClaim(claimId),
  }), [view]);

  const chartsById = useMemo(() => {
    const map = new Map<string, ResearchV2.ResearchChart>();
    for (const chart of view.content.charts ?? []) {
      map.set(chart.chart_id, chart);
    }
    return map;
  }, [view.content.charts]);

  const tablesById = useMemo(() => new Map((view.content.tables ?? []).map((table) => [table.table_id,
    <ResearchTableView key={table.table_id} table={table} />,
  ])), [view.content.tables]);

  const inlineTableIds = useMemo(() => extractReferencedTableIds(reportText), [reportText]);
  const trailingTables = (view.content.tables ?? []).filter((table) =>
    (turn.table_ids ?? []).includes(table.table_id) && !inlineTableIds.has(table.table_id));

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

  const answered = parts.filter((part) => part.status === "answered").length;
  const recordLinks = answerSummary(turn, view.content).filter((part) => part.tab === "numbers" || part.tab === "sources");
  const steps = toolSteps > 0 ? toolSteps : rows.length;
  const [copied, setCopied] = useState(false);
  const markdown = () => answerMarkdown(turn, {
    claims: view.content.claims ?? [], turnClaims: items.claims, tables: view.content.tables ?? [], charts: view.content.charts ?? [],
    citations: view.citations, sources: view.sources, sourceTitle: view.sourceTitle,
  });
  const copyAnswer = async () => {
    if (await copyWithToast(markdown(), "已复制回答")) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }
  };
  const copyMenu: MenuEntry[] = [
    { key: "plain", label: "复制为纯文本", icon: "copy", onSelect: () => void copyWithToast(markdownToPlain(markdown()), "已复制回答") },
    { key: "question", label: "复制问题", icon: "copy", onSelect: () => void copyWithToast(turn.question, "已复制问题") },
    ...(tools?.menu?.length ? ["separator" as const, ...tools.menu] : []),
  ];

  return (
    <div className="answer">
      <AnswerStatus turn={turn} waiting={waiting} draft={isDraft} />
      {isDraft ? <p className="draft-note" role="note">{draftNote(items.claims.length > 0)}</p> : null}
      <ClaimRefContext.Provider value={claimRefs}>
        {lead ? (
          <div className="report answer-lead">
            <MarkdownContent content={lead} variant="report" />
          </div>
        ) : null}
        <StatLine calculations={keyCalculations(items.calculations, reportText || null)} turnId={turn.turn_id} />
        {rest ? (
          <div className="report">
            <MarkdownContent
              content={rest}
              variant="report"
              chartsById={chartsById}
              renderChart={renderChart}
              tablesById={tablesById}
            />
          </div>
        ) : null}
      </ClaimRefContext.Provider>
      {!reportText && partial && items.claims.length === 0 ? (
        <p>{partialSummary({ facts: items.facts.length, calculations: items.calculations.length, claims: items.claims.length,
          groundedParts: parts.filter((part) => Boolean(part.answer?.trim()) && (part.references?.length ?? 0) > 0).length })}</p>
      ) : null}
      {trailingCharts.length > 0 ? (
        <div className="turn-charts">
          {trailingCharts.map((chart) => (
            <ChartCard key={chart.chart_id} chart={chart} />
          ))}
        </div>
      ) : null}
      {trailingTables.map((table) => <div key={table.table_id}>{tablesById.get(table.table_id)}</div>)}
      <ClaimList claims={elevated} showRole className="claims elevated" />
      <GapCallout turn={turn} actions={actions} tone={partial ? "partial" : "completed"} />
      {alert ? <p className="note evidence-alert">{alert}<button type="button" className="link" onClick={() => setChecksOpen(true)}>查看核查</button></p> : null}
      <AttributionLine items={attributions} />
      {!lifecycle && partialNotes ? <PartialNotesBlock notes={partialNotes} /> : null}
      <div className="answer-foot">
        {supporting.length > 0 ? (
          <FootToggle label={`依据 ${supporting.length} 条`} open={evidenceOpen} onToggle={() => view.setEvidence(turn.turn_id, !evidenceOpen)} />
        ) : null}
        {parts.length > 0 ? (
          <FootToggle label={`已回答 ${answered}/${parts.length} 部分`} open={partsOpen} onToggle={() => setPartsOpen((open) => !open)} />
        ) : null}
        {rows.length > 0 ? (
          <FootToggle label={`过程 ${steps} 步`} open={processOpen} onToggle={() => setProcessOpen((open) => !open)} />
        ) : null}
        {hasEvidenceViews(turn) ? (
          <FootToggle label="数据与核查" open={checksOpen} onToggle={() => setChecksOpen((open) => !open)} />
        ) : null}
        {recordLinks.map((part) => (
          <button key={part.tab} type="button" className="foot-btn" onClick={() => view.openPanel({ kind: "record", tab: part.tab, turnKey: turn.turn_id })}>
            {part.label}
            <Icon name="chev" size={14} />
          </button>
        ))}
        {(turn.requirement_plan?.length || turn.semantic_reviews?.length) ? (
          <button type="button" className="foot-btn" onClick={() => view.openPanel({ kind: "record", tab: "steps", turnKey: turn.turn_id })}>问题要求与依据核验<Icon name="chev" size={14} /></button>
        ) : null}
        {turn.replaces_turn_id ? (
          <button type="button" className="foot-btn" title="上一次的回答保留在版本历史中" onClick={() => view.openPanel({ kind: "review" })}>
            重新生成的回答
          </button>
        ) : null}
        <span className="grow" />
        <div className="answer-tools" role="group" aria-label="回答操作">
          {tools?.primary}
          <button type="button" className="icon-btn tool" aria-label="复制回答" title="复制回答（Markdown，含来源）" onClick={() => void copyAnswer()}>
            <Icon name={copied ? "check" : "copy"} size={15} />
          </button>
          <Menu label="更多回答操作" trigger={<Icon name="more" size={16} />} triggerClassName="icon-btn tool" items={copyMenu} />
        </div>
      </div>
      {supporting.length > 0 ? (
        <div className="evidence" hidden={!evidenceOpen}>
          <div className="gtitle"><span className="role support">支持</span>{supporting.length}</div>
          <ClaimList claims={supporting} showRole={false} />
          <p className="note">{VERIFY_NOTE}</p>
        </div>
      ) : items.claims.length > 0 ? <p className="note">{VERIFY_NOTE}</p> : null}
      {partsOpen ? <QuestionParts turn={turn} /> : null}
      {checksOpen ? <EvidenceViews turn={turn} attribution={false} /> : null}
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
  );
}

/**
 * With several turns, the latest conclusion sits under the title so the page reads as one line of
 * inquiry; it links to the turn it comes from.
 */
export function TopicConclusion({ turns }: { turns: readonly ResearchV2.ResearchTurn[] }) {
  if (turns.length < 2) return null;
  const latest = [...turns].reverse().find((turn) => answerBody(turn));
  if (!latest) return null;
  const body = answerBody(latest)!;
  const text = conclusionText(body.text);
  // A one-paragraph answer is its own summary; repeating it at the top adds nothing.
  if (!text || text === conclusionText(body.text.replace(/\n\s*\n/g, " "))) return null;
  const number = displayTurnNumber(latest);
  return (
    <a className="topic-conclusion" href={`#turn-${latest.turn_id}`} onClick={(event) => {
      const target = document.getElementById(`turn-${latest.turn_id}`);
      if (!target) return;
      event.preventDefault();
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    }}>
      <span className="tc-label">当前结论 · 来自第 {number} 轮{body.kind === "draft" ? " · 未全部核验" : ""}</span>
      <span className="tc-text">{text}</span>
    </a>
  );
}

/** The answer's state in a word or two; a run that ended is not the same as an answer that is complete. */
function AnswerStatus({ turn, waiting, draft }: { turn: ResearchV2.ResearchTurn; waiting: boolean; draft: boolean }) {
  const stopped = turn.lifecycle_status === "stopped" || (!turn.lifecycle_status && String(turn.reason) === "provider_cancelled");
  // A submitted final result with documented limits is complete; a run that stopped short is not.
  const finished = turn.status === "completed" || (turn.lifecycle_status === "completed" && turn.reason === "submitted" && !draft);
  const label = waiting ? "等待你的补充"
    : stopped ? "已停止"
    : finished ? "已完成"
    : "部分完成";
  return <span className={`meta answer-status${!finished && !waiting && !stopped ? " partial" : ""}`}>{label}</span>;
}

function ResearchTableView({ table }: { table: ResearchV2.ResearchTable }) {
  const { rowLabels, columns } = tableLayout(table);
  return (
    <figure className="research-table">
      <figcaption>{table.title}{table.subtitle ? <span className="sub">{table.subtitle}</span> : null}</figcaption>
      <div className="md-table-wrap"><div className="md-table-scroll"><table>
        <thead><tr>{rowLabels ? <th scope="col"></th> : null}{columns.map((column) => <th scope="col" key={column.key}>{columnHeader(column)}</th>)}</tr></thead>
        <tbody>{table.rows.map((row, index) => (
          <tr key={index}>
            {rowLabels ? <th scope="row">{row.label ?? ""}</th> : null}
            {columns.map((column) => {
              const display = row.cells[column.key]?.display_value;
              return <td key={column.key} title={display ?? undefined}>{cellText(display, column.unit)}</td>;
            })}
          </tr>
        ))}</tbody>
      </table></div></div>
    </figure>
  );
}

export function TurnView({ turn, actions, first = false, tools, askTools, editing = false, onEditDone, waiting = false }: {
  turn: ResearchV2.ResearchTurn; actions: TurnActions | null; first?: boolean;
  tools?: AnswerTools | undefined; askTools?: ReactNode; editing?: boolean; onEditDone?: (question: string | null) => void; waiting?: boolean;
}) {
  const view = useTopicView();
  const number = displayTurnNumber(turn);
  const focus = turn.focus_citation;
  let focusLabel: string | null = null;
  if (focus) {
    const claim = view.content.claims?.find((item) => item.claim_id === focus.claim_id);
    const citation = claim?.citations?.[focus.citation_index];
    focusLabel = citation ? `深挖引用 ${view.citations.numberFor(citation)}` : "深挖引用";
  }
  // The first question is already the page's large title; repeating it adds nothing.
  return (
    <section id={`turn-${turn.turn_id}`} className={first ? "turn turn-first" : "turn"} aria-label={`第 ${number} 轮`}>
      {!first ? <TurnStamp number={number} iso={turn.started_at} /> : null}
      {editing && onEditDone ? (
        <AskEditor initial={turn.question} onDone={onEditDone} />
      ) : !first || focusLabel ? (
        <AskBubble text={turn.question} focusLabel={focusLabel} tools={askTools} />
      ) : null}
      {(turn.user_instructions ?? []).map((instruction) => (
        <div key={instruction.instruction_id}><AskBubble text={instruction.text} /><span className="meta">{instructionReceiptLabel(instruction.status)}</span><InstructionDelivery instruction={instruction} /></div>
      ))}
      <AnswerBlock turn={turn} actions={actions} tools={tools} waiting={waiting} />
    </section>
  );
}

function instructionReceiptLabel(status: string) {
  if (status === "included") return "已纳入本次研究";
  if (status === "awaiting_receipt") return "模型调用回执待确认";
  return "已收到";
}

function InstructionDelivery({ instruction }: { instruction: ResearchV2.SavedResearchInstruction }) {
  const view = useTopicView();
  const labels: Record<string, string> = { linked_for_review: "已关联依据，待核实", unresolved: "尚未核实", limited: "受资料或权限限制", superseded: "已被后续要求取代", supersession_needs_review: "已定位原文，取代关系待核验" };
  return <div className="meta">
    {instruction.delivery_status ? (labels[instruction.delivery_status] ?? "尚未核实") : "尚未核实"}
    {reviewReasonText(instruction.reason) ? ` · ${reviewReasonText(instruction.reason)}` : null}
    {(instruction.text_locations?.length ?? 0) > 0 ? <details><summary>查看相关原文</summary>{instruction.text_locations!.map((location, index) => <p key={index}>{location.field === "partial_notes" ? "过程说明" : "报告"}{location.instruction_sequence != null ? ` · 指令 ${location.instruction_sequence}` : ""}：{location.excerpt}</p>)}</details> : null}
    {(instruction.references ?? []).map((ref, index) => {
      const id = typeof ref === "string" ? ref : ref.claim_id ?? ref.fact_id ?? ref.calculation_id;
      if (!id) return null;
      const claim = view.content.claims?.find((item) => item.claim_id === id);
      const fact = view.content.facts?.find((item) => item.fact_id === id);
      const calculation = view.content.calculations?.find((item) => item.calculation_id === id);
      if (!claim && !fact && !calculation) return null;
      return <button key={`${id}-${index}`} type="button" className="link" onClick={() => {
        if (claim) view.gotoClaim(id);
        else if (fact) view.openPanel({ kind: "reader", sourceId: fact.source_id, range: [fact.character_start, fact.character_end] });
        else view.openPanel({ kind: "record", tab: "numbers", turnKey: instruction.turn_id });
      }}>查看{claim ? "结论" : fact ? "原文" : "计算"} · {id}</button>;
    })}
  </div>;
}

/** A run that stopped before saving any turn (spec §4.3 回答：中断或已取消). */
export function InterruptedTurn({ number, startedAt, question, status, actions, events = [], first = false }: {
  number: number; startedAt: string; question: string; status: "failed" | "cancelled"; actions: TurnActions; events?: ProgressEvent[]; first?: boolean;
}) {
  const reason = actions.recovery?.reason ? recoveryReasonLabel(actions.recovery.reason) : null;
  return (
    <section className={first ? "turn turn-first" : "turn"} aria-label={`第 ${number} 轮`}>
      {!first ? <TurnStamp number={number} iso={startedAt} /> : null}
      {question && !first ? <AskBubble text={question} /> : null}
      <div className="answer">
        <div className="callout error" role="note">
          <span className="h">{status === "cancelled" ? "已停止" : "运行中断"}</span>
          <span>{status === "cancelled" ? "研究已停止。以下显示已保存的进度。" : "这一轮没有完成，也没有保存新的版本。"}</span>
          {reason ? <span className="why">{reason}</span> : null}
          <div className="actions"><button type="button" className="btn" disabled={actions.locked} onClick={actions.onAskAgain}>重新提问</button><Menu label="更多" trigger="更多" triggerClassName="btn" items={[{ key: "add", label: "添加资料", icon: "plus", onSelect: actions.onAddSources, disabled: actions.locked }]} /></div>

        </div>
        {events.length > 0 ? <ProcessTimeline events={events} initialTailLimit={3} /> : null}
      </div>
    </section>
  );
}

/**
 * The only place a run's state shows while it works: one line (status, what has been found so far,
 * elapsed time) above the live timeline. Stopping lives in the composer's send button.
 */
export function LiveTurn({ number, startedAt, question, live = null, sectionRef, onDraftGrow, continuing = null, first = false, editedQuestion = null }: {
  number: number;
  startedAt: string;
  question: string;
  live?: LiveRun | null;
  sectionRef?: Ref<HTMLElement>;
  onDraftGrow?: (() => void) | undefined;
  continuing?: "continue" | "deliver" | "regenerate" | null | undefined;
  first?: boolean;
  /** For edit-and-resend: the edited question, shown since it differs from the answer being replaced. */
  editedQuestion?: string | null;
}) {
  const view = useTopicView();
  const now = useNow(true);
  const [expanded, setExpanded] = useState(true);
  const progress = live?.progress ?? null;
  const status = progress?.operation_status ?? live?.operation.status ?? "pending";
  const isPending = status === "pending";
  const startIso = progress?.started_at ?? live?.operation.created_at ?? startedAt;
  const startedMs = Date.parse(startIso);
  const elapsed = Number.isNaN(startedMs) ? "" : formatClock(Math.max(0, now - startedMs));
  const found = formatInterimParts(progress?.interim).join(" · ");
  const events = live?.events ?? [];
  const current = progress?.current ?? null;
  const hasTimeline = events.length > 0 || current !== null;
  const isContinue = continuing === "continue" || continuing === "deliver" || continuing === "regenerate";
  const statusTitle = continuing === "regenerate"
    ? (isPending ? "排队中 · 重新生成" : "正在重新生成")
    : continuing === "deliver"
    ? (isPending ? "排队中 · 交付已有成果" : "正在交付已有成果")
    : continuing === "continue"
      ? (isPending ? "排队中 · 继续研究" : "正在继续研究")
      : (isPending ? "排队中" : "正在研究");

  return (
    <section
      ref={sectionRef}
      className={isContinue ? "turn turn-continuation" : first ? "turn turn-first" : "turn"}
      aria-label={continuing === "regenerate" ? `第 ${number} 轮重新生成` : isContinue ? `第 ${number} 轮继续研究` : `第 ${number} 轮`}
      aria-busy="true"
    >
      {!isContinue && !first ? <TurnStamp number={number} iso={startedAt} /> : null}
      {!isContinue && !first && question ? <AskBubble text={question} /> : null}
      {continuing === "regenerate" && editedQuestion ? <AskBubble text={editedQuestion} /> : null}
      {(progress?.instructions ?? []).slice().sort((a, b) => a.sequence - b.sequence).map((instruction) => (
        <div key={instruction.instruction_id}><AskBubble text={instruction.text} /><span className="meta">{instructionReceiptLabel(instruction.status)}</span></div>
      ))}
      <div className="answer live-turn">
        <div className="live-turn-head">
          <span className="dot running" aria-hidden="true" />
          <button
            type="button"
            className="live-turn-title"
            title="打开过程面板"
            disabled={!live}
            onClick={() => view.openPanel({ kind: "record", tab: "steps", turnKey: LIVE_TURN })}
          >
            {statusTitle}
          </button>
          {found ? <span className="live-turn-meta">{found}</span> : null}
          <span className="grow" />
          {elapsed ? <span className="live-turn-time">{elapsed}</span> : null}
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
          <LiveStagesView
            events={events}
            current={current}
            idleText={hasTimeline ? null : isPending ? "任务已提交，等待调度开始…" : "正在准备研究上下文…"}
            stream={live?.stream}
            onOpenProcess={live ? () => view.openPanel({ kind: "record", tab: "steps", turnKey: LIVE_TURN }) : null}
          />
        ) : null}
        <LiveDraftPreview stream={live?.stream} onGrow={onDraftGrow} />
      </div>
    </section>
  );
}

export const PendingTurn = LiveTurn;

/**
 * Where the run is, in five plain stages; then the model's latest note in its own words, what it is
 * doing now and what it has just found. Its raw thinking is one click away, not on the page.
 */
function LiveStagesView({ events, current, idleText, stream, onOpenProcess }: {
  events: ProgressEvent[]; current: CurrentActivity | null; idleText: string | null;
  stream?: LiveRun["stream"] | undefined; onOpenProcess: (() => void) | null;
}) {
  const [thinkingOpen, setThinkingOpen] = useState(false);
  const stages = useMemo(() => liveStages(events, current), [events, current]);
  const finds = useMemo(() => latestFinds(events), [events]);
  const note = useMemo(() => {
    for (const event of [...events].reverse()) if (event.kind === "model" && event.note?.trim()) return event.note.trim();
    return null;
  }, [events]);
  const target = current?.kind === "tool" && current.target && current.target !== current.rationale ? current.target : null;
  const now = current
    ? current.kind === "tool" ? toolLabel(current.name) : modelPhaseLabel(current.phase, current.retry_attempt)
    : idleText;
  const why = current?.kind === "tool" ? current.rationale ?? null : null;
  return (
    <div className="live-stages">
      <ol className="stage-row" aria-label="研究阶段">
        {stages.map((stage) => (
          <li key={stage.key} className={`stage ${stage.state}`} aria-current={stage.state === "active" ? "step" : undefined}>
            <span className="stage-dot" aria-hidden="true" />
            <span className="stage-label">{stage.label}</span>
            {stage.detail ? <span className="stage-detail">{stage.detail}</span> : null}
          </li>
        ))}
      </ol>
      {note ? <p className="live-note">{note}</p> : null}
      {now ? (
        <p className="live-now">
          <span className="live-now-label">{now}</span>
          {why ? <span>{why}</span> : null}
          {target ? <span className="live-target">{target}</span> : null}
        </p>
      ) : null}
      {finds.length > 0 ? <p className="live-finds">刚获取：{finds.map((title) => `《${title}》`).join("、")}</p> : null}
      <div className="live-links">
        {stream ? (
          <button type="button" className="link" aria-expanded={thinkingOpen} onClick={() => setThinkingOpen((open) => !open)}>
            {thinkingOpen ? "收起模型输出" : "查看模型输出"}
          </button>
        ) : null}
        {onOpenProcess && events.length > 0 ? (
          <button type="button" className="link" onClick={onOpenProcess}>查看完整过程 ›</button>
        ) : null}
      </div>
      {thinkingOpen && stream ? <LiveActiveStream stream={stream} /> : null}
    </div>
  );
}
