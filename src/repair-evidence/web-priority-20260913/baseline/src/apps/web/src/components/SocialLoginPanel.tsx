import { t, useLanguage } from "../i18n";
import type { AccountBillingSummary, OAuthSession } from "../oauth-client";

export type OAuthProvider = "google" | "github";

export interface OAuthProviderAvailability {
  readonly provider: OAuthProvider;
  readonly enabled: boolean;
}

export type OAuthReadinessStatus = "offline" | "loading" | "ready" | "unavailable";

interface SocialLoginPanelProps {
  /**
   * This value must come from a future, public server-readiness endpoint.  A
   * client must never infer it from a client ID or bundle-time environment.
   */
  readonly providers?: readonly OAuthProviderAvailability[] | undefined;
  readonly readinessStatus?: OAuthReadinessStatus | undefined;
  /** Only supplied after the server has created a PKCE transaction. */
  readonly onStart?: ((provider: OAuthProvider) => void) | undefined;
  readonly session?: OAuthSession | null | undefined;
  readonly onLogout?: (() => void) | undefined;
  readonly onDelete?: (() => void) | undefined;
  readonly billingSummary?: AccountBillingSummary | null | undefined;
  /** Used only to place the same truthful status panel at a responsive location. */
  readonly className?: string;
}

const unavailableProviders: readonly OAuthProviderAvailability[] = [
  { provider: "google", enabled: false },
  { provider: "github", enabled: false },
];

export function SocialLoginPanel({ providers = unavailableProviders, readinessStatus = "unavailable", onStart, session = null, onLogout, onDelete, billingSummary = null, className = "" }: SocialLoginPanelProps) {
  const { language } = useLanguage();
  const anyEnabled = providers.some(({ enabled }) => enabled);
  const statusCopy = session
    ? `${session.provider === "google" ? "Google" : "GitHub"} · ${session.account.email}`
    : readinessStatus === "loading"
    ? (language === "en" ? "Checking server OAuth readiness…" : "正在确认服务端登录状态…")
    : readinessStatus === "ready" && anyEnabled
      ? (language === "en" ? "Server sign-in is ready when this build has an explicit callback destination." : "服务端登录已就绪；此版本还需要显式配置回调地址。")
      : t(language, "登录服务尚未配置；你可以继续匿名研究，本地草稿不会因该状态上传。");
  const planName = billingSummary?.plan === "max" ? "Max" : billingSummary?.plan === "pro" ? "Pro" : "Free";
  const quotaLabel = billingSummary ? `${billingSummary.quota_remaining.toLocaleString()} / ${billingSummary.quota_limit.toLocaleString()} tokens` : "";
  const statusLabel = billingSummary?.entitlement_status === "active" ? (language === "en" ? "Active" : "生效中") : billingSummary?.entitlement_status ?? "";
  return <section className={`social-login ${className}`.trim()} aria-labelledby="social-login-title">
    <div className="social-login-copy">
      <p className="eyebrow">{t(language, "可选账户登录")}</p>
      <h2 id="social-login-title">{session ? (language === "en" ? "Account signed in" : "账户已登录") : t(language, "匿名研究中")}</h2>
      <p>{statusCopy}</p>
    </div>
    {session ? <div className="social-login-actions" aria-describedby="social-login-title"><button className="social-login-button" type="button" onClick={onLogout}>{language === "en" ? "Log out" : "退出登录"}</button><button className="social-login-button" type="button" onClick={onDelete}>{language === "en" ? "Delete account" : "删除账户"}</button></div> : <div className="social-login-actions" aria-describedby="social-login-title">
      {providers.map(({ provider, enabled }) => {
        const providerName = provider === "google" ? "Google" : "GitHub";
        const canStart = enabled && Boolean(onStart);
        const buttonStatus = canStart
          ? t(language, "可用")
          : enabled
            ? (language === "en" ? "Callback destination not configured" : "未配置回调地址")
            : t(language, "尚未配置");
        return <button
          key={provider}
          className="social-login-button"
          type="button"
          disabled={!canStart}
          onClick={canStart ? () => onStart?.(provider) : undefined}
          aria-label={`${t(language, "使用账户提供方登录")}: ${providerName} — ${buttonStatus}`}
        >
          <span aria-hidden="true" className={`provider-mark provider-${provider}`}>{provider === "google" ? "G" : "⌘"}</span>
          <span>{providerName}</span>
          <small>{buttonStatus}</small>
        </button>;
      })}
    </div>}
    {session && billingSummary ? <div className="account-billing" aria-label={language === "en" ? "Account quota" : "账户额度"}>
      <div><span className="eyebrow">{language === "en" ? "Plan" : "方案"}</span><strong>{planName}</strong><small>{statusLabel}</small></div>
      <div><span className="eyebrow">{language === "en" ? "Remaining this period" : "本周期剩余"}</span><strong>{quotaLabel}</strong><small>{billingSummary.provider_model}</small></div>
    </div> : null}
  </section>;
}
