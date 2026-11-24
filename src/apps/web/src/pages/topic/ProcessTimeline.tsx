import { useMemo, useState } from "react";
import type {
  AcquireSourcesSummary,
  CalculateFinancialsSummary,
  CreateChartSummary,
  CurrentActivity,
  NewsSearchSummary,
  SearchLibrarySummary,
  HistoricalPricesSummary,
  ProgressEvent,
  ReadSourcesSummary,
  RecordFactsSummary,
  SecCompanyConceptSummary,
  ToolEventSummary,
} from "../../api/types";
import {
  ATTRIBUTION_NOTE_LABEL,
  chartTypeLabel,
  formulaLabel,
  LIVE_STAGED_TAG,
  modelPhaseLabel,
  PROCESS_NOTE_LABEL,
  toolLabel,
  toolReasonLabel,
  toolStatusLabel,
} from "../../lib/copy";
import { formatDateTime, formatInteger, formatQuantity } from "../../lib/format";
import { stepRows, type StepRow } from "../../lib/research";
import { Icon, toolIcon } from "../../ui/icons";
import { MarkdownContent } from "../../ui/MarkdownContent";
import { MEDIA, useMedia } from "../../ui/media";
import type { LiveStreamStore } from "./context";
import { LiveActiveStream, LiveActiveStreamRow } from "./LiveStreamView";

function shortDate(iso: string | null): string | null {
  if (!iso) return null;
  const formatted = formatDateTime(iso);
  return formatted ? formatted.slice(0, 10) : iso.slice(0, 10);
}

function DiscoverSummaryView({ summary }: { summary: NewsSearchSummary }) {
  const [open, setOpen] = useState(false);
  const countParts = [`发现 ${formatInteger(summary.found_count)} 条线索`];
  if (summary.cluster_count !== undefined && summary.cluster_count > 0) countParts.push(`，归并为 ${formatInteger(summary.cluster_count)} 组报道`);
  if (summary.eligible_count > 0 || summary.denied_count > 0) {
    const detail = [`可用 ${formatInteger(summary.eligible_count)}`];
    if (summary.denied_count > 0) detail.push(`受限 ${formatInteger(summary.denied_count)}`);
    countParts.push(`（${detail.join(" · ")}）`);
  }
  return (
    <div className="pt-summary">
      <div className="pt-sub-row">
        <span className="pt-sub">{countParts.join("")}</span>
        {summary.leads_preview.length > 0 ? (
          <button type="button" className="link pt-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
            {open ? "收起线索" : `展开线索 (${summary.leads_preview.length})`}
          </button>
        ) : null}
      </div>
      {open && summary.leads_preview.length > 0 ? (
        <ul className="pt-leads">
          {summary.leads_preview.map((lead, idx) => {
            const meta = [lead.host, shortDate(lead.published_at)].filter(Boolean).join(" · ");
            return (
              <li key={`${lead.host}-${idx}`} className="pt-lead">
                <span className="pt-lead-title">{lead.title}</span>
                {meta ? <span className="meta">{meta}</span> : null}
              </li>
            );
          })}
        </ul>
      ) : null}
      {summary.attributions?.includes("gdelt") ? (
        <span className="meta pt-attribution">{`${ATTRIBUTION_NOTE_LABEL}：`}<a className="link" href="https://www.gdeltproject.org/" target="_blank" rel="noopener noreferrer">The GDELT Project</a></span>
      ) : null}
    </div>
  );
}

