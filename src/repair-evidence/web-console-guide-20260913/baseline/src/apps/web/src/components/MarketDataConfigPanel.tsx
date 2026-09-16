import { useEffect, useMemo, useState } from "react";
import { Button, Input, StatusDot } from "./Controls";
import type { OAuthSession } from "../oauth-client";
import {
  fetchMarketDataConfig,
  saveMarketDataConfig,
  testMarketDataProvider,
  type MarketDataConfig,
  type MarketProviderDraft,
  type MarketProviderId,
} from "../market-config-client";
import { useLanguage } from "../i18n";

interface Props {
  readonly apiBaseUrl: string | null;
  readonly session: OAuthSession | null;
  readonly onSaved?: () => void;
}

type TestState = Readonly<Record<string, "testing" | "ready" | "unavailable">>;

export function MarketDataConfigPanel({ apiBaseUrl, session, onSaved }: Props) {
  const { language } = useLanguage();
  const [config, setConfig] = useState<MarketDataConfig | null>(null);
  const [order, setOrder] = useState<readonly MarketProviderId[]>([]);
  const [enabled, setEnabled] = useState<Readonly<Record<string, boolean>>>({});
  const [keys, setKeys] = useState<Readonly<Record<string, string>>>({});
  const [clearKeys, setClearKeys] = useState<Readonly<Record<string, boolean>>>({});
  const [status, setStatus] = useState<"idle" | "loading" | "saving" | "saved" | "error">("idle");
  const [tests, setTests] = useState<TestState>({});
  const [dragged, setDragged] = useState<MarketProviderId | null>(null);

  useEffect(() => {
    if (!apiBaseUrl || !session) { setConfig(null); setOrder([]); setStatus("idle"); return; }
    const controller = new AbortController();
    setStatus("loading");
    void fetchMarketDataConfig(apiBaseUrl, session, controller.signal).then((next) => {
      if (controller.signal.aborted) return;
      setConfig(next);
      setOrder(next.fallback_order);
      setEnabled(Object.fromEntries(next.providers.map((item) => [item.provider_id, item.enabled])));
      setKeys({}); setClearKeys({}); setStatus("idle");
    }).catch(() => { if (!controller.signal.aborted) setStatus("error"); });
    return () => controller.abort();
  }, [apiBaseUrl, session]);

  const byId = useMemo(() => new Map(config?.providers.map((item) => [item.provider_id, item]) ?? []), [config]);
  const drafts = (): readonly MarketProviderDraft[] => order.map((providerId) => ({
    provider_id: providerId,
    enabled: enabled[providerId] ?? true,
    ...(keys[providerId]?.trim() ? { api_key: keys[providerId].trim() } : {}),
    ...(clearKeys[providerId] ? { clear_api_key: true } : {}),
  }));
  const persist = async (): Promise<MarketDataConfig> => {
    if (!apiBaseUrl || !session) throw new Error("Sign-in required");
    const next = await saveMarketDataConfig(apiBaseUrl, session, order, drafts());
    setConfig(next); setOrder(next.fallback_order);
    setEnabled(Object.fromEntries(next.providers.map((item) => [item.provider_id, item.enabled])));
    setKeys({}); setClearKeys({}); onSaved?.();
    return next;
  };
  const save = (): void => {
    setStatus("saving");
    void persist().then(() => setStatus("saved")).catch(() => setStatus("error"));
  };
  const test = (providerId: MarketProviderId): void => {
    if (!apiBaseUrl || !session) return;
    setStatus("saving"); setTests((current) => ({ ...current, [providerId]: "testing" }));
    void persist().then(() => testMarketDataProvider(apiBaseUrl, session, providerId)).then((result) => {
      setStatus("saved"); setTests((current) => ({ ...current, [providerId]: result }));
    }).catch(() => { setStatus("error"); setTests((current) => ({ ...current, [providerId]: "unavailable" })); });
  };
  const move = (providerId: MarketProviderId, direction: -1 | 1): void => {
    setOrder((current) => {
      const index = current.indexOf(providerId); const target = index + direction;
      if (index < 0 || target < 0 || target >= current.length) return current;
      const copy = [...current]; [copy[index], copy[target]] = [copy[target]!, copy[index]!]; return copy;
    });
  };
  const dropBefore = (target: MarketProviderId): void => {
    if (!dragged || dragged === target) return;
    setOrder((current) => {
      const copy = current.filter((item) => item !== dragged); copy.splice(copy.indexOf(target), 0, dragged); return copy;
    });
    setDragged(null);
  };

  if (!session) return <section className="market-config-panel"><div><p className="eyebrow">{language === "en" ? "Market data sources" : "金融数据源"}</p><h2>{language === "en" ? "Sign in to use your API keys" : "登录后配置你的 API Key"}</h2><p>{language === "en" ? "The platform fallback remains available without saving browser credentials." : "平台 fallback 仍可使用；浏览器不会保存或回显你的密钥。"}</p></div></section>;
  if (status === "loading" || !config) return <section className="market-config-panel" aria-busy="true"><StatusDot /> {language === "en" ? "Loading market data settings…" : "正在读取金融数据源配置…"}</section>;

  return <section className="market-config-panel" aria-labelledby="market-config-title">
    <div className="market-config-heading"><div><p className="eyebrow">{language === "en" ? "Market data sources" : "金融数据源"}</p><h2 id="market-config-title">{language === "en" ? "Personal fallback chain" : "个人数据源 fallback"}</h2><p>{language === "en" ? "Your encrypted key overrides the matching platform credential. Drag or use the arrow buttons to set priority." : "个人 Key 会优先替代同一数据源的平台凭据；拖动或使用箭头调整优先级。"}</p></div><Button variant="primary" type="button" disabled={status === "saving"} onClick={save}>{status === "saving" ? (language === "en" ? "Saving…" : "保存中…") : (language === "en" ? "Save sources" : "保存数据源")}</Button></div>
    <ol className="provider-order">
      {order.map((providerId, index) => {
        const provider = byId.get(providerId); if (!provider) return null;
        const testState = tests[providerId];
        return <li key={providerId} draggable onDragStart={() => setDragged(providerId)} onDragOver={(event) => event.preventDefault()} onDrop={() => dropBefore(providerId)} className="provider-card">
          <div className="provider-rank" aria-hidden="true">{index + 1}</div>
          <div className="provider-main"><div className="provider-title"><strong>{provider.display_name}</strong><span>{provider.personal_key_configured && !clearKeys[providerId] ? provider.key_hint : provider.platform_available ? (language === "en" ? "Platform fallback" : "平台 fallback") : (language === "en" ? "Key required" : "需要 Key")}</span></div>
            <label className="provider-toggle"><input type="checkbox" checked={enabled[providerId] ?? true} onChange={(event) => setEnabled((current) => ({ ...current, [providerId]: event.target.checked }))} /> {language === "en" ? "Enabled" : "启用"}</label>
            {provider.requires_key ? <div className="provider-key-row"><Input type="password" autoComplete="off" value={keys[providerId] ?? ""} onChange={(event) => { setKeys((current) => ({ ...current, [providerId]: event.target.value })); setClearKeys((current) => ({ ...current, [providerId]: false })); }} placeholder={provider.personal_key_configured ? (language === "en" ? "Leave blank to keep saved key" : "留空则保留已保存 Key") : (language === "en" ? "Paste provider API key" : "填写数据源 API Key")} aria-label={`${provider.display_name} API key`} />{provider.personal_key_configured ? <Button variant="quiet" type="button" onClick={() => { setKeys((current) => ({ ...current, [providerId]: "" })); setClearKeys((current) => ({ ...current, [providerId]: true })); }}>{language === "en" ? "Remove key" : "移除 Key"}</Button> : null}</div> : null}
          </div>
          <div className="provider-actions"><Button variant="quiet" type="button" disabled={index === 0} onClick={() => move(providerId, -1)} aria-label={`${provider.display_name} ${language === "en" ? "move up" : "上移"}`}>↑</Button><Button variant="quiet" type="button" disabled={index === order.length - 1} onClick={() => move(providerId, 1)} aria-label={`${provider.display_name} ${language === "en" ? "move down" : "下移"}`}>↓</Button><Button variant="secondary" type="button" disabled={status === "saving" || !(enabled[providerId] ?? true)} onClick={() => test(providerId)}>{testState === "testing" ? (language === "en" ? "Testing…" : "测试中…") : (language === "en" ? "Save & test" : "保存并测试")}</Button>{testState && testState !== "testing" ? <span className={`provider-test provider-test-${testState}`}><StatusDot tone={testState === "ready" ? "sage" : "warning"} />{testState === "ready" ? (language === "en" ? "Ready" : "可用") : (language === "en" ? "Unavailable" : "暂不可用")}</span> : null}</div>
        </li>;
      })}
    </ol>
    <p className={`market-config-notice ${status === "error" ? "is-error" : ""}`} role="status">{status === "saved" ? (language === "en" ? "Saved. New research calculations will use this order." : "已保存；新的研究计算将按此顺序请求。") : status === "error" ? (language === "en" ? "The settings request failed. Existing settings were kept." : "配置请求失败；原有设置保持不变。") : (language === "en" ? "Keys are encrypted on the server and are never returned to this page." : "密钥由服务端加密保存，本页面不会读取密钥原文。")}</p>
  </section>;
}
