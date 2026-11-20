import { useMemo } from "react";
import type { ResearchV2 } from "@foresightnine/contracts";
import type { CoverageDocument, CoverageMapView, CoverageRow, ReconciliationRow, ReconciliationView, TurnAttributionView } from "../../api/types";
import { ATTRIBUTION_NOTE_LABEL, COVERAGE_STATUS_TONE, RECONCILIATION_BADGE_LABEL, SOURCE_CLASS_LABEL } from "../../lib/copy";
import { formatInteger } from "../../lib/format";
import { parseAttributions, parseCoverageMap, parseReconciliation } from "../../lib/research";
import { Disclosure } from "../../ui/controls";

const BADGE_TONE: Readonly<Record<string, string>> = {
  consistent: "support",
  rounding_only: "neutral",
  differs_minor: "review",
  differs_material: "error",
};

/** 数据截至: one line per category that has data, with a staleness badge against the research clock. */
export function stalenessLabel(row: CoverageRow): { text: string; stale: boolean } | null {
  if (row.is_stale === true) {
    const days = row.trading_days_behind;
    return { text: days !== null && days > 0 ? `已落后 ${formatInteger(days)} 个交易日` : "数据已过期", stale: true };
  }
  if (row.days_behind_clock !== null && row.days_behind_clock > 0) {
    return { text: `距研究时点 ${formatInteger(row.days_behind_clock)} 天`, stale: row.days_behind_clock >= 30 };
  }
  return null;
}

export function DataAsOfList({ coverage }: { coverage: CoverageMapView }) {
  const rows = coverage.rows.filter((row) => row.data_as_of);
  if (rows.length === 0) return null;
  return (
    <ul className="ev-asof" aria-label="数据截至">
      {rows.map((row) => {
        const stale = stalenessLabel(row);
        return (
          <li key={row.category}>
            <span className="ev-asof-k">{row.label.zh || row.category}</span>
            <span>数据截至 <b>{row.data_as_of}</b></span>
            {stale ? <span className={`role light ${stale.stale ? "review" : "neutral"}`} data-testid="staleness">{stale.text}</span> : null}
          </li>
        );
      })}
    </ul>
  );
}

function ReconRow({ row }: { row: ReconciliationRow }) {
  const tone = BADGE_TONE[row.badge] ?? "neutral";
  return (
    <tr>
      <th scope="row">
        <span className="ev-title">{row.title.zh || row.title.en}</span>
        <span className={`role light ${tone}`}>{RECONCILIATION_BADGE_LABEL[row.badge] ?? row.badge}</span>
      </th>
      <td>
        {row.reference?.value ? <b>{row.reference.value}</b> : <span className="meta">无参考值</span>}
        {row.reference?.source_class ? <span className="meta"> · {SOURCE_CLASS_LABEL[row.reference.source_class] ?? row.reference.source_class}</span> : null}
      </td>
      <td>
        <ul className="ev-cells">
          {row.cells.map((cell, idx) => (
            <li key={`${cell.fact_id}-${idx}`}>
              <b>{cell.display_value}</b>
              <span className="meta">{[SOURCE_CLASS_LABEL[cell.source_class] ?? cell.source_class, cell.host, cell.as_of ?? cell.published_at].filter(Boolean).join(" · ")}</span>
              {cell.is_reference ? <span className="role light support">参考</span> : null}
              {cell.low_ocr_confidence ? <span className="role light review">OCR 置信度低</span> : null}
            </li>
          ))}
        </ul>
      </td>
      <td className="ev-num">{formatInteger(row.independent_source_count)}</td>
    </tr>
  );
}

export function ReconciliationTable({ view }: { view: ReconciliationView }) {
  return (
    <div className="ev-block">
      <div className="gtitle">多来源口径核对</div>
      <p className="note">同一指标、期间与口径的数字来自不同来源时并排列出；不会自动修正任何数值，转载只算一个独立来源。</p>
      <div className="md-table-scroll">
        <table className="ev-table">
          <thead>
            <tr><th>指标</th><th>参考值</th><th>各来源数值</th><th>独立来源</th></tr>
          </thead>
          <tbody>{view.rows.map((row) => <ReconRow key={row.row_id} row={row} />)}</tbody>
        </table>
      </div>
    </div>
  );
}

