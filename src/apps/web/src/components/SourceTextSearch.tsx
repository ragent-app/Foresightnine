import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Field, Input } from "./Controls";
import { searchSourceText } from "../source-text-search";

interface Props {
  sourceId: string;
  en: boolean;
  disabled: boolean;
  load: (sourceId: string) => Promise<{ source_id: string; text?: string }>;
  onSelect: (quotation: string) => void;
}

/** Searches only the display-authorized text fetched for this source. */
export function SourceTextSearch({ sourceId, en, disabled, load, onSelect }: Props) {
  const [text, setText] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const results = useMemo(() => searchSourceText(text ?? "", query), [text, query]);
  async function loadText() {
    setLoading(true); setFailed(false);
    try {
      const source = await load(sourceId);
      if (!alive.current) return;
      if (source.source_id !== sourceId || typeof source.text !== "string") throw new Error("Source text unavailable");
      setText(source.text);
    } catch { if (alive.current) setFailed(true); }
    finally { if (alive.current) setLoading(false); }
  }
  return <details className="general-source">
    <summary>{en ? "Find text in this source" : "在此资料中查找原文"}</summary>
    <p>{en ? "Search runs in this browser after loading the permitted source text. Review and edit the passage before adding it." : "加载允许展示的原文后，在浏览器中搜索。添加前请检查并编辑所选片段。"}</p>
    {text === null ? <Button type="button" disabled={disabled || loading} onClick={() => void loadText()}>{loading ? (en ? "Loading…" : "正在加载…") : (en ? "Load source text" : "加载原文")}</Button> : <>
      <Field label={en ? "Find exact text" : "查找原文"} helper={en ? "At least two characters; case-insensitive." : "至少输入两个字符；不区分大小写。"}>
        <Input value={query} maxLength={120} disabled={disabled} onChange={(event) => setQuery(event.target.value)} />
      </Field>
      {query.trim().length >= 2 && !results.items.length ? <p role="status">{en ? "No matches found." : "未找到匹配原文。"}</p> : null}
      {results.items.map((item, index) => <div className="source-context" key={`${item.start}:${item.end}`}>
        <pre>{text.slice(item.left, item.start)}<mark>{text.slice(item.start, item.end)}</mark>{text.slice(item.end, item.right)}</pre>
        <Button type="button" disabled={disabled} onClick={() => onSelect(text.slice(item.left, item.right))}>{en ? `Use passage ${index + 1}` : `选用片段 ${index + 1}`}</Button>
      </div>)}
      {results.truncated ? <p>{en ? "Only the first 20 matches are shown; narrow the search." : "仅显示前 20 条，请缩小搜索范围。"}</p> : null}
    </>}
    {failed ? <p role="status">{en ? "Source text is unavailable or display permission has changed." : "原文不可用或展示权限已变化。"}</p> : null}
  </details>;
}
