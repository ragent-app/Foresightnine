import { useEffect, useRef, useState } from "react";
import type { SourceContextResult, SourceLocation, SourceTableStructure } from "../general-research-client";
import { Button } from "./Controls";

interface Props {
  sourceId: string;
  location?: SourceLocation | undefined;
  quotation?: string | undefined;
  en: boolean;
  candidate?: number | undefined;
  onUseContext?: ((quotation: string) => void) | undefined;
  onAddSelected?: ((quotation: string) => void) | undefined;
  load: (sourceId: string, start: number, end: number) => Promise<SourceContextResult>;
  loadTable?: ((sourceId: string, start: number, end: number) => Promise<SourceTableStructure>) | undefined;
}

export function ExtractedTableContext({ table, en }: { table: NonNullable<SourceContextResult["table_context"]>; en: boolean }) {
  return <details>
    <summary>{en ? "Show preceding extracted table rows" : "查看此前提取的表格行"}</summary>
    <p>{en ? "These are adjacent extracted lines; column alignment, headings and meaning are not verified. Check the original document." : "以下是相邻的提取文本；列对齐、表头和含义尚未核验，请对照原始文件。"}</p>
    {table.truncated_before ? <p>{en ? "Earlier rows were omitted." : "此前还有未显示的行。"}</p> : null}
    <pre>{table.before}<mark>{table.selected}</mark>{table.after}</pre>
  </details>;
}