function docPagesNote(doc: CoverageDocument): string[] {
  const parts: string[] = [];
  const pages = doc.pages;
  if (!pages) return parts;
  const needsOcr = pages.by_status["needs_ocr"] ?? 0;
  if (needsOcr > 0) parts.push(`${formatInteger(needsOcr)} 页待 OCR`);
  if (pages.unread_text_pages > 0) parts.push(`${formatInteger(pages.unread_text_pages)} 页文字未读`);
  if (pages.ocr_low_confidence_pages.length > 0) parts.push(`OCR 低置信度页：${pages.ocr_low_confidence_pages.slice(0, 8).join("、")}`);
  return parts;
}

export function CoverageMapBlock({ coverage }: { coverage: CoverageMapView }) {
  const docs = coverage.documents.filter((doc) => docPagesNote(doc).length > 0);
  return (
    <div className="ev-block">
      <div className="gtitle">取证覆盖与缺口</div>
      <ul className="ev-cov">
        {coverage.rows.map((row) => {
          const tone = COVERAGE_STATUS_TONE[row.status] ?? "neutral";
          return (
            <li key={row.category}>
              <div className="ev-cov-head">
                <span className="ev-title">{row.label.zh || row.category}</span>
                <span className={`role light ${tone}`}>{row.status_label.zh || row.status}</span>
              </div>
              {row.not_established.map((statement, idx) => <span key={idx} className="why ev-not">{statement}</span>)}
            </li>
          );
        })}
      </ul>
      {docs.length > 0 ? (
        <ul className="ev-docs" aria-label="未读与待识别页面">
          {docs.map((doc) => (
            <li key={doc.source_id}>
              <span className="ev-title">{doc.title || doc.source_id}</span>
              <span className="meta">{docPagesNote(doc).join(" · ")}</span>
            </li>
          ))}
        </ul>
      ) : null}
      <p className="note">此图由运行记录确定性生成，不是模型自述。</p>
    </div>
  );
}

export function AttributionLine({ items }: { items: readonly TurnAttributionView[] }) {
  if (items.length === 0) return null;
  return (
    <p className="note ev-attribution">
      {ATTRIBUTION_NOTE_LABEL}：
      {items.map((item, idx) => (
        <span key={item.id}>
          {idx > 0 ? "；" : ""}
          {item.url ? <a className="link" href={item.url} target="_blank" rel="noopener noreferrer">{item.text}</a> : item.text}
        </span>
      ))}
    </p>
  );
}

/** 数据截至、口径核对表、覆盖与缺口地图和来源署名，来自一轮研究保存的确定性视图。 */
export function EvidenceViews({ turn }: { turn: ResearchV2.ResearchTurn }) {
  const reconciliation = useMemo(() => parseReconciliation(turn.reconciliation), [turn.reconciliation]);
  const coverage = useMemo(() => parseCoverageMap(turn.coverage_map), [turn.coverage_map]);
  const attributions = useMemo(() => parseAttributions(turn.attributions), [turn.attributions]);
  const differs = reconciliation?.rows.some((row) => row.status === "differs") ?? false;
  if (!reconciliation && !coverage && attributions.length === 0) return null;
  return (
    <div className="ev-views">
      {coverage ? <DataAsOfList coverage={coverage} /> : null}
      {reconciliation || coverage ? (
        <Disclosure summary="口径核对与取证覆盖" defaultOpen={differs}>
          {reconciliation ? <ReconciliationTable view={reconciliation} /> : null}
          {coverage ? <CoverageMapBlock coverage={coverage} /> : null}
        </Disclosure>
      ) : null}
      <AttributionLine items={attributions} />
    </div>
  );
}
