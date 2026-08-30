import type { ReactNode } from "react";
import { useLanguage, t } from "../i18n";
import type { Stage } from "../types";
import { SocialLoginPanel, type OAuthProviderAvailability, type OAuthReadinessStatus, type OAuthProvider } from "./SocialLoginPanel";
import type { OAuthSession } from "../oauth-client";

const navItems: readonly { stage: Stage; label: string; icon: string }[] = [
  { stage: "thesis", label: "新建推演", icon: "✎" },
  { stage: "challenge", label: "反方审查", icon: "□" },
  { stage: "worldline", label: "世界线", icon: "⑂" },
  { stage: "portfolio", label: "配置方案", icon: "◈" },
  { stage: "nav", label: "组合路径", icon: "⌁" },
  { stage: "console", label: "控制台", icon: "♢" },
];

interface AppShellProps {
  readonly stage: Stage;
  readonly onStage: (stage: Stage) => void;
  /** Navigation is fail-closed when the parent does not provide a gate. */
  readonly canNavigate?: (stage: Stage) => boolean;
  readonly children: ReactNode;
  readonly inspector?: ReactNode;
  readonly oauthProviders?: readonly OAuthProviderAvailability[];
  readonly oauthReadinessStatus?: OAuthReadinessStatus;
  readonly onOAuthStart?: ((provider: OAuthProvider) => void) | undefined;
  readonly oauthSession?: OAuthSession | null | undefined;
  readonly onOAuthLogout?: (() => void) | undefined;
  readonly onOAuthDelete?: (() => void) | undefined;
}

export function AppShell({ stage, onStage, canNavigate = (target) => target === "thesis" || target === "console", children, inspector, oauthProviders, oauthReadinessStatus, onOAuthStart, oauthSession, onOAuthLogout, onOAuthDelete }: AppShellProps) {
  const { language, setLanguage } = useLanguage();
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">{t(language, "跳到主要内容")}</a>
    <aside className="rail" aria-label={t(language, "研究导航")}>
      <div className="brand-row"><div className="brand">ForesightNine</div><button className="rail-item language-toggle" type="button" onClick={() => setLanguage(language === "zh" ? "en" : "zh")} aria-label={language === "zh" ? "Switch language to English" : "切换为中文"}>{language === "zh" ? "EN" : "中文"}</button></div>
      <nav className="rail-nav">
        {navItems.map((item) => <button key={item.stage} className={`rail-item ${stage === item.stage ? "is-active" : ""}`} aria-current={stage === item.stage ? "page" : undefined} disabled={!canNavigate(item.stage)} onClick={() => { if (canNavigate(item.stage)) onStage(item.stage); }}>
          <span className="rail-icon" aria-hidden="true">{item.icon}</span><span>{t(language, item.label)}</span>
        </button>)}
      </nav>
      <SocialLoginPanel providers={oauthProviders} readinessStatus={oauthReadinessStatus} onStart={onOAuthStart} session={oauthSession} onLogout={onOAuthLogout} onDelete={onOAuthDelete} />
      <div className="rail-footer">
        <div className="rail-rule" />
        <span aria-hidden="true">♢</span>
        <span>{t(language, "公开研究模式 · 不连接券商 · 不托管资金")}</span>
      </div>
    </aside>
    <main id="main-content" className="main-sheet">{children}</main>
    {inspector ? <aside className="inspector" aria-label={t(language, "研究侧栏")}>{inspector}</aside> : null}
  </div>;
}

export function Header({ eyebrow, title, action }: { readonly eyebrow?: string; readonly title: string; readonly action?: ReactNode }) {
  return <header className="page-header">
    <div>{eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}<h1>{title}</h1></div>
    {action ? <div className="header-action">{action}</div> : null}
  </header>;
}

export function Disclosure({ compact = false }: { readonly compact?: boolean }) {
  const { language } = useLanguage();
  return <p className={`disclosure ${compact ? "disclosure-compact" : ""}`}><span aria-hidden="true">♢</span> {t(language, compact ? "情景模拟，不是预测或收益承诺。" : "本产品用于情景研究与教育，不构成投资建议；不连接券商、不下单、不托管资金。")}</p>;
}
