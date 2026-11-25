import { useMemo } from "react";
import type { ResearchV2 } from "@foresightnine/contracts";
import type { CoverageDocument, CoverageMapView, CoverageRow, ReconciliationRow, ReconciliationView, TurnAttributionView } from "../../api/types";
import { ATTRIBUTION_NOTE_LABEL, COVERAGE_STATUS_TONE, RECONCILIATION_BADGE_LABEL, SOURCE_CLASS_LABEL } from "../../lib/copy";
import { formatInteger } from "../../lib/format";
import { parseAttributions, parseCoverageMap, parseReconciliation } from "../../lib/research";
import { Disclosure } from "../../ui/controls";

const BADGE_TONE: Readonly<Record<string, string>> = {
  single_source: "neutral",
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
        <span className={`role light ${tone}`}>{row.badge === "single_source" ? "仅单一来源，未交叉核实" : RECONCILIATION_BADGE_LABEL[row.badge] ?? row.badge}</span>
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
              {cell.quotation_excerpt ? <blockquote>{cell.quotation_excerpt}</blockquote> : null}
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
      <p className="note">仅比较已记录且可匹配的数值；单一来源未交叉核实。数值差异可能来自口径或模型解释，需查看来源原文；不会自动修正任何数值。</p>
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
  const needsOcr = (pages.by_status["needs_ocr"] ?? 0) + (pages.by_status["text_partial"] ?? 0);
  if (needsOcr > 0) parts.push(`${formatInteger(needsOcr)} 页待 OCR`);
  if (pages.unread_text_pages > 0) parts.push(`${formatInteger(pages.unread_text_pages)} 页文字未读`);
  if (pages.ocr_low_confidence_pages.length > 0) parts.push(`OCR 低置信度页：${pages.ocr_low_confidence_pages.slice(0, 8).join("、")}`);
  return parts;
}

const SOURCE_ORIGINS: Record<string, string> = { regulatory_filing: "监管披露", verified_issuer: "已确认发行人", issuer_candidate_unverified: "可能为发行人网站（未确认）", website_unverified: "网站来源（身份未确认）", data_provider: "数据服务", user_upload: "用户上传", unknown: "来源身份未知" };
const INGESTION: Record<string, string> = { user_supplied: "用户提供", official: "官方来源", market_data: "行情数据" };
const METHODS: Record<string, string> = {
  web_html: "网页",
  web_pdf: "网页 PDF",
  user_pdf: "上传 PDF",
  user_html: "上传网页",
  user_pptx: "上传 PPTX",
  user_docx: "上传 DOCX",
  user_text: "上传文本/字幕",
  user_supplied: "用户输入",
  official_html: "官方网页",
  sec_filing_html: "监管文件",
  sec_earnings_release: "SEC 业绩新闻稿",
  sec_filing_table: "SEC 财报表格",
  sec_filing_sections: "SEC 章节拆解",
  sec_material_events: "SEC 重大事件",
  market_data: "行情接口",
  historical_prices: "历史价格",
  sec_companyfacts: "监管结构化财务数据",
  sec_companyconcept: "监管结构化财务数据",
  structured_financials: "结构化财务数据",
  fmp_consensus: "一致预期（BYOK）",
  fmp_transcript: "电话会纪要（BYOK）",
  peer_relative_valuation: "同业相对估值",
  governance_capital_audit: "治理与资本审计",
  call_qa_verification: "电话会 Q&A 核验",
};
const PERMISSIONS: Record<string, string> = { store: "保存", model_input: "模型使用", display: "展示", export: "导出" };

