import { useId, useState } from "react";
import type { AvailableVersionSource } from "../../api/types";
import { formatDateTimeWithOffset } from "../../lib/format";
import { ineligibility, INELIGIBLE_TEXT, MAX_SOURCES_PER_RUN } from "../../lib/sources";
import { Disclosure, Segmented } from "../../ui/controls";
import { Panel } from "../../ui/Panel";
import { BUDGET_FIELDS, budgetError, optionsValid, type RunOptions } from "./options";

const SEARCH = [{ value: "on" as const, label: "开启" }, { value: "off" as const, label: "关闭" }];
const SWITCH = [{ value: "auto" as const, label: "自动" }, { value: "off" as const, label: "关闭" }];

export interface TopicFields {
  asOf: string;
  scope: string;
}

type Props =
  | { mode: "new"; options: RunOptions; topic: TopicFields; onApply: (options: RunOptions, topic: TopicFields) => void; onClose: () => void }
  | {
    mode: "topic"; options: RunOptions; asOfIso: string | null; scope: string; sources: readonly AvailableVersionSource[];
    onApply: (options: RunOptions) => void; onClose: () => void;
  };

/** Spec §4.6. On an existing topic the cutoff and scope are fixed at creation and read-only. */
export function ResearchSettingsPanel(props: Props) {
  const [options, setOptions] = useState<RunOptions>(props.options);
  const [topic, setTopic] = useState<TopicFields>(props.mode === "new" ? props.topic : { asOf: "", scope: "" });
  const ids = useId();
  const valid = optionsValid(options);

  const apply = () => {
    if (!valid) return;
    if (props.mode === "new") props.onApply(options, topic);
    else props.onApply(options);
    props.onClose();
  };

  return (
    <Panel
      title="研究设置"
      onClose={props.onClose}
      footer={<><button type="button" className="btn plain" onClick={props.onClose}>取消</button><button type="button" className="btn primary" onClick={apply} disabled={!valid}>应用</button></>}
    >
      <p className="note inset">默认设置适合大多数问题。这里的修改只对下一轮生效。</p>
      <div className="field">
        <label htmlFor={`${ids}-asof`}>截止时间</label>
        {props.mode === "new" ? (
          <input id={`${ids}-asof`} type="datetime-local" value={topic.asOf} onChange={(event) => setTopic({ ...topic, asOf: event.target.value })} />
        ) : (
          <input id={`${ids}-asof`} readOnly value={props.asOfIso ? formatDateTimeWithOffset(props.asOfIso) : "未设置"} />
        )}
        <span className="help">只使用此时间之前公开的资料。{props.mode === "topic" ? "主题创建后不能修改。" : "留空表示不限制。"}</span>
      </div>
      <div className="field">
        <label htmlFor={`${ids}-scope`}>研究范围与期望成果</label>
        {props.mode === "new" ? (
          <textarea id={`${ids}-scope`} value={topic.scope} maxLength={4000} onChange={(event) => setTopic({ ...topic, scope: event.target.value })} placeholder="例如：只看最近两个财年，输出一张对比表" />
        ) : (
          <textarea id={`${ids}-scope`} readOnly value={props.scope || "未设置"} />
        )}
        {props.mode === "topic" ? <span className="help">主题创建后不能修改。</span> : null}
      </div>
      <div className="list">
        <div className="item">
          <div className="line"><span className="grow">联网搜索</span>
            <Segmented inline label="联网搜索" options={SEARCH} value={options.discover} onChange={(discover) => setOptions({ ...options, discover })} />
          </div>
          <span className="meta">搜索结果只作为线索，获取原文后才能作为证据。</span>
        </div>
        <div className="item">
          <div className="line"><span className="grow">自动获取官方资料</span>
            <Segmented inline label="自动获取官方资料" options={SWITCH} value={options.autoFetch} onChange={(autoFetch) => setOptions({ ...options, autoFetch })} />
          </div>
        </div>
      </div>
      {props.mode === "topic" && props.sources.length > 0 ? (
        <SourceSelection sources={props.sources} asOfIso={props.asOfIso} selected={options.selectedSourceIds} onChange={(selectedSourceIds) => setOptions({ ...options, selectedSourceIds })} />
      ) : null}
      <Disclosure summary="预算（可选）" defaultOpen={BUDGET_FIELDS.some(({ key }) => options.budget[key] !== "")}>
        <div className="grid2">
          {BUDGET_FIELDS.map((field) => {
            const error = budgetError(field.key, options.budget[field.key]);
            const id = `${ids}-${field.key}`;
            return (
              <div className="field" key={field.key}>
                <label htmlFor={id}>{field.label}</label>
                <input
                  id={id}
                  inputMode={field.decimal ? "decimal" : "numeric"}
                  placeholder={field.placeholder}
                  value={options.budget[field.key]}
                  aria-invalid={error ? true : undefined}
                  aria-describedby={error ? `${id}-err` : undefined}
                  onChange={(event) => setOptions({ ...options, budget: { ...options.budget, [field.key]: event.target.value } })}
                />
                {error ? <span className="err" id={`${id}-err`}>{error}</span> : null}
              </div>
            );
          })}
        </div>
        <p className="note">留空表示不设上限；0 表示该项额度为零。单次请求超时和供应商限制仍然有效。</p>
      </Disclosure>
    </Panel>
  );
}

/** Which of the topic's sources the next turn reads; all eligible ones by default (null). */
function SourceSelection({ sources, asOfIso, selected, onChange }: {
  sources: readonly AvailableVersionSource[];
  asOfIso: string | null;
  selected: readonly string[] | null;
  onChange: (selected: readonly string[] | null) => void;
}) {
  const reasons = new Map(sources.map((source) => [source.source_id, ineligibility(source, asOfIso)]));
  const eligible = sources.filter((source) => reasons.get(source.source_id) === null).map((source) => source.source_id);
  const chosen = new Set(selected ?? eligible);
  const count = eligible.filter((id) => chosen.has(id)).length;
  const toggle = (id: string) => {
    const next = new Set(chosen);
    if (next.has(id)) next.delete(id); else next.add(id);
    const ids = eligible.filter((item) => next.has(item));
    onChange(ids.length === eligible.length ? null : ids);
  };
  return (
    <div className="field">
      <span className="lt">本轮读取的资料</span>
      <div className="list">
        {sources.map((source) => {
          const reason = reasons.get(source.source_id) ?? null;
          return (
            <label key={source.source_id} className={`item check-row${reason ? " off" : ""}`}>
              <input type="checkbox" checked={reason === null && chosen.has(source.source_id)} disabled={reason !== null} onChange={() => toggle(source.source_id)} />
              <span className="grow">
                <span>{source.title}</span>
                {reason ? <span className="meta">不会读取：{INELIGIBLE_TEXT[reason]}</span> : null}
              </span>
            </label>
          );
        })}
      </div>
      <span className="help">
        已选 {count} / {eligible.length} 份可读取的资料。{count === 0 ? "不选择资料时，本轮只依靠联网搜索和自动获取的资料。" : ""}
        {count > MAX_SOURCES_PER_RUN ? `每轮最多读取 ${MAX_SOURCES_PER_RUN} 份，超出的部分不会处理。` : ""}
      </span>
    </div>
  );
}
