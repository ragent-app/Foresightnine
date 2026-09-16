import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Input, StatusDot } from "./Controls";
import type { OAuthSession } from "../oauth-client";
import {
  createMarketRequestGenerationGuard,
  fetchMarketDataConfig,
  isLoadedMarketConfigOwner,
  marketConfigSessionKey,
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

const KEY_HELP: Readonly<Record<MarketProviderId, readonly [string, string]>> = {
  market_data: ["在 Market Data 供应商账户中创建并复制 API Token。", "Create and copy an API token from your Market Data provider account."],
  alpha_vantage: ["填写 Alpha Vantage API Key；可在其账户或申请页面获取。", "Enter the Alpha Vantage API key from its account or key-request page."],
  twelve_data: ["填写 Twelve Data Dashboard 中显示的 API Key。", "Enter the API key shown in the Twelve Data dashboard."],
  finnhub: ["填写 Finnhub Dashboard 中显示的 API Token。", "Enter the API token shown in the Finnhub dashboard."],
  yahoo: ["该来源不需要用户密钥，可直接参与 fallback。", "No personal key is required; this source can participate directly in fallback."],
};

export function MarketDataConfigPanel({ apiBaseUrl, session, onSaved }: Props) {
  const { language } = useLanguage();
  const [config, setConfig] = useState<MarketDataConfig | null>(null);
  const [loadedSessionKey, setLoadedSessionKey] = useState<string | null>(null);
  const [order, setOrder] = useState<readonly MarketProviderId[]>([]);
  const [enabled, setEnabled] = useState<Readonly<Record<string, boolean>>>({});
  const [keys, setKeys] = useState<Readonly<Record<string, string>>>({});
  const [clearKeys, setClearKeys] = useState<Readonly<Record<string, boolean>>>({});
  const [status, setStatus] = useState<"idle" | "loading" | "saving" | "saved" | "error">("idle");
  const [tests, setTests] = useState<TestState>({});
  const [dragged, setDragged] = useState<MarketProviderId | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const requestGeneration = useRef(createMarketRequestGenerationGuard()).current;
  const mutationController = useRef<AbortController | null>(null);
  const sessionKey = session ? marketConfigSessionKey(session) : "anonymous";

  useEffect(() => {
    const generation = requestGeneration.next();
    mutationController.current?.abort();
    mutationController.current = null;
    setConfig(null); setLoadedSessionKey(null); setOrder([]); setEnabled({}); setKeys({}); setClearKeys({}); setTests({}); setDragged(null);
    if (!apiBaseUrl || !session) { setStatus("idle"); return; }
    const controller = new AbortController();
    setStatus("loading");
    void fetchMarketDataConfig(apiBaseUrl, session, controller.signal).then((next) => {
      if (controller.signal.aborted || !requestGeneration.isCurrent(generation)) return;
      setConfig(next);
      setLoadedSessionKey(sessionKey);
      setOrder(next.fallback_order);
      setEnabled(Object.fromEntries(next.providers.map((item) => [item.provider_id, item.enabled])));
      setKeys({}); setClearKeys({}); setStatus("idle");
    }).catch(() => { if (!controller.signal.aborted && requestGeneration.isCurrent(generation)) setStatus("error"); });
    return () => controller.abort();
  }, [apiBaseUrl, sessionKey, reloadKey]);

  const byId = useMemo(() => new Map(config?.providers.map((item) => [item.provider_id, item]) ?? []), [config]);
  const drafts = (): readonly MarketProviderDraft[] => order.map((providerId) => {
    const apiKey = keys[providerId]?.trim() ?? "";
    if (apiKey && apiKey.length < 8) throw new Error("Provider key is too short");
    return {
      provider_id: providerId,
      enabled: enabled[providerId] ?? true,
      ...(apiKey ? { api_key: apiKey } : {}),
      ...(clearKeys[providerId] ? { clear_api_key: true } : {}),
    };
  });
  const persist = async (controller: AbortController, generation: number, activeSession: OAuthSession): Promise<MarketDataConfig | null> => {
    if (!apiBaseUrl || !isLoadedMarketConfigOwner(loadedSessionKey, session) || marketConfigSessionKey(activeSession) !== sessionKey) throw new Error("Current account settings have not loaded");
    const next = await saveMarketDataConfig(apiBaseUrl, activeSession, order, drafts(), controller.signal);
    if (controller.signal.aborted || !requestGeneration.isCurrent(generation)) return null;
    setConfig(next); setOrder(next.fallback_order);
    setEnabled(Object.fromEntries(next.providers.map((item) => [item.provider_id, item.enabled])));
    setKeys({}); setClearKeys({}); onSaved?.();
    return next;
  };
  const save = (): void => {
    if (!session || !isLoadedMarketConfigOwner(loadedSessionKey, session) || status === "loading" || status === "saving") return;
    mutationController.current?.abort();
    const controller = new AbortController(); mutationController.current = controller;
    const generation = requestGeneration.next();
    setStatus("saving");
    void persist(controller, generation, session).then((next) => { if (next && requestGeneration.isCurrent(generation)) setStatus("saved"); }).catch(() => { if (!controller.signal.aborted && requestGeneration.isCurrent(generation)) setStatus("error"); });
  };
  const test = (providerId: MarketProviderId): void => {
    if (!apiBaseUrl || !session || !isLoadedMarketConfigOwner(loadedSessionKey, session) || status === "loading" || status === "saving") return;
    mutationController.current?.abort();
    const controller = new AbortController(); mutationController.current = controller;
    const generation = requestGeneration.next();
    setStatus("saving"); setTests((current) => ({ ...current, [providerId]: "testing" }));
    void persist(controller, generation, session).then((next) => next ? testMarketDataProvider(apiBaseUrl, session, providerId, controller.signal) : null).then((result) => {
      if (!result || controller.signal.aborted || !requestGeneration.isCurrent(generation)) return;
      setStatus("saved"); setTests((current) => ({ ...current, [providerId]: result }));
    }).catch(() => { if (!controller.signal.aborted && requestGeneration.isCurrent(generation)) { setStatus("error"); setTests((current) => ({ ...current, [providerId]: "unavailable" })); } });
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

  if (!session) return <section className="market-config-panel"><div><p className="eyebrow">{language === "en" ? "API / financial data configuration" : "API / 金融数据配置"}</p><h2>{language === "en" ? "Sign in to enter your provider keys" : "登录后自行填写金融数据 API Key"}</h2><p>{language === "en" ? "After sign-in, this area provides key inputs, provider enable switches, fallback ordering, save, replace, remove, and connection testing. The platform fallback remains available without browser-stored credentials." : "登录后，此处会显示 API Key 输入框、数据源启停、fallback 排序、保存、替换、移除和连接测试。未填写个人 Key 时仍可使用平台 fallback；浏览器不会保存或回显密钥。"}</p></div></section>;
  if (status === "error" && !config) return <section className="market-config-panel" role="alert"><p>{language === "en" ? "Market data settings could not be loaded. Existing server settings were not changed." : "金融数据源配置读取失败；服务端现有设置未被修改。"}</p><Button variant="secondary" type="button" onClick={() => setReloadKey((value) => value + 1)}>{language === "en" ? "Retry loading" : "重新读取"}</Button></section>;
  if (status === "loading" || !config || !isLoadedMarketConfigOwner(loadedSessionKey, session)) return <section className="market-config-panel" aria-busy="true"><StatusDot /> {language === "en" ? "Loading market data settings…" : "正在读取金融数据源配置…"}</section>;

  return <section className="market-config-panel" aria-labelledby="market-config-title">
    <div className="market-config-heading"><div><p className="eyebrow">{language === "en" ? "API / financial data configuration" : "API / 金融数据配置"}</p><h2 id="market-config-title">{language === "en" ? "Your market-data fallback chain" : "你的金融数据 fallback 链路"}</h2><p>{language === "en" ? "Enter keys obtained from your provider accounts. Your encrypted key overrides only the matching platform credential; drag or use arrows to set priority." : "填写你从金融数据供应商账户取得的 API Key。个人 Key 只覆盖同一数据源的平台凭据；拖动或使用箭头调整优先级。"}</p></div><Button variant="primary" type="button" disabled={status === "saving"} onClick={save}>{status === "saving" ? (language === "en" ? "Saving…" : "保存中…") : (language === "en" ? "Save API / sources" : "保存 API / 数据源")}</Button></div>
    <div className="market-config-steps" aria-label={language === "en" ? "Configuration steps" : "配置步骤"}>
      <div><strong>1</strong><span>{language === "en" ? "Get a key from the provider" : "从供应商获取 Key"}</span></div>
      <div><strong>2</strong><span>{language === "en" ? "Enter, enable, and order" : "填写、启用并排序"}</span></div>
      <div><strong>3</strong><span>{language === "en" ? "Save and test" : "保存并测试连接"}</span></div>
    </div>
    <ol className="provider-order">
      {order.map((providerId, index) => {
        const provider = byId.get(providerId); if (!provider) return null;
        const testState = tests[providerId];
        return <li key={providerId} draggable onDragStart={() => setDragged(providerId)} onDragOver={(event) => event.preventDefault()} onDrop={() => dropBefore(providerId)} className="provider-card">
          <div className="provider-rank" aria-hidden="true">{index + 1}</div>
          <div className="provider-main"><div className="provider-title"><strong>{provider.display_name}</strong><span>{provider.personal_key_configured && !clearKeys[providerId] ? provider.key_hint : provider.platform_available ? (language === "en" ? "Platform fallback" : "平台 fallback") : (language === "en" ? "Key required" : "需要 Key")}</span></div>
            <label className="provider-toggle"><input type="checkbox" checked={enabled[providerId] ?? true} onChange={(event) => setEnabled((current) => ({ ...current, [providerId]: event.target.checked }))} /> {language === "en" ? "Enabled" : "启用"}</label>
            {provider.requires_key ? <><div className="provider-key-row"><Input type="password" minLength={8} maxLength={512} autoComplete="off" spellCheck={false} name={`market-provider-${providerId}-api-key`} value={keys[providerId] ?? ""} onChange={(event) => { setKeys((current) => ({ ...current, [providerId]: event.target.value })); setClearKeys((current) => ({ ...current, [providerId]: false })); }} placeholder={provider.personal_key_configured ? (language === "en" ? "Leave blank to keep saved key" : "留空则保留已保存 Key") : (language === "en" ? "Enter your provider API key" : "填写你自己的数据源 API Key")} aria-label={`${provider.display_name} API key`} />{provider.personal_key_configured ? <Button variant="quiet" type="button" onClick={() => { setKeys((current) => ({ ...current, [providerId]: "" })); setClearKeys((current) => ({ ...current, [providerId]: true })); }}>{language === "en" ? "Remove key" : "移除 Key"}</Button> : null}</div><small className="provider-key-help">{KEY_HELP[providerId][language === "en" ? 1 : 0]}</small></> : <small className="provider-key-help">{KEY_HELP[providerId][language === "en" ? 1 : 0]}</small>}
          </div>
          <div className="provider-actions"><Button variant="quiet" type="button" disabled={index === 0} onClick={() => move(providerId, -1)} aria-label={`${provider.display_name} ${language === "en" ? "move up" : "上移"}`}>↑</Button><Button variant="quiet" type="button" disabled={index === order.length - 1} onClick={() => move(providerId, 1)} aria-label={`${provider.display_name} ${language === "en" ? "move down" : "下移"}`}>↓</Button><Button variant="secondary" type="button" disabled={status === "saving" || !(enabled[providerId] ?? true)} onClick={() => test(providerId)}>{testState === "testing" ? (language === "en" ? "Testing…" : "测试中…") : (language === "en" ? "Save & test" : "保存并测试")}</Button>{testState && testState !== "testing" ? <span className={`provider-test provider-test-${testState}`}><StatusDot tone={testState === "ready" ? "sage" : "warning"} />{testState === "ready" ? (language === "en" ? "Ready" : "可用") : (language === "en" ? "Unavailable" : "暂不可用")}</span> : null}</div>
        </li>;
      })}
    </ol>
    <p className={`market-config-notice ${status === "error" ? "is-error" : ""}`} role="status">{status === "saved" ? (language === "en" ? "Saved. New research calculations will use this order." : "已保存；新的研究计算将按此顺序请求。") : status === "error" ? (language === "en" ? "The settings request failed. Existing settings were kept." : "配置请求失败；原有设置保持不变。") : (language === "en" ? "Keys are encrypted on the server and are never returned to this page." : "密钥由服务端加密保存，本页面不会读取密钥原文。")}</p>
  </section>;
}