export function SourceContext({ sourceId, location, quotation, en, load, loadTable, candidate, onUseContext, onAddSelected }: Props) {
  const [open, setOpen] = useState(false);
  const [context, setContext] = useState<SourceContextResult | null>(null);
  const [failed, setFailed] = useState(false);
  const [table, setTable] = useState<SourceTableStructure | null>(null);
  const [tableLoading, setTableLoading] = useState(false);
  const tableRequest = useRef(0);
  const start = location?.character_start;
  const end = location?.character_end;
  const valid = typeof start === "number" && typeof end === "number" &&
    Number.isSafeInteger(start) && Number.isSafeInteger(end) && start >= 0 && end > start && end - start <= 1000;

  useEffect(() => {
    if (!open || !valid || start === undefined || end === undefined) return;
    let active = true;
    setContext(null); setFailed(false);
    void load(sourceId, start, end).then((value) => { if (active) setContext(value); })
      .catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [open, valid, start, end, sourceId, load]);

  useEffect(() => {
    setTable(null); setTableLoading(false);
    return () => { tableRequest.current += 1; };
  }, [sourceId, start, end]);

  async function inspectTable() {
    if (!loadTable || start === undefined || end === undefined) return;
    const request = ++tableRequest.current;
    setTableLoading(true); setTable(null);
    try {
      const result = await loadTable(sourceId, start, end);
      if (request === tableRequest.current) setTable(result);
    } catch {
      if (request === tableRequest.current) setTable({ status: "unavailable" });
    } finally {
      if (request === tableRequest.current) setTableLoading(false);
    }
  }

  if (location?.status === "ambiguous" && Array.isArray(location.matches)) return <div className="source-context">
    <p>{en ? "This quotation has multiple matching locations. Compare the candidates." : "这段引文有多个匹配位置，请对比候选位置。"}</p>
    {typeof location.same_page_candidate_index === "number" && Number.isInteger(location.same_page_candidate_index) && location.same_page_candidate_index >= 0 && location.same_page_candidate_index < location.matches.length ?
      <p>{en ? `Location ${location.same_page_candidate_index + 1} is near another citation on the same page. This does not establish the correct location or support.` : `候选位置 ${location.same_page_candidate_index + 1} 与另一条引文位于同页附近；这不能证明位置正确或支持结论。`}</p> : null}
    {location.matches.slice(0, 5).map((match, index) => match && typeof match === "object" ? <SourceContext key={`${match.character_start}:${match.character_end}:${index}`}
      sourceId={sourceId} location={{ ...(typeof match.character_start === "number" ? { character_start: match.character_start } : {}),
        ...(typeof match.character_end === "number" ? { character_end: match.character_end } : {}),
        ...(typeof match.page === "number" ? { page: match.page } : {}) }} quotation={quotation} en={en} load={load} loadTable={loadTable} candidate={index + 1} onUseContext={onUseContext} /> : null)}
    {location.truncated ? <p>{en ? "Additional matches were omitted." : "还有未显示的匹配位置。"}</p> : null}
  </div>;
  if (!valid) return null;
  return <div className="source-context">
    <Button aria-expanded={open} onClick={() => setOpen((value) => !value)}>{candidate ? (open ? (en ? `Close location ${candidate}` : `收起候选位置 ${candidate}`) : (en ? `View location ${candidate}` : `查看候选位置 ${candidate}`)) : onAddSelected ? (open ? (en ? "Close suggested passage" : "收起候选原文") : (en ? "View suggested passage" : "查看候选原文")) : open ? (en ? "Close source context" : "收起原文上下文") : (en ? "View source context" : "查看原文上下文")}</Button>
    {open && failed ? <p role="status">{en ? "Source context is unavailable or permission has changed." : "无法查看原文上下文，资料可能不可用或权限已变化。"}</p> : null}
    {open && !failed && !context ? <p role="status">{en ? "Loading source context…" : "正在读取原文上下文…"}</p> : null}
    {open && context ? <div>
      <p>{context.title}{context.page_start !== null ? ` · ${en ? "page" : "第"} ${context.page_start}${context.page_end !== context.page_start ? `–${context.page_end}` : ""}${en ? "" : " 页"}` : ""}</p>
      {quotation === undefined ? <p role="status">{onAddSelected ? (en ? "This is a suggested source passage. Check its relevance before adding it." : "这是候选原文，请核对相关性后再添加。") : (en ? "No saved quotation is available for comparison." : "没有可供比对的已保存引文。")}</p> : quotation !== context.selected ? <p role="status">{en ? "The saved quotation does not match the selected source text." : "保存的引文与所选原文位置不一致。"}</p> : <p>{en ? "The quotation matches this text; its support for the finding is not independently verified." : "引文与此处原文一致；是否支持结论尚未独立核验。"}</p>}
      <pre>{context.before}<mark>{context.selected}</mark>{context.after}</pre>
      {context.table_context ? <ExtractedTableContext table={context.table_context} en={en} /> : null}
      {context.table_context && loadTable ? <div>
        <Button disabled={tableLoading} onClick={() => void inspectTable()}>{en ? "Inspect original HTML table structure" : "检查原始 HTML 表格结构"}</Button>
        {tableLoading ? <p role="status">{en ? "Inspecting original HTML…" : "正在检查原始 HTML…"}</p> : null}
        {table && table.status !== "available" ? <p role="status">{en ? "The original structure could not be established; try again or inspect the original file." : "无法确认原始表格结构；可重试或查看原始文件。"}</p> : null}
        {table?.status === "available" && table.rows ? <div>
          <p>{en ? "Cells and spans come from the saved HTML. Formatting, units, periods and meaning still require checking against the original." : "单元格和跨列来自保存的 HTML；格式、单位、期间与含义仍需对照原文核验。"}</p>
          {table.truncated_before ? <p>{en ? "Earlier rows were omitted." : "此前还有未显示的行。"}</p> : null}
          <div className="source-table-scroll"><table><tbody>{table.rows.map((row, rowIndex) => <tr key={rowIndex}>{row.cells.map((cell, cellIndex) => <td key={cellIndex} colSpan={cell.end - cell.start}>{cell.text}</td>)}</tr>)}</tbody></table></div>
        </div> : null}
      </div> : null}
      {onAddSelected && context.selected.trim() && Array.from(context.selected).length <= 1000 ? <Button onClick={() => onAddSelected(context.selected)}>{en ? "Add selected passage as quotation" : "添加所选原文作为引文"}</Button> : null}
      {candidate && onUseContext && quotation === context.selected && Array.from(context.before + context.selected + context.after).length <= 1000 ? <>
        <p>{en ? "You can use the displayed context as a quotation. Check that it supports the finding; saving will check its location again." : "可将显示的上下文作为引文。请核对其是否支持结论；保存时会重新检查位置。"}</p>
        <Button onClick={() => onUseContext(context.before + context.selected + context.after)}>{en ? "Use this context as quotation" : "使用此上下文作为引文"}</Button>
      </> : null}
    </div> : null}
  </div>;
}
