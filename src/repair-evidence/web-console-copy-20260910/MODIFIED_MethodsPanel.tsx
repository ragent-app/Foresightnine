import { Button, Tag } from "./Controls";
import { Header, Disclosure } from "./AppShell";
import { SocialLoginPanel, type OAuthProviderAvailability, type OAuthReadinessStatus, type OAuthProvider } from "./SocialLoginPanel";
import { t, useLanguage } from "../i18n";
import type { AccountBillingSummary, OAuthSession } from "../oauth-client";

interface ConsoleProps {
  readonly onExport: () => void;
  readonly providers?: readonly OAuthProviderAvailability[];
  readonly readinessStatus?: OAuthReadinessStatus;
  readonly onOAuthStart?: ((provider: OAuthProvider) => void) | undefined;
  readonly oauthSession?: OAuthSession | null;
  readonly onOAuthLogout?: () => void;
  readonly onOAuthDelete?: () => void;
  readonly billingSummary?: AccountBillingSummary | null;
}
const evidenceGrades: readonly (readonly [string, string, string])[] = [
  ["A", "原始数据、监管文件、公司公告", "来自政府、监管、央行、国际组织、主流学术不期明的公开材料"],
  ["B", "可靠媒体、机构研究、可追溯数据库", "由具有专业声誉的机构或研究者基于 A 级来源形成"],
  ["C", "二手评论或专家访谈", "来源可识别但观点本身仍需审查"],
  ["D", "用户内部经验或不可外部验证信息", "可作为私有前提单独标记"],
  ["E", "模型推断", "不直接来自可核验的外部来源"],
];
export function ConsolePanel({ onExport, providers, readinessStatus, onOAuthStart, oauthSession, onOAuthLogout, onOAuthDelete, billingSummary }: ConsoleProps) {
  const { language } = useLanguage();
  const billingStatus = billingSummary?.plan === "max" ? "Max" : billingSummary?.plan === "pro" ? "Pro" : "Free";
  const billingCopy = billingSummary ? `${billingStatus} · ${billingSummary.quota_remaining.toLocaleString()} / ${billingSummary.quota_limit.toLocaleString()} tokens · ${billingSummary.provider_model}` : t(language, "Google Play 月度订阅已配置：Pro 每月 3000 万 token，Max 每月 5 亿 token。商品需在 Play Console 激活后才可购买；会员权益由服务端验证。");
  return <section className="content-column methods-view"><Header eyebrow={t(language, "可选账户登录")} title={t(language, "研究控制台")} action={<Button variant="secondary" onClick={onExport}>↓ {t(language, "下载方法说明")}</Button>} /><p className="lead">{t(language, "账户状态、模型路由和产品边界都在这里明确呈现；未配置项不会模拟为可用服务。")}</p><SocialLoginPanel className="console-login" providers={providers} readinessStatus={readinessStatus} onStart={onOAuthStart} session={oauthSession} onLogout={onOAuthLogout} onDelete={onOAuthDelete} billingSummary={billingSummary} /><section className="console-grid" aria-label={t(language, "控制台状态")}><article><p className="eyebrow">{t(language, "模型提供方路由")}</p><h2>{t(language, "服务端 OpenAI-compatible · 本机 Cline pool")}</h2><span className="console-status">{billingSummary?.provider_model ?? t(language, "已配置")}</span><p>{t(language, "研究请求通过服务端 OpenAI-compatible 适配器路由到本机 Cline pool；模型密钥不会下发到设备。")}</p></article><article><p className="eyebrow">{t(language, "个人 OpenAI 兼容提供方")}</p><h2>{t(language, "预留接口")}</h2><span className="console-status">{t(language, "尚未配置")}</span><p>{t(language, "未来版本可通过加密的服务端凭据仓配置兼容端点和模型选择；本构建没有 BYOK 输入、持久化或网络调用。")}</p></article><article><p className="eyebrow">{t(language, "订阅与账单")}</p><h2>Google Play</h2><span className="console-status">{billingSummary ? billingStatus : t(language, "已配置")}</span><p>{billingCopy}</p></article></section><nav className="method-nav" aria-label={t(language, "研究方法导航")}><a href="#evidence">1 {t(language, "观点审查")}</a><a href="#evidence">2 {t(language, "证据等级")}</a><a href="#probability">3 {t(language, "世界线概率")}</a><a href="#fm-method">4 {t(language, "FM 因子模型")}</a><a href="#public">5 {t(language, "公开版安全层")}</a><a href="#risk">6 {t(language, "风险与限制")}</a></nav><section id="evidence" className="method-section"><h2>2 {t(language, "证据等级")}</h2><p>{t(language, "所有证据在使用前被分级；级别不代表正确性，仅表示来源与可核验性。")}</p><div className="evidence-scale">{evidenceGrades.map(([grade, title, desc]) => <div key={grade}><span className={`grade grade-${grade}`}>{grade}</span><strong>{t(language, title)}</strong><small>{t(language, desc)}</small></div>)}</div><p className="method-note">{t(language, "等级表示来源与可核验性，不代表结论正确；D/E 不得表述为公开事实。")}</p></section><section id="probability" className="method-section split-section"><div><h2>3 {t(language, "世界线概率")}</h2><p><i>P(H)</i> {t(language, "是前提成立的先验概率；")}<i>P(W | H)</i> {t(language, "是在假设成立条件下世界线发生的条件概率；")}<i>P(H∩W)</i> {t(language, "用于排序与组合。")}</p><Tag tone="accent">{t(language, "区间优先")} · model_estimate_unvalidated</Tag></div><div className="equation">P(H ∩ W) = P(H) × P(W | H)<br /><small>{t(language, "联合区间按配置精度重新计算")}</small></div></section><section id="fm-method" className="method-section split-section"><div><h2>4 {t(language, "组合双层输出")}</h2><h3>FM Raw（{t(language, "实验层")}）</h3><p>{t(language, "保留原始因子、假设、参数与结果，不做迎合性或可读性改写。")}</p></div><div id="public"><h3 className="accent-heading">Public Default（{t(language, "公开层")})</h3><p>{t(language, "在不覆盖 Raw 的前提下，叠加集中度、流动性、可实施性与冲击约束，显示每项调整理由。")}</p></div></section><section id="risk" className="risk-box"><span aria-hidden="true">♢</span><div><h2>{t(language, "风险与限制")}</h2><p>{t(language, "本产品用于情景研究与教育，不构成投资建议；不连接券商、不下单、不托管资金。")}</p><p>{t(language, "所有结果基于公开信息、用户输入与模型推断，存在重大不确定性与误差风险。")}</p></div></section><Disclosure /></section>;
}
