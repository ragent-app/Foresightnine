import { Button, StatusDot, Tag } from "./Controls";
import { Header } from "./AppShell";
import { t, useLanguage } from "../i18n";
import type { FmStatus } from "../types";

interface FmProps { readonly status: FmStatus; readonly progress: number; readonly step: string; readonly resultReady: boolean; readonly onCompute: () => void; readonly onCancel: () => void; readonly onPortfolio: () => void; }
const steps = ["验证签名数据快照", "筛选有效因子", "截面百分位与 IC 方向", "合成信号与标准化", "目标权重与成本死区"];

export function FmPanel({ status, progress, step, resultReady, onCompute, onCancel, onPortfolio }: FmProps) {
  const { language } = useLanguage();
  return <section className="content-column fm-view">
    <Header title={t(language, "本地因子计算")} action={<span className="operation-label"><StatusDot tone={status === "complete" ? "sage" : status === "failed" ? "danger" : "accent"} />{status === "computing" ? `${t(language, "正在用户设备上计算")} · ${progress}%` : status === "complete" ? t(language, "本地计算完成") : t(language, "等待本地计算")}</span>} />
    <div className="fm-topline"><span>{t(language, "原始市场分片不会上传最终组合")}</span><span className="worker-badge">Web Worker · {t(language, "本地计算")}</span></div>
    <div className="fm-grid"><ol className="trace-list">{steps.map((item, index) => <li className={`${progress >= (index + 1) * 20 ? "is-done" : progress > index * 20 ? "is-active" : ""}`} key={item}><span>{progress >= (index + 1) * 20 ? "✓" : index + 1}</span><div><strong>{t(language, item)}</strong><small>{progress >= (index + 1) * 20 ? t(language, "已完成") : progress > index * 20 ? t(language, step) : t(language, "等待当前步骤完成")}</small></div></li>)}</ol><div className="factor-table-wrap"><table className="factor-table"><caption>{t(language, "因子质量与录取状态")}</caption><thead><tr><th>{t(language, "因子")}</th><th>IC</th><th>ICIR</th><th>{t(language, "质量权重")}</th><th>{t(language, "状态")}</th></tr></thead><tbody>{[["盈利修正", "0.072", "0.64", "0.046", "已接受"], ["质量", "0.051", "0.58", "0.030", "已接受"], ["中期动量", "−0.044", "−0.52", "0.023", "已接受"], ["低波动", "0.028", "0.31", "0.009", "已拒绝"], ["估值", "−0.018", "−0.21", "0.004", "已拒绝"]].map((row) => <tr key={row[0]}>{row.map((cell, index) => <td key={`${row[0]}-${index}`}>{index === 4 ? <><StatusDot tone={cell === "已接受" ? "sage" : "danger"} /> {t(language, cell)}</> : t(language, cell)}</td>)}</tr>)}</tbody></table><p className="formula-note">{t(language, "质量权重 q")}<sub>f</sub> = max(|IC<sub>f</sub>|, 1e−6) × max(|ICIR<sub>f</sub>|, 1e−6)。{t(language, "负 IC 只改变贡献方向，不改变质量权重。")}</p></div></div>
    <div className="fm-explanation"><div><Tag tone="accent">FM-v1</Tag><h2>{t(language, "合成与标准化信号")}</h2><p>rank_pct(signal) − 0.5 → {t(language, "按 IC 方向调整")} → {t(language, "按有效支持加权")} → {t(language, "截面 z-score")} → clip / re-normalise。</p></div><div className="fm-controls">{status === "computing" ? <Button variant="danger" onClick={onCancel}>{t(language, "暂停 / 取消计算")}</Button> : resultReady ? <Button variant="primary" onClick={onPortfolio}>{t(language, "查看配置方案")}</Button> : <Button variant="primary" onClick={onCompute}>{t(language, "开始本地计算")}</Button>}{resultReady ? <span className="intermediate-note">{t(language, "中间值已包含在 JSON 审计导出中")}</span> : <span className="intermediate-note">{t(language, "完成计算后可在 JSON 审计导出中查看")}</span>}</div></div>
    {status === "cancelled" ? <div className="notice notice-warning" role="status">{t(language, "本地计算已取消；中间结果未上传。")} <Button variant="quiet" onClick={onCompute}>{t(language, "继续计算")}</Button></div> : null}
    {status === "failed" ? <div className="notice notice-warning" role="alert">{t(language, "因子计算或金融数据获取失败，已保留研究状态。")} <Button variant="quiet" onClick={onCompute}>{t(language, "重试")}</Button></div> : null}
  </section>;
}
