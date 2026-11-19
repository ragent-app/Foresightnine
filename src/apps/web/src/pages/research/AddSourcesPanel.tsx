import type { ResearchV2 } from "@foresightnine/contracts";
import { useId, useRef, useState, type DragEvent, type FormEvent } from "react";
import { ApiError } from "../../api/http";
import { createTextSource, fetchOfficialSource, fetchSecConceptSource, searchSecIdentities, uploadHtmlSource, uploadPdfSource, useResearchCapabilities, useSecAvailable, useWebFetchAvailable } from "../../api/sources";
import type { CreatedSource, SecIdentity, SourcePermissionFlags } from "../../api/types";
import { PERMISSION_LABEL } from "../../lib/copy";
import {
  DEFAULT_PERMISSIONS, FETCH_HELP, fetchUrlProblem, fileToBase64, formatBytes, ineligibility, INELIGIBLE_TEXT, MAX_TEXT_CHARACTERS,
  MAX_UPLOAD_BYTES, SEC_UNAVAILABLE, titleFromFilename, uploadKind, type UploadKind,
} from "../../lib/sources";
import { Disclosure, Segmented } from "../../ui/controls";
import { Icon } from "../../ui/icons";
import { Panel } from "../../ui/Panel";
import { describeError } from "./errors";
import { localInputToIso } from "./options";

export type AddSourceTab = "file" | "text" | "url" | "sec";

export interface AddSourceInitial {
  tab?: AddSourceTab;
  url?: string;
  title?: string;
  file?: File;
}

const TABS: { value: AddSourceTab; label: string }[] = [
  { value: "file", label: "文件" },
  { value: "text", label: "文本" },
  { value: "url", label: "网址" },
  { value: "sec", label: "SEC 数据" },
];

const PERMISSION_HELP: Record<keyof SourcePermissionFlags, string> = {
  store: "保存后才能用于研究",
  model_input: "研究 Agent 可以读取全文",
  display: "在阅读器中展示原文，用来核对引文",
  export: "随研究记录一起导出原文",
};

const TAXONOMIES = [
  { value: "us-gaap", label: "US GAAP" },
  { value: "ifrs-full", label: "IFRS" },
  { value: "dei", label: "DEI" },
] as const;

// Common XBRL tags offered as suggestions only; any valid tag can be typed.
const COMMON_TAGS = [
  "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "OperatingIncomeLoss", "NetIncomeLoss",
  "EarningsPerShareDiluted", "Assets", "Liabilities", "StockholdersEquity", "CashAndCashEquivalentsAtCarryingValue",
  "NetCashProvidedByUsedInOperatingActivities", "PaymentsToAcquirePropertyPlantAndEquipment",
];
const TAG_PATTERN = /^[A-Za-z][A-Za-z0-9_]*$/;
const UNIT_PATTERN = /^[A-Za-z0-9-]+$/;

/** Endpoint-specific wording for the failures the backend reports with a fixed message. */
function addError(tab: AddSourceTab, kind: UploadKind | null, error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 503 && /contact is not configured/i.test(error.message)) return "SEC 数据还没有配置联系信息，暂时无法获取。";
    if (error.status === 503 && tab === "file") return kind === "html" ? "网页解析繁忙，请稍后重试。" : "PDF 解析暂时不可用，请稍后重试。";
    if (error.status === 422 && error.details.length === 0) {
      if (tab === "file") return kind === "html" ? "网页文件无效、超过 5 MB、不是 UTF-8 编码，或没有可提取的文字。" : "PDF 无效、超过 5 MB、已加密，或没有可提取的文字。";
      if (tab === "url") {
        if (/blocked by fetch policy/i.test(error.message)) return "这个网站或跳转目标不在允许获取的范围内，可以保存后按文件添加。";
        if (/rejected the request/i.test(error.message)) return "目标网站拒绝了自动访问（可能需要登录或触发了防爬验证），可以保存后按文件添加。";
        if (/not found/i.test(error.message)) return "找不到该网址对应的页面（404/410）。";
        if (/exceeded the 5 mb/i.test(error.message)) return "网页或提取文本超过 5 MB / 200 万字符上限，可以分段保存后添加。";
        return "这个网址的内容无法按支持的格式获取。";
      }
      if (tab === "sec") return "没有取到这个数据项。请检查公司、标签和单位。";
    }
  }
  return describeError(error);
}

interface Props {
  /** Topic cutoff, if any; used only to explain whether the next turn reads the source. */
  asOfIso: string | null;
  /** Hands the created source to the page (attach to a topic, or stage it for a new one). */
  onAdded: (source: CreatedSource) => Promise<void>;
  onClose: () => void;
  initial?: AddSourceInitial | undefined;
  /** A run is active: adding is unavailable (spec §7). */
  locked?: boolean;
}

