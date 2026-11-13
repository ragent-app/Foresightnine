import { useMemo, useState } from "react";
import type { ResearchFact, SourceContextResult, SourceTableStructure } from "../general-research-client";
import { Button, Select } from "./Controls";
import { SourceContext } from "./SourceContext";

interface Props {
  facts: ResearchFact[];
  en: boolean;
  load: (sourceId: string, start: number, end: number) => Promise<SourceContextResult>;
  loadTable: (sourceId: string, start: number, end: number) => Promise<SourceTableStructure>;
}

type Entry = { fact: ResearchFact; entity: string; metric: string; row: string };
const ENTITY_PAGE = 4;
const ROW_PAGE = 10;

export function StructuredComparison({ facts, en, load, loadTable }: Props) {
  const [selectedMetric, setSelectedMetric] = useState("");
  const [entityPage, setEntityPage] = useState(0);
  const [rowPage, setRowPage] = useState(0);
  const entries = useMemo(() => facts.flatMap((fact): Entry[] => {
    const meta = fact.structured_metadata;
    if (!fact.extraction_method || !meta || typeof meta.entity_id !== "string" ||
        typeof meta.metric_id !== "string" || !fact.source_id) return [];
    // Keep periods, units and currencies distinct; no implied conversion.
    const row = JSON.stringify([fact.period, fact.unit, fact.currency ?? null]);
    return [{ fact, entity: meta.entity_id, metric: meta.metric_id, row }];
  }), [facts]);
  const metrics = useMemo(() => Array.from(new Set(entries.map((entry) => entry.metric))).sort(), [entries]);
  const metric = metrics.includes(selectedMetric) ? selectedMetric : metrics[0] ?? "";
  const filtered = useMemo(() => entries.filter((entry) => entry.metric === metric), [entries, metric]);
  const entities = useMemo(() => Array.from(new Set(filtered.map((entry) => entry.entity))).sort(), [filtered]);
  const rows = useMemo(() => Array.from(new Set(filtered.map((entry) => entry.row))).sort().reverse(), [filtered]);
  const entityPages = Math.max(1, Math.ceil(entities.length / ENTITY_PAGE));
  const rowPages = Math.max(1, Math.ceil(rows.length / ROW_PAGE));
  const currentEntityPage = Math.min(entityPage, entityPages - 1);
  const currentRowPage = Math.min(rowPage, rowPages - 1);
  const shownEntities = entities.slice(currentEntityPage * ENTITY_PAGE, (currentEntityPage + 1) * ENTITY_PAGE);
  const shownRows = rows.slice(currentRowPage * ROW_PAGE, (currentRowPage + 1) * ROW_PAGE);
  const cells = useMemo(() => {
    const result = new Map<string, Entry[]>();
    for (const item of filtered) {
      const key = JSON.stringify([item.row, item.entity]);
      const current = result.get(key) ?? [];
      current.push(item);
      result.set(key, current);
    }
    return result;
  }, [filtered]);
  if (entries.length < 2 || !metric) return null;
  return <section className="general-source">
    <h3>{en ? "Structured observation comparison" : "结构化数值比较"}</h3>
    <p>{en ? "Rows group the exact recorded metric, period, unit and currency. Similar labels do not establish comparability; filing revisions remain separate. Values and source meaning are not independently verified." : "按记录的相同指标、期间、单位和币种分组；名称相同不代表口径可比，不同申报或修订保留为独立记录。数值及来源含义尚未独立核验。"}</p>
    <label>{en ? "Recorded metric" : "记录的指标"}<Select value={metric} onChange={(event) => { setSelectedMetric(event.target.value); setEntityPage(0); setRowPage(0); }}>
      {metrics.map((value) => <option key={value} value={value}>{value}</option>)}
    </Select></label>
    <div className="source-table-scroll"><table>
      <thead><tr><th scope="col">{en ? "Period · unit · currency" : "期间 · 单位 · 币种"}</th>{shownEntities.map((entity) => <th scope="col" key={entity}>{entity}</th>)}</tr></thead>
      <tbody>{shownRows.map((row) => {
        const [period, unit, currency] = JSON.parse(row) as [string, string, string | null];
        return <tr key={row}><th scope="row">{period} · {unit} · {currency ?? (en ? "unspecified" : "未指定")}</th>{shownEntities.map((entity) => {
          const values = cells.get(JSON.stringify([row, entity])) ?? [];
          return <td key={entity}>{values.length ? values.map(({ fact }) => <div key={fact.fact_id}>
            <strong>{fact.value}</strong>{fact.structured_metadata?.filing_date ? <small> · {en ? "Filed" : "申报"}: {String(fact.structured_metadata.filing_date)}</small> : null}
            {fact.structured_metadata?.accn ? <small> · {String(fact.structured_metadata.accn)}</small> : null}
            {fact.source_id ? <SourceContext sourceId={fact.source_id} location={fact.location} quotation={fact.quotation} en={en} load={load} loadTable={loadTable} /> : null}
          </div>) : <span>{en ? "Not recorded" : "未记录"}</span>}</td>;
        })}</tr>;
      })}</tbody>
    </table></div>
    {entityPages > 1 ? <div className="general-actions"><Button disabled={currentEntityPage === 0} onClick={() => setEntityPage(currentEntityPage - 1)}>{en ? "Previous entities" : "上一组对象"}</Button><span>{currentEntityPage + 1} / {entityPages}</span><Button disabled={currentEntityPage + 1 >= entityPages} onClick={() => setEntityPage(currentEntityPage + 1)}>{en ? "Next entities" : "下一组对象"}</Button></div> : null}
    {rowPages > 1 ? <div className="general-actions"><Button disabled={currentRowPage === 0} onClick={() => setRowPage(currentRowPage - 1)}>{en ? "Previous periods" : "上一组期间"}</Button><span>{currentRowPage + 1} / {rowPages}</span><Button disabled={currentRowPage + 1 >= rowPages} onClick={() => setRowPage(currentRowPage + 1)}>{en ? "Next periods" : "下一组期间"}</Button></div> : null}
  </section>;
}
