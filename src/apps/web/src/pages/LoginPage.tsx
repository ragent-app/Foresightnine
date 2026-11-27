import { useQuery } from "@tanstack/react-query";
import { useId, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { localAuthEnabled, oauthReturnUri } from "../api/config";
import { ApiError } from "../api/http";
import { localLogin, localRegister } from "../auth/local";
import { fetchProviders, startOAuth, type OAuthProvider } from "../auth/oauth";
import { Disclosure } from "../ui/controls";
import { describeError } from "./research/errors";

const PROVIDER_LABEL: Record<OAuthProvider, string> = { google: "使用 Google 继续", github: "使用 GitHub 继续" };

function LocalAccount() {
  const ids = useId();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState<"login" | "register" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const valid = email.length >= 3 && password.length >= 12 && password.length <= 256;

  const run = async (mode: "login" | "register") => {
    if (!valid || busy) return;
    setBusy(mode);
    setError(null);
    try {
      await (mode === "login" ? localLogin(email, password) : localRegister(email, password));
    } catch (failure) {
      setError(failure instanceof ApiError && failure.status === 401 ? "邮箱或密码不正确。" : failure instanceof ApiError && failure.status === 409 ? "这个邮箱已经注册。" : describeError(failure));
    } finally {
      setBusy(null);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run("login");
  };

  return (
    <Disclosure summary="开发环境本地账户">
      <form className="inner" onSubmit={submit}>
        <div className="field"><label htmlFor={`${ids}-mail`}>邮箱</label><input id={`${ids}-mail`} type="email" autoComplete="username" value={email} onChange={(event) => setEmail(event.target.value)} /></div>
        <div className="field">
          <label htmlFor={`${ids}-pw`}>密码</label>
          <input id={`${ids}-pw`} type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />
          <span className="help">至少 12 位</span>
        </div>
        {error ? <div className="banner" role="alert">{error}</div> : null}
        <div className="actions-row">
          <button type="submit" className="btn primary" disabled={!valid || busy !== null}>{busy === "login" ? "正在登录…" : "登录"}</button>
          <button type="button" className="btn" disabled={!valid || busy !== null} onClick={() => void run("register")}>{busy === "register" ? "正在注册…" : "注册"}</button>
        </div>
      </form>
    </Disclosure>
  );
}

/** Spec §4.1. Only providers the backend reports as enabled are offered. */
export function LoginPage() {
  const [params] = useSearchParams();
  const oauthReady = oauthReturnUri() !== null;
  const providers = useQuery({ queryKey: ["auth-providers"], queryFn: fetchProviders, enabled: oauthReady, staleTime: 60_000 });
  const enabled = (providers.data ?? []).filter((item) => item.enabled).map((item) => item.provider);
  const local = localAuthEnabled();

  return (
    <div className="login-wrap" data-page="LoginPage">
      <main className="login glass">
        <span className="brand-mark lg" aria-hidden="true">F9</span>
        <h1 className="heading">登录 ForesightNine</h1>
        <p className="lede">比较关注公司的投资价值，跟踪影响判断的新证据。</p>
        {params.get("oauth") === "failed" ? <div className="banner" role="alert">登录没有完成，请重试。</div> : null}
        {providers.isError ? <div className="banner warn" role="status">暂时无法读取登录方式，请稍后刷新。</div> : null}
        {enabled.map((provider, index) => (
          <button key={provider} type="button" className={`btn lg block${index === 0 ? " primary" : ""}`} onClick={() => startOAuth(provider)}>
            {PROVIDER_LABEL[provider]}
          </button>
        ))}
        {oauthReady && providers.isSuccess && enabled.length === 0 && !local ? <p className="meta">当前没有可用的登录方式。</p> : null}
        {!oauthReady && !local ? <p className="meta">这个环境没有配置登录方式。</p> : null}
        {local ? <LocalAccount /> : null}
        <p className="meta legal"><a href="/privacy.html">隐私政策</a> · <a href="/delete-account.html">删除账户说明</a></p>
      </main>
    </div>
  );
}
