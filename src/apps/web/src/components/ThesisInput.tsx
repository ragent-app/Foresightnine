import { useMemo, useState } from "react";
import { Button, Field, Input, Select, Textarea } from "./Controls";
import { Disclosure, Header } from "./AppShell";
import { t, useLanguage } from "../i18n";
import { currencySymbol, validateThesis } from "../validation";
import type { Currency } from "../types";

interface ThesisInputProps {
  readonly willArchiveOnEdit?: boolean;
  readonly hasExistingReview?: boolean;
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
  const { language } = useLanguage();
  const [submitted, setSubmitted] = useState(false);
  const errors = useMemo(() => submitted ? validateThesis(props) : {}, [props, submitted]);
  const onSubmit = (event: React.FormEvent) => { event.preventDefault(); if (props.hasExistingReview) { props.onStart(); return; } setSubmitted(true); if (Object.keys(validateThesis(props)).length === 0) props.onStart(); };
  return <section className="content-column thesis-view">
    <Header title={t(language, "把观点变成可审计的世界线")} />
    <p className="lead">{t(language, "描述前提、时间段、关键事件，以及你预期它将如何影响资产。")}</p>
    {props.willArchiveOnEdit ? <p role="status">{language === "en" ? "Editing the premise saves this research and starts a separate draft." : "修改观点会先保存本研究，再建立独立草稿。"}</p> : props.draftRestored ? <div className="notice notice-info" role="status">{t(language, "已恢复草稿。")}</div> : null}
    <form onSubmit={onSubmit} noValidate>
      <Field label={t(language, "你的观点")} error={errors.viewpoint ? t(language, errors.viewpoint) : undefined} helper={t(language, "最多 2,000 个字符。")}>
        <Textarea value={props.viewpoint} onChange={(event) => props.onViewpoint(event.target.value)} maxLength={2000} placeholder={t(language, "如果未来两年全球数据中心电力需求持续上升，并推动电网设备投资提速，那么……")} aria-invalid={Boolean(errors.viewpoint)} />
        <span className="character-count">{props.viewpoint.length} / 2000</span>
      </Field>
      <div className="money-row">
        <Field label={t(language, "可投资资金")} error={errors.capital ? t(language, errors.capital) : undefined} helper={t(language, "仅用于情景模拟，不会连接账户。")}>
          <div className="money-input"><span aria-hidden="true">{currencySymbol(props.currency)}</span><Input inputMode="decimal" value={props.capital} onChange={(event) => props.onCapital(event.target.value)} aria-label={t(language, "可投资资金金额")} aria-invalid={Boolean(errors.capital)} /><Select value={props.currency} onChange={(event) => props.onCurrency(event.target.value)} aria-label={t(language, "基础货币")}><option>CNY</option><option>USD</option><option>EUR</option></Select></div>
        </Field>
        <Button variant="primary" type="submit">{props.hasExistingReview ? (language === "en" ? "Open review" : "查看反方审查") : t(language, "开始反方审查")} <span aria-hidden="true">→</span></Button>
      </div>
    </form>
    <div className="process-rail" aria-label={t(language, "研究进度")}>
      {["观点", "证据", "世界线", "配置"].map((label, index) => <div className={`process-step ${index === 0 ? "is-current" : ""}`} key={label}><span>{index + 1}</span><b>{t(language, label)}</b></div>)}
    </div>
    <div className="thesis-actions"><Button variant="quiet" type="button" onClick={props.onReset}>{language === "en" ? "Save and start new" : "保存并新建"}</Button><span>{t(language, "草稿自动保存于此设备。")}</span></div>
    <Disclosure />
  </section>;
}