export function CoverageMapBlock({ coverage }: { coverage: CoverageMapView }) {
  const docs = coverage.documents;
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
        <ul className="ev-docs" aria-label="来源身份与覆盖">
          {docs.map((doc) => (
            <li key={doc.source_id}>
              <span className="ev-title">{doc.title || doc.source_id}</span>
              <span className="meta">{[doc.origin ? SOURCE_ORIGINS[doc.origin] ?? "来源身份未知" : "", doc.ingestion_kind ? `入口：${INGESTION[doc.ingestion_kind] ?? "未知"}` : "", doc.retrieval_method ? `获取方式：${METHODS[doc.retrieval_method] ?? "未知"}` : "", doc.permissions ? `权限：${Object.entries(doc.permissions).filter(([, allowed]) => allowed).map(([key]) => PERMISSIONS[key] ?? key).join("、") || "无"}` : "", ...docPagesNote(doc)].filter(Boolean).join(" · ")}</span>
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

/** One sentence on what was searched; the individual searches are one tap away. */
function CounterEvidenceView({ value }: { value: unknown }) {
  if (!value || typeof value !== "object") return null;
  const data = value as Record<string, unknown>;
  const attempts = Array.isArray(data.attempts) ? data.attempts.filter((a): a is Record<string, unknown> => !!a && typeof a === "object") : [];
  const findings = Array.isArray(data.linked_findings) ? data.linked_findings.filter((a): a is Record<string, unknown> => !!a && typeof a === "object") : [];
  if (!attempts.length && !findings.length) return null;
  const external = attempts.filter((a) => a.scope === "external").length;
  const internal = attempts.filter((a) => a.scope === "document").length;
  const searched = [external ? `外部搜索 ${external} 次` : "", internal ? `文内搜索 ${internal} 次` : ""].filter(Boolean).join("、");
  return <div className="ev-block"><div className="gtitle">反证检查</div>
    <p>{searched ? `${searched}，` : ""}{findings.length ? `找到 ${findings.length} 条可能的反证。` : "没有找到反证。没找到不代表不存在。"}</p>
    {findings.length ? <ul>{findings.map((f, index) => <li key={index}><blockquote>{String(f.quotation ?? "")}</blockquote><span className="note">来源独立性未核实</span></li>)}</ul> : null}
    {attempts.length ? <Disclosure summary="查看搜索了什么">
      <ul>{attempts.map((a, index) => <li key={index}>{String(a.hypothesis ?? a.target ?? "")}</li>)}</ul>
    </Disclosure> : null}
  </div>;
}

/** Whether a turn has any saved check views to fold under the answer. */
export function hasEvidenceViews(turn: ResearchV2.ResearchTurn): boolean {
  return Boolean(parseReconciliation(turn.reconciliation) || parseCoverageMap(turn.coverage_map) || turn.counter_evidence);
}

/** One line worth reading next to the answer: stale data or a material mismatch between sources. */
export function evidenceAlert(turn: ResearchV2.ResearchTurn): string | null {
  const coverage = parseCoverageMap(turn.coverage_map);
  const reconciliation = parseReconciliation(turn.reconciliation);
  const stale = coverage?.rows.some((row) => row.data_as_of && stalenessLabel(row)?.stale) ?? false;
  const differs = reconciliation?.rows.some((row) => row.badge === "differs_material") ?? false;
  if (stale && differs) return "部分数据已过期，且不同来源的数字有明显差异。";
  if (stale) return "部分数据已过期。";
  if (differs) return "不同来源的数字有明显差异。";
  return null;
}

/** 数据截至、口径核对表、覆盖与缺口地图和来源署名，来自一轮研究保存的确定性视图。 */
export function EvidenceViews({ turn, attribution = true }: { turn: ResearchV2.ResearchTurn; attribution?: boolean }) {
  const reconciliation = useMemo(() => parseReconciliation(turn.reconciliation), [turn.reconciliation]);
  const coverage = useMemo(() => parseCoverageMap(turn.coverage_map), [turn.coverage_map]);
  const attributions = useMemo(() => parseAttributions(turn.attributions), [turn.attributions]);
  const differs = reconciliation?.rows.some((row) => row.status === "differs") ?? false;
  if (!reconciliation && !coverage && !turn.counter_evidence && attributions.length === 0) return null;
  return (
    <div className="ev-views">
      {coverage ? <DataAsOfList coverage={coverage} /> : null}
      <CounterEvidenceView value={turn.counter_evidence} />
      {reconciliation || coverage ? (
        <Disclosure summary="口径核对与取证覆盖" defaultOpen={differs}>
          {reconciliation ? <ReconciliationTable view={reconciliation} /> : null}
          {coverage ? <CoverageMapBlock coverage={coverage} /> : null}
        </Disclosure>
      ) : null}
      {attribution ? <AttributionLine items={attributions} /> : null}
    </div>
  );
}
