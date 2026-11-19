import type { ResearchV2 } from "@foresightnine/contracts";
import { useState } from "react";
import { useVersionSources } from "../../api/queries";
import { downloadSourceOriginal } from "../../api/sources";
import type { AvailableVersionSource } from "../../api/types";
import {
  CALC_NOTE, calcChecks, INVALID_NOTE, LEAD_NOTE, PERMISSION_LABEL, PERMISSION_ORDER, retrievalMethodLabel, SOURCE_KIND_LABEL, toolLabel, VERIFICATION_LABEL,
} from "../../lib/copy";
import { saveBlob } from "../../lib/download";
import { formatDateTime, formatInteger, formatQuantity } from "../../lib/format";
import { ineligibility, INELIGIBLE_TEXT, isFetchableUrl } from "../../lib/sources";
import { claimRole, parseTurnEvents, readCoverage, ROLE_LABEL, stepRows, type AnswerTab, type ClaimRole } from "../../lib/research";
import { Segmented } from "../../ui/controls";
import { Icon, toolIcon } from "../../ui/icons";
import { Panel } from "../../ui/Panel";
import { describeError } from "../research/errors";
import { calculationTitle } from "./Conversation";
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

function ClaimsTab() {
  const view = useTopicView();
  const claims = view.content.claims ?? [];
  if (claims.length === 0) return <p className="empty-note">还没有可引用的结论。</p>;
  const groups: ClaimRole[] = ["support", "counter", "review"];
  return (
    <>
      {groups.map((role) => {
        const list = claims.filter((claim) => claimRole(claim) === role);
        if (list.length === 0 && role !== "review") return null;
        return (
          <div key={role}>
            <div className="gtitle"><span className={`role ${role}`}>{ROLE_LABEL[role]}</span>{list.length}</div>
            {list.length === 0 ? (
              <p className="note inset">没有与已有结论冲突的新证据。</p>
            ) : (
              <div className="list">
                {list.map((claim) => {
                  const numbers = (claim.citations ?? []).map((citation) => view.citations.numberFor(citation)).filter((n) => n > 0);
                  const turn = view.turnNumber(claim.turn_id);
                  const meta = [turn ? `第 ${turn} 轮` : null, numbers.length ? `引用 ${numbers.join("、")}` : "没有引用", claim.edited_by_user ? "你修改过" : null].filter(Boolean).join(" · ");
                  return (
                    <button key={claim.claim_id ?? claim.text} type="button" className="item" onClick={() => claim.claim_id && view.gotoClaim(claim.claim_id)}>
                      <span>{claim.text}</span>
                      <span className="meta">{meta}</span>
                    </button>
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
              return (
                <div key={source.source_id} className="item">
                  <span className="title">{source.title}</span>
                  <span className="meta">{sourceMeta(source)}</span>
                  <Permissions source={source} />
                  {reason ? <span className="flag">下一轮不会读取：{INELIGIBLE_TEXT[reason]}</span> : null}
                  {source.pdf_text_spacing_warning ? <span className="flag">PDF 提取的文字间距可能有误，可以换一种顺序重新提取。</span> : null}
                  {coverage.totalCharacters ? <CoverageBar ranges={coverage.ranges} total={coverage.totalCharacters} /> : null}
                  <span className="meta">
                    {coverage.totalCharacters !== null ? `已读 ${formatInteger(coverage.readCharacters)} / ${formatInteger(coverage.totalCharacters)} 字符` : `已读 ${formatInteger(coverage.readCharacters)} 字符`}
                    {" · "}{VERIFICATION_LABEL[source.verification] ?? source.verification}
                  </span>
                  <div className="actions-row">
                    <button type="button" className="btn" onClick={() => view.openPanel({ kind: "reader", sourceId: source.source_id, range: coverage.ranges[0] ?? null })}>打开原文</button>
                    <DownloadOriginalButton source={source} />
                    {source.retrieval_method === "user_pdf" && source.original_pdf_available && source.pdf_text_spacing_warning ? <ReextractButton source={source} /> : null}
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
              const fetchable = !acquired && isFetchableUrl(lead.url);
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
          <p className="note inset">{LEAD_NOTE}{candidates.some((lead) => !isFetchableUrl(lead.url)) ? " 无法直接获取的网页，可以保存后按文件添加。" : ""}</p>
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
            {calculations.map((calc) => {
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
    </>
  );
}

function StepsTab({ turnKey, onTurn }: { turnKey: string | undefined; onTurn: (turnKey: string) => void }) {
  const view = useTopicView();
  const options = view.turns.map((turn) => ({ value: turn.turn_id, label: `第 ${view.turnNumber(turn.turn_id) ?? "?"} 轮` }));
  if (view.live) options.push({ value: LIVE_TURN, label: "进行中" });
  if (options.length === 0) return <p className="empty-note">还没有研究过程。</p>;
  const selected = options.some((option) => option.value === turnKey) ? turnKey! : options[options.length - 1]!.value;
  const liveSelected = selected === LIVE_TURN && view.live;
  const events = liveSelected ? view.live!.events : parseTurnEvents(view.turns.find((turn) => turn.turn_id === selected)?.events);
  const rows = stepRows(events);
  const current = liveSelected ? view.live!.progress?.current ?? null : null;
  const hasInvalid = events.some((event) => event.kind === "tool" && event.status === "invalid");
  return (
    <>
      {options.length > 1 ? <Segmented label="选择轮次" options={options} value={selected} onChange={onTurn} /> : null}
      {rows.length === 0 && !current ? <p className="empty-note">这一轮还没有记录步骤。</p> : (
        <ol className="steps">
          {rows.map((row) => row.kind === "model" ? (
            <li key={row.index} className="step model"><span /><span>{row.text}</span><span /></li>
          ) : (
            <li key={row.index} className="step">
              <span className="ic"><Icon name={toolIcon(row.tool)} size={16} /></span>
              <span>{row.label}</span>
              <span className={`role light ${row.tone}`}>{row.status}</span>
              {row.rationale || row.reason ? <span className="why">{[row.rationale, row.reason].filter(Boolean).join(" · ")}</span> : null}
            </li>
          ))}
          {current ? (
            <li className="step current">
              <span className="ic"><Icon name={current.kind === "tool" ? toolIcon(current.name) : "sum"} size={16} /></span>
              <span>{current.kind === "tool" ? toolLabel(current.name) : "模型思考中"}</span>
              <span className="role light neutral">进行中</span>
            </li>
          ) : null}
        </ol>
      )}
      {hasInvalid ? <p className="note inset">{INVALID_NOTE}</p> : null}
    </>
  );
}

