import { t, useLanguage } from "../i18n";

export type OAuthProvider = "google" | "github";

export interface OAuthProviderAvailability {
  readonly provider: OAuthProvider;
  readonly configured: boolean;
}

interface SocialLoginPanelProps {
  /**
   * This value must come from a future, public server-readiness endpoint.  A
   * client must never infer it from a client ID or bundle-time environment.
   */
  readonly providers?: readonly OAuthProviderAvailability[];
  /** Only supplied after the server has created a PKCE transaction. */
  readonly onStart?: (provider: OAuthProvider) => void;
  /** Used only to place the same truthful status panel at a responsive location. */
  readonly className?: string;
}

const unavailableProviders: readonly OAuthProviderAvailability[] = [
  { provider: "google", configured: false },
  { provider: "github", configured: false },
];

export function SocialLoginPanel({ providers = unavailableProviders, onStart, className = "" }: SocialLoginPanelProps) {
  const { language } = useLanguage();
  return <section className={`social-login ${className}`.trim()} aria-labelledby="social-login-title">
    <div className="social-login-copy">
      <p className="eyebrow">{t(language, "可选账户登录")}</p>
      <h2 id="social-login-title">{t(language, "匿名研究中")}</h2>
      <p>{t(language, "登录服务尚未配置；你可以继续匿名研究，本地草稿不会因该状态上传。")}</p>
    </div>
    <div className="social-login-actions" aria-describedby="social-login-title">
      {providers.map(({ provider, configured }) => {
        const providerName = provider === "google" ? "Google" : "GitHub";
        const enabled = configured && Boolean(onStart);
        return <button
          key={provider}
          className="social-login-button"
          type="button"
          disabled={!enabled}
          onClick={enabled ? () => onStart?.(provider) : undefined}
          aria-label={`${t(language, "使用账户提供方登录")}: ${providerName}${enabled ? "" : ` — ${t(language, "尚未配置")}`}`}
        >
          <span aria-hidden="true" className={`provider-mark provider-${provider}`}>{provider === "google" ? "G" : "⌘"}</span>
          <span>{providerName}</span>
          <small>{enabled ? t(language, "可用") : t(language, "尚未配置")}</small>
        </button>;
      })}
    </div>
  </section>;
}
