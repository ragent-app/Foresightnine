import { t, useLanguage } from "../i18n";

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
  /** Used only to place the same truthful status panel at a responsive location. */
  readonly className?: string;
}

const unavailableProviders: readonly OAuthProviderAvailability[] = [
  { provider: "google", enabled: false },
  { provider: "github", enabled: false },
];

export function SocialLoginPanel({ providers = unavailableProviders, readinessStatus = "unavailable", onStart, className = "" }: SocialLoginPanelProps) {
  const { language } = useLanguage();
  const anyEnabled = providers.some(({ enabled }) => enabled);
  const statusCopy = readinessStatus === "loading"
    ? (language === "en" ? "Checking server OAuth readiness…" : "正在确认服务端登录状态…")
    : readinessStatus === "ready" && anyEnabled
      ? (language === "en" ? "Server configuration is ready; this build has no safe callback/deep-link handler yet." : "服务端配置已就绪；当前版本尚未接通安全回调/深链处理。")
      : t(language, "登录服务尚未配置；你可以继续匿名研究，本地草稿不会因该状态上传。");
  return <section className={`social-login ${className}`.trim()} aria-labelledby="social-login-title">
    <div className="social-login-copy">
      <p className="eyebrow">{t(language, "可选账户登录")}</p>
      <h2 id="social-login-title">{t(language, "匿名研究中")}</h2>
      <p>{statusCopy}</p>
    </div>
    <div className="social-login-actions" aria-describedby="social-login-title">
      {providers.map(({ provider, enabled }) => {
        const providerName = provider === "google" ? "Google" : "GitHub";
        const canStart = enabled && Boolean(onStart);
        const buttonStatus = canStart
          ? t(language, "可用")
          : enabled
            ? (language === "en" ? "Callback not wired" : "回调未接通")
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
    </div>
  </section>;
}