function LibrarySummaryView({ summary }: { summary: SearchLibrarySummary }) {
  const parts = [`命中 ${formatInteger(summary.hit_count)} 处`, `覆盖 ${formatInteger(summary.source_count)} 份资料`];
  if (summary.unsearched_pages > 0) parts.push(`${formatInteger(summary.unsearched_pages)} 页尚未提取文字，未检索`);
  return (
    <div className="pt-summary">
      <span className="pt-sub">{parts.join(" · ")}</span>
      {summary.hits_preview.length > 0 ? (
        <ul className="pt-items">
          {summary.hits_preview.map((hit, idx) => (
            <li key={`${hit.title}-${idx}`} className="pt-item">
              <span className="pt-item-title">{hit.title}</span>
              {hit.page !== null ? <span className="meta">{`第 ${hit.page} 页`}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function AcquireSummaryView({ summary }: { summary: AcquireSourcesSummary }) {
  const countParts: string[] = [];
  if (summary.acquired_count > 0) countParts.push(`已获取 ${formatInteger(summary.acquired_count)} 份`);
  if (summary.failed_count > 0) countParts.push(`未获取 ${formatInteger(summary.failed_count)} 份`);
  return (
    <div className="pt-summary">
      {countParts.length > 0 ? <span className="pt-sub">{countParts.join(" · ")}</span> : summary.items.length === 0 ? <span className="pt-sub">无获取结果</span> : null}
      {summary.items.length > 0 ? (
        <ul className="pt-items">
          {summary.items.map((item, idx) => {
            const status = toolStatusLabel(item.status);
            const reason = toolReasonLabel(item.reason);
            const chars = item.total_characters !== null ? `${formatInteger(item.total_characters)} 字符` : null;
            const meta = [item.host && item.host !== item.title ? item.host : null, status, chars, reason].filter(Boolean).join(" · ");
            return (
              <li key={`${item.host}-${idx}`} className="pt-item">
                <span className="pt-item-title">{item.title || item.host || status}</span>
                {meta ? <span className="meta">{meta}</span> : null}
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}

function SecSummaryView({ summary }: { summary: SecCompanyConceptSummary }) {
  const concept = `${summary.entity} · ${summary.taxonomy ? `${summary.taxonomy}:` : ""}${summary.tag}${summary.unit ? ` (${summary.unit})` : ""}`;
  const meta = [
    summary.title,
    toolStatusLabel(summary.status),
    summary.total_characters !== null ? `${formatInteger(summary.total_characters)} 字符` : null,
  ].filter(Boolean).join(" · ");
  return (
    <div className="pt-summary">
      <span className="pt-sub">{concept}</span>
      {meta ? <span className="meta">{meta}</span> : null}
    </div>
  );
}

function PricesSummaryView({ summary }: { summary: HistoricalPricesSummary }) {
  const range = `${summary.instrument_id} (${summary.start_date} ~ ${summary.end_date})`;
  const meta = [summary.title, toolStatusLabel(summary.status)].filter(Boolean).join(" · ");
  return (
    <div className="pt-summary">
      <span className="pt-sub">{range}</span>
      {meta ? <span className="meta">{meta}</span> : null}
    </div>
  );
}

function ReadSummaryView({ summary }: { summary: ReadSourcesSummary }) {
  if (summary.items.length === 0) {
    return <span className="pt-sub">{summary.read_count > 0 ? `已读取 ${formatInteger(summary.read_count)} 段资料` : "未读取资料"}</span>;
  }
  return (
    <ul className="pt-items">
      {summary.items.map((item, idx) => {
        const range = `第 ${formatInteger(item.start)}–${formatInteger(item.end)} 字符${item.total_characters !== null ? ` / 共 ${formatInteger(item.total_characters)} 字符` : ""}`;
        return (
          <li key={`${item.source_id}-${item.start}-${idx}`} className="pt-item">
            <span className="pt-item-title">{item.title || item.source_id}</span>
            <span className="meta">{range}</span>
          </li>
        );
      })}
    </ul>
  );
}

function FactsSummaryView({ summary, live }: { summary: RecordFactsSummary; live: boolean }) {
  return (
    <div className="pt-summary">
      <div className="pt-sub-row">
        <span className="pt-sub">{`已记录 ${formatInteger(summary.recorded_count)} 个数字`}</span>
        {live ? <span className="pt-staged">{LIVE_STAGED_TAG}</span> : null}
      </div>
      {summary.facts_preview.length > 0 ? (
        <div className="pt-chips">
          {summary.facts_preview.map((fact, idx) => {
            const quantity = formatQuantity(fact.value, fact.unit, fact.currency);
            const valueText = `${quantity.value}${quantity.unit ? ` ${quantity.unit}` : ""}`;
            return (
              <span key={`${fact.label}-${fact.period}-${idx}`} className="pt-chip">
                <span className="pt-chip-k">{fact.label}{fact.period ? ` (${fact.period})` : ""}</span>
                <b>{valueText}</b>
              </span>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

function CalcsSummaryView({ summary, live }: { summary: CalculateFinancialsSummary; live: boolean }) {
  return (
    <div className="pt-summary">
      <div className="pt-sub-row">
        <span className="pt-sub">{`完成 ${formatInteger(summary.calculated_count)} 项计算`}</span>
        {live ? <span className="pt-staged">{LIVE_STAGED_TAG}</span> : null}
      </div>
      {summary.calcs_preview.length > 0 ? (
        <div className="pt-chips">
          {summary.calcs_preview.map((calc, idx) => {
            const quantity = formatQuantity(calc.value, calc.unit, calc.currency);
            const valueText = `${quantity.value}${quantity.unit ? ` ${quantity.unit}` : ""}`;
            return (
              <span key={`${calc.formula_id}-${idx}`} className="pt-chip" title={calc.expression}>
                <span className="pt-chip-k">{formulaLabel(calc.formula_id)}</span>
                <b>{valueText}</b>
              </span>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

function CreateChartSummaryView({ summary, live }: { summary: CreateChartSummary; live: boolean }) {
  const detail = summary.chart_type === "causal_graph"
    ? `${chartTypeLabel(summary.chart_type)} · ${formatInteger(summary.series_count)} 个节点 · ${formatInteger(summary.point_count)} 条传导关系`
    : `${chartTypeLabel(summary.chart_type)} · ${formatInteger(summary.series_count)} 条序列 · ${formatInteger(summary.point_count)} 个数据点`;
  return (
    <div className="pt-summary">
      <div className="pt-sub-row">
        <span className="pt-sub">{summary.title}</span>
        {live ? <span className="pt-staged">{LIVE_STAGED_TAG}</span> : null}
      </div>
      <span className="meta">{detail}</span>
    </div>
  );
}

function ToolSummaryView({ summary, live }: { summary: ToolEventSummary; live: boolean }) {
  switch (summary.kind) {
    case "news_search":
      return <DiscoverSummaryView summary={summary} />;
    case "search_library":
      return <LibrarySummaryView summary={summary} />;
    case "acquire_sources":
      return <AcquireSummaryView summary={summary} />;
    case "sec_company_concept":
      return <SecSummaryView summary={summary} />;
    case "historical_prices":
      return <PricesSummaryView summary={summary} />;
    case "read_sources":
      return <ReadSummaryView summary={summary} />;
    case "record_facts":
      return <FactsSummaryView summary={summary} live={live} />;
    case "calculate_financials":
      return <CalcsSummaryView summary={summary} live={live} />;
    case "create_chart":
      return <CreateChartSummaryView summary={summary} live={live} />;
  }
}

function TimelineStepRow({ row, live, inPanel = false }: { row: StepRow; live: boolean; inPanel?: boolean }) {
  if (row.kind === "model") {
    // Token accounting belongs to the record panel; under the answer a model step just says it ran.
    const failedCall = row.text.startsWith("模型调用失败");
    const usageText = inPanel || failedCall ? row.text : null;
    if (row.note) {
      return (
        <li className="pt-row pt-model-note">
          <span className="pt-ic" aria-hidden="true"><Icon name="sparkles" size={14} /></span>
          <div className="pt-main">
            <div className="pt-head">
              <span className="pt-note-label">{PROCESS_NOTE_LABEL}</span>
              {usageText ? <span className="meta">{usageText}</span> : null}
            </div>
            <div className="pt-note">
              <MarkdownContent content={row.note} variant="inline" />
            </div>
          </div>
        </li>
      );
    }
    const failed = row.text.startsWith("模型调用失败");
    // Under the answer, a model call with nothing to say is noise between the steps it chose.
    if (!inPanel && !failed) return null;
    return (
      <li className={`pt-row pt-model${failed ? " is-error" : ""}`}>
        <span className="pt-ic" aria-hidden="true">
          {failed ? <Icon name="alert" size={14} /> : <span className="pt-mini-dot" />}
        </span>
        <div className="pt-main">
          <span className="pt-model-text">{usageText ?? row.text.split(" · ")[0]}</span>
        </div>
      </li>
    );
  }

  const whyText = [row.rationale, row.reason].filter(Boolean).join(" · ");
  return (
    <li className="pt-row pt-tool">
      <span className="pt-ic" aria-hidden="true"><Icon name={toolIcon(row.tool)} size={15} /></span>
      <div className="pt-main">
        <div className="pt-head">
          <span className="pt-label">{row.label}</span>
          {row.status ? <span className={`role light ${row.tone}`}>{row.status}</span> : null}
        </div>
        {whyText ? <span className="pt-why">{whyText}</span> : null}
        {row.summary ? <ToolSummaryView summary={row.summary} live={live} /> : null}
      </div>
    </li>
  );
}

function CurrentStepRow({ current, stream }: { current: CurrentActivity; stream?: LiveStreamStore | undefined }) {
  const isTool = current.kind === "tool";
  const label = isTool ? toolLabel(current.name) : modelPhaseLabel(current.phase, current.retry_attempt);
  const rationale = isTool
    ? current.rationale || (current.name === "create_chart" ? "正在组装并校验图表数据与单位口径…" : null)
    : null;
  const target = isTool && current.target && current.target !== rationale ? current.target : null;
  return (
    <li className="pt-row pt-current">
      <span className="pt-ic" aria-hidden="true"><span className="dot running" /></span>
      <div className="pt-main">
        <div className="pt-head">
          <span className="pt-label">{label}</span>
          <span className="role light neutral">进行中</span>
        </div>
        {rationale ? <span className="pt-why">{rationale}</span> : null}
        {target ? <span className="pt-target">{target}</span> : null}
        {stream ? <LiveActiveStream stream={stream} /> : null}
      </div>
    </li>
  );
}

export interface ProcessTimelineProps {
  events: ProgressEvent[];
  current?: CurrentActivity | null;
  live?: boolean;
  stream?: LiveStreamStore | undefined;
  initialTailLimit?: number;
  inPanel?: boolean;
}

export function ProcessTimeline({ events, current = null, live = false, stream, initialTailLimit, inPanel = false }: ProcessTimelineProps) {
  const tablet = useMedia(MEDIA.tablet);
  const [showAll, setShowAll] = useState(false);
  const rows = useMemo(() => stepRows(events), [events]);

  if (rows.length === 0 && !current) {
    if (live && stream) {
      return (
        <ol className={`pt-list${inPanel ? " in-panel" : ""}`} aria-label="研究过程时间线">
          <LiveActiveStreamRow stream={stream} />
        </ol>
      );
    }
    return null;
  }

  const mobileLimit = !tablet ? 4 : undefined;
  const effectiveLimit =
    mobileLimit !== undefined && initialTailLimit !== undefined
      ? Math.min(mobileLimit, initialTailLimit)
      : mobileLimit ?? initialTailLimit;

  const activeCount = current ? 1 : 0;
  let visibleRows = rows;
  let hiddenCount = 0;
  if (!showAll && effectiveLimit !== undefined && rows.length + activeCount > effectiveLimit) {
    const keepCompleted = Math.max(0, effectiveLimit - activeCount);
    visibleRows = keepCompleted > 0 ? rows.slice(rows.length - keepCompleted) : [];
    hiddenCount = rows.length - visibleRows.length;
  }

  return (
    <ol className={`pt-list${inPanel ? " in-panel" : ""}`} aria-label="研究过程时间线">
      {hiddenCount > 0 ? (
        <li className="pt-earlier">
          <button type="button" className="pt-earlier-btn" onClick={() => setShowAll(true)}>
            {`展开前 ${hiddenCount} 步`}
          </button>
        </li>
      ) : null}
      {visibleRows.map((row) => <TimelineStepRow key={row.index} row={row} live={live} inPanel={inPanel} />)}
      {current ? <CurrentStepRow current={current} stream={live ? stream : undefined} /> : live && stream ? <LiveActiveStreamRow stream={stream} /> : null}
    </ol>
  );
}
