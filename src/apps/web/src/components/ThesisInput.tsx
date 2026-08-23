import { useMemo, useState } from "react";
import { Button, Field, Input, Select, Textarea } from "./Controls";
import { Disclosure, Header } from "./AppShell";
import { currencySymbol, validateThesis } from "../validation";
import type { Currency } from "../types";

interface ThesisInputProps {
  readonly viewpoint: string;
  readonly capital: string;
  readonly currency: Currency;
  readonly draftRestored: boolean;
  readonly onViewpoint: (value: string) => void;
  readonly onCapital: (value: string) => void;
  readonly onCurrency: (value: string) => void;
  readonly onStart: () => void;
  readonly onReset: () => void;
}

export function ThesisInput(props: ThesisInputProps) {
  const [submitted, setSubmitted] = useState(false);
  const errors = useMemo(() => submitted ? validateThesis(props) : {}, [props, submitted]);
  const onSubmit = (event: React.FormEvent) => { event.preventDefault(); setSubmitted(true); if (Object.keys(validateThesis(props)).length === 0) props.onStart(); };
  return <section className="content-column thesis-view">
    <Header title="把观点变成可审计的世界线" />
    <p className="lead">描述前提、时间段、关键事件，以及你预期它将如何影响资产。</p>
    {props.draftRestored ? <div className="notice notice-info" role="status">已恢复本地草稿。内容只保存在此设备。</div> : null}
    <form onSubmit={onSubmit} noValidate>
      <Field label="你的观点" error={errors.viewpoint} helper="观点最多 2,000 个字符；可在刷新后恢复草稿。">
        <Textarea value={props.viewpoint} onChange={(event) => props.onViewpoint(event.target.value)} maxLength={2000} placeholder="如果未来两年全球数据中心电力需求持续上升，并推动电网设备投资提速，那么……" aria-invalid={Boolean(errors.viewpoint)} />
        <span className="character-count">{props.viewpoint.length} / 2000</span>
      </Field>
      <div className="money-row">
        <Field label="可投资资金" error={errors.capital} helper="仅用于情景模拟，不会连接账户。">
          <div className="money-input"><span aria-hidden="true">{currencySymbol(props.currency)}</span><Input inputMode="decimal" value={props.capital} onChange={(event) => props.onCapital(event.target.value)} aria-label="可投资资金金额" aria-invalid={Boolean(errors.capital)} /><Select value={props.currency} onChange={(event) => props.onCurrency(event.target.value)} aria-label="基础货币"><option>CNY</option><option>USD</option><option>EUR</option></Select></div>
        </Field>
        <Button variant="primary" type="submit">开始反方审查 <span aria-hidden="true">→</span></Button>
      </div>
    </form>
    <div className="process-rail" aria-label="研究进度">
      {["观点", "证据", "世界线", "配置"].map((label, index) => <div className={`process-step ${index === 0 ? "is-current" : ""}`} key={label}><span>{index + 1}</span><b>{label}</b></div>)}
    </div>
    <div className="thesis-actions"><Button variant="quiet" type="button" onClick={props.onReset}>重置</Button><span>草稿自动保存于本地 · 不上传观点</span></div>
    <Disclosure />
  </section>;
}