/** Add-sources panel (M3): file, pasted text, official URL or SEC company data. */
export function AddSourcesPanel({ asOfIso, onAdded, onClose, initial, locked = false }: Props) {
  const ids = useId();
  const secAvailable = useSecAvailable();
  const webFetchAvailable = useWebFetchAvailable();
  const allowedHosts = useResearchCapabilities().data?.sources.find((item) => item.name === "official_html_pdf")?.hosts;
  const fileInput = useRef<HTMLInputElement>(null);
  const [tab, setTab] = useState<AddSourceTab>(initial?.tab ?? (initial?.url ? "url" : "file"));
  const [file, setFile] = useState<File | null>(initial?.file ?? null);
  const [fileProblem, setFileProblem] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [title, setTitle] = useState(initial?.title ?? (initial?.file ? titleFromFilename(initial.file.name) : ""));
  const [textOrder, setTextOrder] = useState<"layout" | "reading">("layout");
  const [text, setText] = useState("");
  const [textUrl, setTextUrl] = useState("");
  const [url, setUrl] = useState(initial?.url ?? "");
  const [urlTouched, setUrlTouched] = useState(Boolean(initial?.url));
  const [publishedAt, setPublishedAt] = useState("");
  const [eventAt, setEventAt] = useState("");
  const [revisedAt, setRevisedAt] = useState("");
  const [simulated, setSimulated] = useState(false);
  const [permissions, setPermissions] = useState<SourcePermissionFlags>(DEFAULT_PERMISSIONS);
  const [secQuery, setSecQuery] = useState("");
  const [secSearching, setSecSearching] = useState(false);
  const [secResult, setSecResult] = useState<{ candidates: SecIdentity[]; limitation: string } | null>(null);
  const [secError, setSecError] = useState<string | null>(null);
  const [company, setCompany] = useState<SecIdentity | null>(null);
  const [tag, setTag] = useState("");
  const [taxonomy, setTaxonomy] = useState<(typeof TAXONOMIES)[number]["value"]>("us-gaap");
  const [unit, setUnit] = useState("");
  const [filingBefore, setFilingBefore] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Once the source exists, a retry only re-runs the hand-off, so no duplicate source is created.
  const [created, setCreated] = useState<CreatedSource | null>(null);

  const kind = file ? uploadKind(file) : null;
  const sec = tab === "sec";
  const datesAllowed = !sec;
  const urlProblem = tab === "url" ? fetchUrlProblem(url, secAvailable, { webFetchEnabled: webFetchAvailable, allowedHosts }) : null;
  const textUrlProblem = tab === "text" && textUrl.trim() ? (() => { try { const parsed = new URL(textUrl.trim()); return /^https?:$/.test(parsed.protocol) ? null : "只支持 http 或 https 网址"; } catch { return "网址格式不正确"; } })() : null;
  const tagProblem = tag.trim() && !TAG_PATTERN.test(tag.trim()) ? "标签只能包含字母、数字和下划线，并以字母开头" : null;
  const unitProblem = unit.trim() && !UNIT_PATTERN.test(unit.trim()) ? "单位只能包含字母、数字和连字符" : null;

  const pickFile = (next: File | undefined) => {
    setError(null);
    if (!next) return;
    const nextKind = uploadKind(next);
    if (!nextKind) { setFileProblem("只支持 PDF 或 HTML 文件。其他格式可以复制文字后在“文本”中添加。"); return; }
    if (next.size > MAX_UPLOAD_BYTES) { setFileProblem(`文件为 ${formatBytes(next.size)}，超过 5 MB 的上限。`); return; }
    setFileProblem(null);
    setFile(next);
    if (!title.trim() || (file && title === titleFromFilename(file.name))) setTitle(titleFromFilename(next.name));
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    pickFile(event.dataTransfer.files[0]);
  };

  const runSecSearch = async () => {
    const query = secQuery.trim();
    if (!query || secSearching) return;
    setSecSearching(true);
    setSecError(null);
    try {
      const result = await searchSecIdentities(query);
      setSecResult({ candidates: result.candidates, limitation: result.limitation });
      setCompany(result.candidates.length === 1 ? result.candidates[0]! : null);
    } catch (searchError) {
      setSecResult(null);
      setSecError(addError("sec", null, searchError));
    } finally {
      setSecSearching(false);
    }
  };

  const titleNeeded = tab !== "sec";
  const ready = (() => {
    if (locked || busy) return false;
    if (created) return true;
    if (titleNeeded && !title.trim()) return false;
    if (tab === "file") return file !== null && kind !== null;
    if (tab === "text") return text.trim().length > 0 && !textUrlProblem;
    if (tab === "url") return !urlProblem;
    return secAvailable && company !== null && tag.trim() !== "" && !tagProblem && !unitProblem;
  })();

  const effectiveUrl = tab === "url" ? url.trim() : tab === "text" ? textUrl.trim() : null;
  const preview = ineligibility({
    is_simulated: sec ? false : simulated,
    url: effectiveUrl,
    permissions,
    published_at: sec ? null : localInputToIso(publishedAt),
    revised_at: sec ? null : localInputToIso(revisedAt),
  }, sec ? null : asOfIso);

  const dates = () => {
    const values: { published_at?: string; event_at?: string; revised_at?: string } = {};
    const published = localInputToIso(publishedAt);
    const event = localInputToIso(eventAt);
    const revised = localInputToIso(revisedAt);
    if (published) values.published_at = published;
    if (event) values.event_at = event;
    if (revised) values.revised_at = revised;
    return values;
  };

  const create = async (): Promise<CreatedSource> => {
    const shared = { permissions, ...(simulated ? { is_simulated: true } : {}), ...dates() };
    if (tab === "file") {
      const encoded = await fileToBase64(file!);
      return kind === "pdf"
        ? uploadPdfSource({ title: title.trim(), pdf_base64: encoded, text_order: textOrder, ...shared })
        : uploadHtmlSource({ title: title.trim(), html_base64: encoded, ...shared });
    }
    if (tab === "text") {
      const body: ResearchV2.SourceInput = { title: title.trim(), text, ...shared, ...(textUrl.trim() ? { url: textUrl.trim() } : {}) };
      return createTextSource(body);
    }
    if (tab === "url") return fetchOfficialSource({ url: url.trim(), title: title.trim(), ...shared });
    return fetchSecConceptSource({
      cik: company!.cik, taxonomy, tag: tag.trim(), permissions,
      ...(unit.trim() ? { unit: unit.trim() } : {}),
      ...(filingBefore ? { filing_before: filingBefore } : {}),
    });
  };

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError(null);
    let source = created;
    try {
      if (!source) {
        source = await create();
        setCreated(source);
      }
    } catch (createError) {
      setError(addError(tab, kind, createError));
      setBusy(false);
      return;
    }
    try {
      await onAdded(source);
      onClose();
    } catch (handoffError) {
      setError(`资料已保存，但还没有加入研究：${describeError(handoffError)}`);
    } finally {
      setBusy(false);
    }
  };

  const disabled = busy || created !== null;
  const togglePermission = (key: keyof SourcePermissionFlags) => setPermissions({ ...permissions, [key]: !permissions[key] });

  return (
    <Panel
      title="添加资料"
      onClose={onClose}
      tabs={<Segmented asTabs label="资料类型" options={TABS.map((option) => ({ ...option, disabled: disabled && option.value !== tab }))} value={tab} onChange={(next) => { setTab(next); setError(null); }} />}
      footer={
        <>
          <button type="button" className="btn plain" onClick={onClose}>取消</button>
          <button type="submit" form={`${ids}-form`} className="btn primary" disabled={!ready}>
            {busy ? "正在添加…" : created ? "重试加入研究" : "添加"}
          </button>
        </>
      }
    >
      <form id={`${ids}-form`} className="add-source" onSubmit={(event) => void submit(event)} noValidate>
        {locked ? <p className="callout-lite warn" role="note"><Icon name="alert" size={16} />本轮研究进行中，结束后再添加资料。</p> : null}

        {tab === "file" ? (
          <>
            {file ? (
              <div className="file-row">
                <Icon name="file" />
                <span className="grow"><span className="title">{file.name}</span><span className="meta">{kind === "pdf" ? "PDF" : "HTML"} · {formatBytes(file.size)}</span></span>
                <button type="button" className="icon-btn close" aria-label="移除文件" disabled={disabled} onClick={() => { setFile(null); if (fileInput.current) fileInput.current.value = ""; }}>
                  <Icon name="close" size={14} />
                </button>
              </div>
            ) : (
              <div
                className={`dropzone${dragging ? " over" : ""}`}
                onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
                onDragLeave={() => setDragging(false)}
                onDrop={onDrop}
              >
                <Icon name="upload" size={22} />
                <span>拖入 PDF 或 HTML 文件</span>
                <button type="button" className="btn" onClick={() => fileInput.current?.click()}>选择文件</button>
                <span className="meta">单个文件不超过 5 MB</span>
              </div>
            )}
            <input ref={fileInput} type="file" accept=".pdf,.html,.htm,application/pdf,text/html" hidden onChange={(event) => pickFile(event.target.files?.[0])} />
            {fileProblem ? <span className="inline-error" role="alert">{fileProblem}</span> : null}
            <TitleField id={`${ids}-title`} value={title} onChange={setTitle} disabled={disabled} />
            {kind === "pdf" ? (
              <div className="field">
                <span className="lt">文字提取顺序</span>
                <Segmented label="文字提取顺序" options={[{ value: "layout", label: "按版面" }, { value: "reading", label: "按阅读顺序" }]} value={textOrder} onChange={setTextOrder} />
                <span className="help">表格较多的财报通常按版面提取更准确；多栏排版的文章可以选择按阅读顺序。</span>
              </div>
            ) : null}
          </>
        ) : null}

        {tab === "text" ? (
          <>
            <TitleField id={`${ids}-title`} value={title} onChange={setTitle} disabled={disabled} />
            <div className="field">
              <label htmlFor={`${ids}-text`}>正文</label>
              <textarea id={`${ids}-text`} className="tall" value={text} maxLength={MAX_TEXT_CHARACTERS} disabled={disabled} onChange={(event) => setText(event.target.value)} placeholder="粘贴报告、公告或笔记的文字" />
              <span className="help">{text.length.toLocaleString("en-US")} 字符</span>
            </div>
            <div className="field">
              <label htmlFor={`${ids}-text-url`}>来源网址（可选）</label>
              <input id={`${ids}-text-url`} type="url" inputMode="url" value={textUrl} disabled={disabled} aria-invalid={textUrlProblem ? true : undefined} onChange={(event) => setTextUrl(event.target.value)} placeholder="https://" />
              {textUrlProblem ? <span className="err">{textUrlProblem}</span> : <span className="help">只作记录，不会联网获取。</span>}
            </div>
          </>
        ) : null}

        {tab === "url" ? (
          <>
            {!webFetchAvailable ? <p className="callout-lite warn" role="note"><Icon name="alert" size={16} />当前部署限制了开放网页抓取，仅支持已允许的网站或 SEC 申报文件；其他网页可以保存后按文件添加。</p> : null}
            <div className="field">
              <label htmlFor={`${ids}-url`}>网址</label>
              <input id={`${ids}-url`} type="url" inputMode="url" value={url} disabled={disabled} aria-invalid={urlTouched && urlProblem ? true : undefined}
                onChange={(event) => setUrl(event.target.value)} onBlur={() => setUrlTouched(true)} placeholder="https://" />
              {urlTouched && urlProblem ? <span className="err">{urlProblem}</span> : null}
              <span className="help">{FETCH_HELP}</span>
            </div>
            <TitleField id={`${ids}-title`} value={title} onChange={setTitle} disabled={disabled} />
          </>
        ) : null}

        {sec ? (
          <>
            {!secAvailable ? <p className="callout-lite warn" role="note"><Icon name="alert" size={16} />{SEC_UNAVAILABLE}</p> : null}
            <div className="field">
              <label htmlFor={`${ids}-sec-q`}>公司</label>
              <div className="input-row">
                <input id={`${ids}-sec-q`} value={secQuery} disabled={disabled} placeholder="股票代码、公司名称或 CIK"
                  onChange={(event) => setSecQuery(event.target.value)}
                  onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void runSecSearch(); } }} />
                <button type="button" className="btn" onClick={() => void runSecSearch()} disabled={disabled || !secAvailable || !secQuery.trim() || secSearching}>{secSearching ? "查找中…" : "查找"}</button>
              </div>
              {secError ? <span className="err" role="alert">{secError}</span> : null}
            </div>
            {secResult ? (
              secResult.candidates.length === 0 ? <p className="note inset">没有找到匹配的公司。</p> : (
                <div className="field">
                  <span className="lt">选择公司</span>
                  <div className="list" role="radiogroup" aria-label="选择公司">
                    {secResult.candidates.map((candidate) => (
                      <label key={candidate.cik} className="item check-row">
                        <input type="radio" name={`${ids}-company`} checked={company?.cik === candidate.cik} disabled={disabled} onChange={() => setCompany(candidate)} />
                        <span className="grow"><span className="title">{candidate.name}</span><span className="meta">{candidate.ticker} · CIK {candidate.cik}</span></span>
                      </label>
                    ))}
                  </div>
                  <span className="help">来自 SEC 当前的代码对照表；交易所、证券类别和历史有效性未确认。</span>
                </div>
              )
            ) : null}
            <div className="field">
              <label htmlFor={`${ids}-tag`}>数据项（XBRL 标签）</label>
              <input id={`${ids}-tag`} list={`${ids}-tags`} value={tag} disabled={disabled} aria-invalid={tagProblem ? true : undefined} onChange={(event) => setTag(event.target.value)} placeholder="例如 Revenues" autoCapitalize="off" spellCheck={false} />
              <datalist id={`${ids}-tags`}>{COMMON_TAGS.map((item) => <option key={item} value={item} />)}</datalist>
              {tagProblem ? <span className="err">{tagProblem}</span> : <span className="help">获取这家公司历次申报中该数据项的数值。</span>}
            </div>
            <div className="grid2">
              <div className="field">
                <label htmlFor={`${ids}-tax`}>分类标准</label>
                <select id={`${ids}-tax`} value={taxonomy} disabled={disabled} onChange={(event) => setTaxonomy(event.target.value as typeof taxonomy)}>
                  {TAXONOMIES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                </select>
              </div>
              <div className="field">
                <label htmlFor={`${ids}-unit`}>单位（可选）</label>
                <input id={`${ids}-unit`} value={unit} disabled={disabled} aria-invalid={unitProblem ? true : undefined} onChange={(event) => setUnit(event.target.value)} placeholder="USD" autoCapitalize="off" spellCheck={false} />
                {unitProblem ? <span className="err">{unitProblem}</span> : null}
              </div>
            </div>
            <div className="field">
              <label htmlFor={`${ids}-before`}>只取此日期前的申报（可选）</label>
              <input id={`${ids}-before`} type="date" value={filingBefore} disabled={disabled} onChange={(event) => setFilingBefore(event.target.value)} />
            </div>
          </>
        ) : null}

        {datesAllowed ? (
          <div className="field">
            <label htmlFor={`${ids}-published`}>公开时间{asOfIso ? "" : "（可选）"}</label>
            <input id={`${ids}-published`} type="datetime-local" value={publishedAt} disabled={disabled} onChange={(event) => setPublishedAt(event.target.value)} />
            <span className="help">{asOfIso ? "这个研究设置了截止时间，需要填写公开时间才能读取这份资料。" : "用来区分资料何时公开、何时获取。"}</span>
            <Disclosure summary="事件时间和修订时间">
              <div className="grid2">
                <div className="field">
                  <label htmlFor={`${ids}-event`}>事件时间</label>
                  <input id={`${ids}-event`} type="datetime-local" value={eventAt} disabled={disabled} onChange={(event) => setEventAt(event.target.value)} />
                </div>
                <div className="field">
                  <label htmlFor={`${ids}-revised`}>修订时间</label>
                  <input id={`${ids}-revised`} type="datetime-local" value={revisedAt} disabled={disabled} onChange={(event) => setRevisedAt(event.target.value)} />
                </div>
              </div>
            </Disclosure>
          </div>
        ) : null}

        <div className="field">
          <span className="lt">使用权限</span>
          <div className="list">
            {(Object.keys(PERMISSION_HELP) as (keyof SourcePermissionFlags)[]).map((key) => (
              <label key={key} className="item check-row">
                <input type="checkbox" checked={permissions[key]} disabled={disabled || key === "store"} onChange={() => togglePermission(key)} />
                <span className="grow"><span>{PERMISSION_LABEL[key]}</span><span className="meta">{PERMISSION_HELP[key]}</span></span>
              </label>
            ))}
            {!sec ? (
              <label className="item check-row">
                <input type="checkbox" checked={simulated} disabled={disabled} onChange={() => setSimulated(!simulated)} />
                <span className="grow"><span>模拟或示例数据</span><span className="meta">只用于演示，不会作为真实证据</span></span>
              </label>
            ) : null}
          </div>
          <span className="help">权限和来源信息由你声明，系统不会核验。</span>
        </div>

        <p className={`callout-lite ${preview ? "warn" : "ok"}`} role="status">
          <Icon name={preview ? "alert" : "ok"} size={16} />
          {preview ? `添加后下一轮不会读取：${INELIGIBLE_TEXT[preview]}。` : "下一轮研究会读取这份资料。"}
        </p>
        {error ? <span className="inline-error" role="alert">{error}</span> : null}
      </form>
    </Panel>
  );
}

function TitleField({ id, value, onChange, disabled }: { id: string; value: string; onChange: (value: string) => void; disabled: boolean }) {
  return (
    <div className="field">
      <label htmlFor={id}>标题</label>
      <input id={id} value={value} maxLength={500} disabled={disabled} onChange={(event) => onChange(event.target.value)} placeholder="例如：公司 2026 财年年报" />
    </div>
  );
}
