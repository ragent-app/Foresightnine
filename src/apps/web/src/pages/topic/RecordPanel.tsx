import type { ResearchV2 } from "@foresightnine/contracts";
import { useId, useState } from "react";
import { ApiError } from "../../api/http";
import { useCreateCalculation, useRecalculate, useVersionSources } from "../../api/queries";
import { downloadSourceOriginal, useResearchCapabilities, useSecAvailable, useWebFetchAvailable } from "../../api/sources";
import type { AvailableVersionSource } from "../../api/types";
import {
  CALC_NOTE, calcChecks, formatInterimSummary, INVALID_NOTE, LEAD_NOTE, PERMISSION_LABEL, PERMISSION_ORDER, retrievalMethodLabel, SOURCE_KIND_LABEL, VERIFICATION_LABEL,
} from "../../lib/copy";
import { saveBlob } from "../../lib/download";
import { formatDateTime, formatInteger, formatQuantity } from "../../lib/format";
import { FORMULAS, pendingClaimEdits, type ClaimRoleInput, type FormulaId } from "../../lib/review";
import { ineligibility, INELIGIBLE_TEXT, isFetchableUrl } from "../../lib/sources";
import { claimRole, parseTurnEvents, readCoverage, ROLE_LABEL, stepRows, type AnswerTab, type ClaimRole } from "../../lib/research";
import { Disclosure, Segmented } from "../../ui/controls";
import { Icon } from "../../ui/icons";
import { Panel } from "../../ui/Panel";
import { describeError } from "../research/errors";
import { calculationTitle } from "./Conversation";
import { ProcessTimeline } from "./ProcessTimeline";
import { CONFLICT_TEXT } from "./ReviewPanel";
import { LIVE_TURN, useTopicView } from "./context";

const TABS: { value: AnswerTab; label: string }[] = [
  { value: "claims", label: "结论" },
  { value: "sources", label: "资料" },
  { value: "numbers", label: "数字" },
  { value: "steps", label: "过程" },
];

export function RecordPanel({ tab, turnKey, onTab, onTurn, onClose }: {
  tab: AnswerTab; turnKey: string | undefined; onTab: (tab: AnswerTab) => void; onTurn: (turnKey: string) => void; onClose: () => void;
}) {
  return (
    <Panel
      title="研究记录"
      onClose={onClose}
      detents
      tabs={<Segmented asTabs label="研究记录页签" options={TABS} value={tab} onChange={onTab} />}
    >
      {tab === "claims" ? <ClaimsTab /> : null}
      {tab === "sources" ? <SourcesTab /> : null}
      {tab === "numbers" ? <NumbersTab /> : null}
      {tab === "steps" ? <StepsTab turnKey={turnKey} onTurn={onTurn} /> : null}
    </Panel>
  );
}

const ROLE_OPTIONS: { value: ClaimRoleInput; label: string }[] = [
  { value: "support", label: "支持" },
  { value: "counterargument", label: "反方" },
];

function ClaimEditor({ claim, onDone }: { claim: ResearchV2.ResearchClaim; onDone: () => void }) {
  const view = useTopicView();
  const id = claim.claim_id!;
  const existing = view.draft.claims?.[id];
  const [text, setText] = useState(existing?.text ?? claim.text);
  const [role, setRole] = useState<ClaimRoleInput>(existing?.role ?? claim.role);
  const textId = useId();
  const empty = !text.trim();
  const commit = () => {
    if (empty) return;
    view.setDraft((draft) => ({ ...draft, claims: { ...draft.claims, [id]: { text, role } } }));
    onDone();
  };
  return (
    <div className="item editing">
      <label className="sr-only" htmlFor={textId}>结论文字</label>
      <textarea id={textId} value={text} maxLength={3000} onChange={(event) => setText(event.target.value)} autoFocus />
      <Segmented inline label="结论角色" options={ROLE_OPTIONS} value={role} onChange={setRole} />
      {(claim.citations ?? []).length > 0 ? <span className="meta">引文保持不变；修改后的结论会标注“你修改过”。</span> : null}
      <div className="actions-row">
        <button type="button" className="btn primary" onClick={commit} disabled={empty}>完成</button>
        <button type="button" className="btn plain" onClick={onDone}>取消</button>
      </div>
    </div>
  );
}

function ClaimsTab() {
  const view = useTopicView();
  const [editing, setEditing] = useState<string | null>(null);
  const claims = view.content.claims ?? [];
  const edits = view.draft.claims ?? {};
  const pending = new Set(pendingClaimEdits(view.version, view.draft));
  if (claims.length === 0) return <p className="empty-note">还没有可引用的结论。</p>;
  const groups: ClaimRole[] = ["support", "counter", "review"];
  const undo = (id: string) => view.setDraft((draft) => {
    const next = { ...draft.claims };
    delete next[id];
    return { ...draft, claims: next };
  });
  return (
    <>
      {pending.size > 0 ? (
        <p className="callout-lite warn" role="note">
          {pending.size} 条修改还没有保存。
          <button type="button" className="link" onClick={() => view.openPanel({ kind: "review" })}>去保存</button>
        </p>
      ) : null}
      {groups.map((role) => {
        // Grouping follows the draft role so an edited claim moves to its new group.
        const list = claims.filter((claim) => {
          const edit = claim.claim_id && pending.has(claim.claim_id) ? edits[claim.claim_id] : undefined;
          return claimRole(edit ? { ...claim, role: edit.role } : claim) === role;
        });
        if (list.length === 0 && role !== "review") return null;
        return (
          <div key={role}>
            <div className="gtitle"><span className={`role ${role}`}>{ROLE_LABEL[role]}</span>{list.length}</div>
            {list.length === 0 ? (
              <p className="note inset">没有与已有结论冲突的新证据。</p>
            ) : (
              <div className="list">
                {list.map((claim) => {
                  const id = claim.claim_id;
                  if (id && editing === id) return <ClaimEditor key={id} claim={claim} onDone={() => setEditing(null)} />;
                  const changed = id ? pending.has(id) : false;
                  const numbers = (claim.citations ?? []).map((citation) => view.citations.numberFor(citation)).filter((n) => n > 0);
                  const turn = view.turnNumber(claim.turn_id);
                  const meta = [turn ? `第 ${turn} 轮` : "你添加", numbers.length ? `引用 ${numbers.join("、")}` : "没有引用", claim.edited_by_user ? "你修改过" : null].filter(Boolean).join(" · ");
                  return (
                    <div key={id ?? claim.text} className="item claim-item">
                      <button type="button" className="claim-go" onClick={() => id && view.gotoClaim(id)}>
                        <span>{changed ? edits[id!]!.text.trim() : claim.text}</span>
                        <span className="meta">{meta}{changed ? <span className="unsaved"> · 未保存</span> : null}</span>
                      </button>
                      {id ? (
                        <span className="claim-actions">
                          {changed ? <button type="button" className="link" onClick={() => undo(id)}>撤销</button> : null}
                          <button type="button" className="link" onClick={() => setEditing(id)} disabled={view.locked} title={view.locked ? "本轮研究进行中" : undefined}>编辑</button>
                        </span>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </>
  );
}

export function Permissions({ source }: { source: AvailableVersionSource }) {
  return (
    <div className="perms">
      {PERMISSION_ORDER.map((key) => {
        const allowed = source.permissions[key];
        return <span key={key} className={allowed ? "" : "no"}>{allowed ? "✓" : "✕"} {PERMISSION_LABEL[key]}</span>;
      })}
    </div>
  );
}

export function sourceMeta(source: AvailableVersionSource): string {
  const parts = [SOURCE_KIND_LABEL[source.kind] ?? source.kind, retrievalMethodLabel(source.retrieval_method)];
  if (source.published_at) parts.push(`公开 ${formatDateTime(source.published_at)}`);
  parts.push(`获取 ${formatDateTime(source.obtained_at)}`);
  if (source.is_simulated) parts.push("模拟数据");
  return parts.join(" · ");
}


function CoverageBar({ ranges, total }: { ranges: readonly (readonly [number, number])[]; total: number }) {
  return (
    <div className="cov" role="img" aria-label="已读范围">
      <svg viewBox={`0 0 ${total} 1`} preserveAspectRatio="none" aria-hidden="true">
        {ranges.map(([start, end]) => <rect key={`${start}-${end}`} x={start} y={0} width={Math.max(end - start, total / 400)} height={1} />)}
      </svg>
    </div>
  );
}

export function canDownloadOriginal(source: AvailableVersionSource): boolean {
  return Boolean(source.original_pdf_available || source.original_html_available) && source.permissions.display && source.permissions.export;
}

/** Downloads the retained PDF or HTML (needs display and export permission). */
export function DownloadOriginalButton({ source }: { source: AvailableVersionSource }) {
  const [state, setState] = useState<"idle" | "busy" | "error">("idle");
  if (!canDownloadOriginal(source)) return null;
  const download = async () => {
    setState("busy");
    try {
      const { blob, filename } = await downloadSourceOriginal(source.source_id);
      saveBlob(blob, filename);
      setState("idle");
    } catch {
      setState("error");
    }
  };
  const label = source.original_pdf_available ? "下载原始 PDF" : "下载原始网页";
  return (
    <>
      <button type="button" className="btn" onClick={() => void download()} disabled={state === "busy"}>{state === "busy" ? "正在下载…" : label}</button>
      {state === "error" ? <span className="inline-error" role="alert">原件暂时无法下载。</span> : null}
    </>
  );
}

function ReextractButton({ source }: { source: AvailableVersionSource }) {
  const view = useTopicView();
  const [state, setState] = useState<{ busy: boolean; error: string | null }>({ busy: false, error: null });
  const order = source.pdf_text_order === "reading" ? "按版面" : "按阅读顺序";
  const run = async () => {
    setState({ busy: true, error: null });
    try {
      await view.reextractPdf(source);
      setState({ busy: false, error: null });
    } catch (error) {
      setState({ busy: false, error: describeError(error) });
    }
  };
  return (
    <>
      <button type="button" className="btn" onClick={() => void run()} disabled={state.busy || view.locked}>{state.busy ? "正在提取…" : `${order}重新提取`}</button>
      {state.error ? <span className="inline-error" role="alert">{state.error}</span> : null}
    </>
  );
}

function SourcesTab() {
  const view = useTopicView();
  const query = useVersionSources(view.topicId, view.version.version_id);
  const secAvailable = useSecAvailable();
  const webFetchAvailable = useWebFetchAvailable();
  const allowedHosts = useResearchCapabilities().data?.sources.find((item) => item.name === "official_html_pdf")?.hosts;
  const fetchOptions = { webFetchEnabled: webFetchAvailable, allowedHosts };
  const asOf = view.topic.request.as_of ?? null;
  const candidates = (view.content.source_candidates ?? []).map((row) => ({
    title: typeof row.title === "string" ? row.title : "未命名线索",
    url: typeof row.url === "string" ? row.url : "",
    published: typeof row.published_at === "string" ? row.published_at : null,
  }));
  const known = new Set((query.data ?? []).flatMap((row) => (row.status === "available" && row.url ? [row.url] : [])));
  const rows = query.data ?? [];
  return (
    <>
      <div>
        <div className="gtitle">
          <span className="grow">资料 · {rows.length}</span>
          <button type="button" className="link" onClick={() => view.openPanel({ kind: "add" })} disabled={view.locked} title={view.locked ? "本轮研究进行中，结束后再添加" : undefined}>
            <Icon name="plus" size={14} />添加资料
          </button>
        </div>
        {query.isLoading ? <div className="skeleton line" /> : null}
        {query.isError ? <p className="note inset">资料列表暂时无法加载。</p> : null}
        {query.data && rows.length === 0 ? <p className="note inset">这个研究还没有资料。添加文件、文本或官方网址后，下一轮研究会读取它们。</p> : null}
        {rows.length > 0 ? (
          <div className="list">
            {rows.map((source) => {
              if (source.status !== "available") {
                return <div key={source.source_id} className="item"><span className="title">资料不可用</span><span className="meta mono">{source.source_id}</span></div>;
              }
              const coverage = readCoverage(view.content, source.source_id);
              const reason = ineligibility(source, asOf);
              const canReextract = source.retrieval_method === "user_pdf" && Boolean(source.original_pdf_available) && Boolean(source.pdf_text_spacing_warning);
              return (
                <div key={source.source_id} className="item">
                  <span className="title">{source.title}</span>
                  <span className="meta">{sourceMeta(source)}</span>
                  <Permissions source={source} />
                  {reason ? <span className="flag">下一轮不会读取：{INELIGIBLE_TEXT[reason]}</span> : null}
                  {source.pdf_text_spacing_warning ? (
                    <span className="flag">
                      {canReextract ? "PDF 提取的文字间距可能有误，可以换一种顺序重新提取。" : "PDF 提取的文字间距可能有误，可对照原始文档或保存后重新上传提取。"}
                    </span>
                  ) : null}
                  {coverage.totalCharacters ? <CoverageBar ranges={coverage.ranges} total={coverage.totalCharacters} /> : null}
                  <span className="meta">
                    {coverage.totalCharacters !== null ? `已读 ${formatInteger(coverage.readCharacters)} / ${formatInteger(coverage.totalCharacters)} 字符` : `已读 ${formatInteger(coverage.readCharacters)} 字符`}
                    {" · "}{VERIFICATION_LABEL[source.verification] ?? source.verification}
                  </span>
                  <div className="actions-row">
                    <button type="button" className="btn" onClick={() => view.openPanel({ kind: "reader", sourceId: source.source_id, range: coverage.ranges[0] ?? null })}>打开原文</button>
                    <DownloadOriginalButton source={source} />
                    {canReextract ? <ReextractButton source={source} /> : null}
                  </div>
                </div>
              );
            })}
          </div>
        ) : null}
      </div>
      {candidates.length > 0 ? (
        <div>
          <div className="gtitle">候选线索 · 未核验</div>
          <div className="list leads">
            {candidates.map((lead, index) => {
              let host = "";
              let web = false;
              try {
                const parsed = new URL(lead.url);
                host = parsed.host;
                web = parsed.protocol === "https:" || parsed.protocol === "http:";
              } catch { host = ""; }
              const acquired = known.has(lead.url);
              const fetchable = !acquired && isFetchableUrl(lead.url, secAvailable, fetchOptions);
              return (
                <div key={`${lead.url}-${index}`} className="lead">
                  <span>{lead.title}</span>
                  <span className="meta">{[host, lead.published ? formatDateTime(lead.published).slice(0, 10) : null, acquired ? "已获取" : "未获取"].filter(Boolean).join(" · ")}</span>
                  {!acquired && (fetchable || web) ? (
                    <span className="lead-actions">
                      {fetchable ? (
                        <button type="button" className="link" disabled={view.locked} onClick={() => view.openPanel({ kind: "add", initial: { tab: "url", url: lead.url, title: lead.title } })}>
                          <Icon name="fetch" size={13} />获取原文
                        </button>
                      ) : (
                        <a className="link" href={lead.url} target="_blank" rel="noopener noreferrer"><Icon name="external" size={13} />打开网页</a>
                      )}
                    </span>
                  ) : null}
                </div>
              );
            })}
          </div>
          <p className="note inset">{LEAD_NOTE}{candidates.some((lead) => !isFetchableUrl(lead.url, secAvailable, fetchOptions)) ? " 无法直接获取的网页，可以保存后按文件添加。" : ""}</p>
        </div>
      ) : null}
    </>
  );
}

function factFlags(fact: ResearchV2.ResearchFact): string[] {
  const flags: string[] = [];
  if (fact.currency_quote_missing) flags.push("引文中没有出现币种");
  if (fact.currency_unspecified) flags.push("没有标注币种");
  if (fact.scale_quote_missing) flags.push("引文中没有出现数量单位");
  return flags;
}

function NumbersTab() {
  const view = useTopicView();
  const calculations = view.content.calculations ?? [];
  const facts = view.content.facts ?? [];
  const factById = new Map(facts.map((fact) => [fact.fact_id, fact]));
  const openFact = (fact: ResearchV2.ResearchFact) =>
    view.openPanel({ kind: "reader", sourceId: fact.source_id, range: [fact.location.character_start, fact.location.character_end] });
  if (calculations.length === 0 && facts.length === 0) return <p className="empty-note">还没有记录数字。</p>;
  return (
    <>
      {calculations.length > 0 ? (
        <div>
          <div className="gtitle">计算 · {calculations.length}</div>
          <div className="list">
            {calculations.map((calc, index) => {
              const quantity = formatQuantity(calc.value, calc.unit, calc.currency);
              const turn = view.turnNumber(calc.turn_id);
              return (
                <div key={calc.calculation_id} className="item">
                  <div className="line">
                    <div className="grow">{calculationTitle(calc)}<div className="meta">{calc.expression}</div></div>
                    <div className="right"><div className="value lg">{quantity.value}</div>{quantity.unit ? <div className="meta">{quantity.unit}</div> : null}</div>
                  </div>
                  <div className="chips">
                    {calc.inputs.map((input) => {
                      const fact = factById.get(input.fact_id);
                      return fact ? (
                        <button key={input.fact_id} type="button" className="fact-ref" onClick={() => openFact(fact)} title={fact.label}>{input.fact_id}</button>
                      ) : <span key={input.fact_id} className="mono">{input.fact_id}</span>;
                    })}
                    {calcChecks(calc).map((check) => <span key={check.label} className={`role light ${check.tone}`}>{check.label}</span>)}
                    {turn ? <span>第 {turn} 轮</span> : <span>你添加</span>}
                  </div>
                  {calc.legacy_unit_warning ? <RecalculateRow index={index} /> : null}
                </div>
              );
            })}
          </div>
        </div>
      ) : null}
      {facts.length > 0 ? (
        <div>
          <div className="gtitle">数字 · {facts.length}</div>
          <div className="list">
            {facts.map((fact) => {
              const quantity = formatQuantity(fact.value, fact.unit, fact.currency);
              const turn = view.turnNumber(fact.turn_id);
              return (
                <button key={fact.fact_id} type="button" className="item" onClick={() => openFact(fact)}>
                  <div className="line">
                    <div className="grow">
                      {fact.label}
                      <div className="meta">{[fact.fact_id, fact.period, turn ? `第 ${turn} 轮` : null].filter(Boolean).join(" · ")}</div>
                      {factFlags(fact).map((flag) => <div key={flag} className="flag">{flag}</div>)}
                    </div>
                    <div className="right"><div className="value">{quantity.value}</div>{quantity.unit ? <div className="meta">{quantity.unit}</div> : null}</div>
                  </div>
                </button>
              );
            })}
          </div>
          <p className="note inset">{CALC_NOTE}</p>
        </div>
      ) : null}
      {facts.length >= 2 ? <NewCalculation facts={facts} /> : null}
    </>
  );
}

function calculationErrorText(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) return CONFLICT_TEXT;
  if (error instanceof ApiError && error.status === 422) {
    if (/requires review|unspecified|not identified/i.test(error.message)) return "所选数字的引文、币种或单位需要先复核，暂时不能计算。";
    if (/missing or ambiguous/i.test(error.message)) return "所选数字在当前版本中不存在。";
    if (/limit/i.test(error.message)) return "这个研究的计算数量已达上限。";
    return "这两个数字不能用这个公式计算。请检查期间、单位和币种。";
  }
  return describeError(error);
}

function RecalculateRow({ index }: { index: number }) {
  const view = useTopicView();
  const recalc = useRecalculate(view.topicId);
  return (
    <>
      <span className="flag">旧版本把百分比之差记成了百分比，需要重新计算。</span>
      <div className="actions-row">
        <button type="button" className="btn" disabled={view.locked || recalc.isPending}
          onClick={() => recalc.mutate({ parentVersionId: view.version.version_id, index }, { onSuccess: () => view.notify("已重新计算，并保存为新版本。") })}>
          {recalc.isPending ? "正在计算…" : "重新计算"}
        </button>
        {recalc.isError ? <span className="inline-error" role="alert">{calculationErrorText(recalc.error)}</span> : null}
      </div>
    </>
  );
}

function factOption(fact: ResearchV2.ResearchFact): string {
  const quantity = formatQuantity(fact.value, fact.unit, fact.currency);
  return `${fact.label} · ${fact.period} · ${quantity.value}${quantity.unit ? ` ${quantity.unit}` : ""}`;
}

/** Spec §4.4 新建计算: the backend computes with recorded facts and saves a new version. */
function NewCalculation({ facts }: { facts: readonly ResearchV2.ResearchFact[] }) {
  const view = useTopicView();
  const create = useCreateCalculation(view.topicId);
  const ids = useId();
  const [formula, setFormula] = useState<FormulaId>("growth_percent");
  const [first, setFirst] = useState("");
  const [second, setSecond] = useState("");
  const spec = FORMULAS.find((item) => item.id === formula)!;
  const same = first !== "" && first === second;
  const ready = first !== "" && second !== "" && !same && !view.locked && !create.isPending;
  const blocked = (fact: ResearchV2.ResearchFact) => fact.verification === "user_edited_value_not_in_quotation";
  const submit = () => {
    if (!ready) return;
    create.mutate({ parent_version_id: view.version.version_id, formula, first_fact_id: first, second_fact_id: second }, {
      onSuccess: (topic) => {
        const latest = topic.versions[topic.versions.length - 1];
        const result = latest?.content.calculations?.[latest.content.calculations.length - 1];
        const quantity = result ? formatQuantity(result.value, result.unit, result.currency) : null;
        view.notify(result && quantity ? `已计算${calculationTitle(result)}：${quantity.value}${quantity.unit ? ` ${quantity.unit}` : ""}。已保存为新版本。` : "已计算并保存为新版本。");
        setFirst("");
        setSecond("");
      },
    });
  };
  const select = (id: string, label: string, value: string, onChange: (value: string) => void) => (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <select id={id} value={value} onChange={(event) => onChange(event.target.value)} disabled={view.locked}>
        <option value="">选择数字</option>
        {facts.map((fact) => <option key={fact.fact_id} value={fact.fact_id} disabled={blocked(fact)}>{factOption(fact)}{blocked(fact) ? "（需复核）" : ""}</option>)}
      </select>
    </div>
  );
  return (
    <Disclosure summary="新建计算">
      <Segmented label="公式" options={FORMULAS.map((item) => ({ value: item.id, label: item.label }))} value={formula} onChange={setFormula} />
      <span className="note">{spec.hint}</span>
      {select(`${ids}-first`, spec.first, first, setFirst)}
      {select(`${ids}-second`, spec.second, second, setSecond)}
      {same ? <span className="inline-error">请选择两个不同的数字。</span> : null}
      <div className="actions-row">
        <button type="button" className="btn primary" onClick={submit} disabled={!ready} title={view.locked ? "本轮研究进行中，结束后再计算" : undefined}>
          {create.isPending ? "正在计算…" : "计算"}
        </button>
      </div>
      {create.isError ? <span className="inline-error" role="alert">{calculationErrorText(create.error)}</span> : null}
      <p className="note">计算由系统按已记录的数字完成，结果会保存为新版本。</p>
    </Disclosure>
  );
}

function StepsTab({ turnKey, onTurn }: { turnKey: string | undefined; onTurn: (turnKey: string) => void }) {
  const view = useTopicView();
  const options = view.turns.map((turn) => ({ value: turn.turn_id, label: `第 ${view.turnNumber(turn.turn_id) ?? "?"} 轮` }));
  if (view.live) options.push({ value: LIVE_TURN, label: "进行中" });
  if (options.length === 0) return <p className="empty-note">还没有研究过程。</p>;
  const selected = options.some((option) => option.value === turnKey) ? turnKey! : options[options.length - 1]!.value;
  const liveSelected = Boolean(selected === LIVE_TURN && view.live);
  const events = liveSelected ? view.live!.events : parseTurnEvents(view.turns.find((turn) => turn.turn_id === selected)?.events);
  const rows = stepRows(events);
  const current = liveSelected ? view.live!.progress?.current ?? null : null;
  const interimText = liveSelected ? formatInterimSummary(view.live!.progress?.interim) : "";
  const hasInvalid = events.some((event) => event.kind === "tool" && event.status === "invalid");
  return (
    <>
      {options.length > 1 ? <Segmented label="选择轮次" options={options} value={selected} onChange={onTurn} /> : null}
      {interimText ? <p className="meta pt-interim-bar">{interimText}</p> : null}
      {rows.length === 0 && !current ? (
        <p className="empty-note">这一轮还没有记录步骤。</p>
      ) : (
        <ProcessTimeline events={events} current={current} live={liveSelected} inPanel />
      )}
      {hasInvalid ? <p className="note inset">{INVALID_NOTE}</p> : null}
    </>
  );
}

