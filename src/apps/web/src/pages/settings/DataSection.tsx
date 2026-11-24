import { useEffect, useId, useState } from "react";
import { ApiError } from "../../api/http";
import {
  marketUpdateFrom,
  testMarketProvider,
  useMarketDataConfig,
  useSaveMarketDataConfig,
  type EarningsProviderSummary,
  type MarketDataConfig,
  type MarketProviderId,
  type MarketProviderSummary,
} from "../../api/settings";
import { apiKeyProblem } from "../../lib/settings";
import { useLayout } from "../../ui/AppLayout";
import { Icon } from "../../ui/icons";
import { Panel } from "../../ui/Panel";
import { describeError } from "../research/errors";
import { TestStatus } from "./ModelSection";

type TestState = "ready" | "unavailable" | "testing" | "error";

const DEFAULT_FMP_SUMMARY: EarningsProviderSummary = {
  provider_id: "fmp",
  display_name: "Financial Modeling Prep (FMP)",
  enabled: true,
  requires_key: true,
  personal_key_configured: false,
  platform_available: false,
  platform_key_allowed: false,
  key_hint: null,
};

function dataErrorText(error: unknown): string {
  if (error instanceof ApiError && error.status === 503) return "密钥存储暂时不可用，请稍后重试。";
  return describeError(error);
}

function keyStatus(provider: MarketProviderSummary): string {
  if (!provider.requires_key) return "无需密钥";
  if (provider.personal_key_configured) return `我的密钥 ${provider.key_hint ?? ""}`.trim();
  if (provider.platform_available) return "使用平台密钥";
  return "未设置密钥，暂不可用";
}

function fmpKeyStatus(fmp: EarningsProviderSummary): string {
  if (fmp.personal_key_configured) return `我的 BYOK 密钥 ${fmp.key_hint ?? ""}`.trim();
  if (fmp.platform_available && fmp.platform_key_allowed) return "开发平台密钥可用（建议配置个人 BYOK）";
  return "未设置个人 BYOK 密钥（未配置时自动降级至 SEC 8-K 99.1 与用户上传资料）";
}

function usable(provider: MarketProviderSummary): boolean {
  return provider.enabled && (!provider.requires_key || provider.personal_key_configured || provider.platform_available);
}

function KeyPanel({ provider, config, onClose }: { provider: MarketProviderSummary; config: MarketDataConfig; onClose: () => void }) {
  const ids = useId();
  const save = useSaveMarketDataConfig();
  const [apiKey, setApiKey] = useState("");
  const [touched, setTouched] = useState(false);
  const problem = apiKeyProblem(apiKey, true);
  const isTushare = provider.provider_id === "tushare";
  const credentialLabel = isTushare ? "Token" : "API Key";
  const submit = () => {
    setTouched(true);
    if (problem || save.isPending) return;
    save.mutate(marketUpdateFrom(config, { key: { providerId: provider.provider_id, apiKey } }), { onSuccess: onClose });
  };
  const clear = () => save.mutate(marketUpdateFrom(config, { key: { providerId: provider.provider_id, clear: true } }), { onSuccess: onClose });
  return (
    <Panel
      title={`${provider.display_name} 的 ${credentialLabel}`}
      onClose={onClose}
      footerClassName="split"
      footer={
        <>
          {provider.personal_key_configured ? <button type="button" className="btn plain" onClick={clear} disabled={save.isPending}>删除我的密钥</button> : <span />}
          <span className="actions-row">
            <button type="button" className="btn plain" onClick={onClose}>取消</button>
            <button type="button" className="btn primary" onClick={submit} disabled={save.isPending}>{save.isPending ? "正在保存…" : "保存"}</button>
          </span>
        </>
      }
    >
      <form className="add-source" onSubmit={(event) => { event.preventDefault(); submit(); }} noValidate>
        <div className="field">
          <label htmlFor={`${ids}-key`}>{credentialLabel}</label>
          <input id={`${ids}-key`} type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} autoComplete="off" autoFocus
            placeholder={provider.personal_key_configured ? `已保存 ${provider.key_hint ?? ""} · 输入新值即可替换` : `输入 ${credentialLabel}`} aria-invalid={touched && problem ? true : undefined} />
          {touched && problem ? <span className="err">{problem}</span> : <span className="help">{isTushare ? "Token 加密保存，只用于向 Tushare 请求 A 股（上交所、深交所、北交所）日线行情和财务数据，仅限你本人的研究使用。" : `密钥加密保存，只用于向 ${provider.display_name} 请求历史行情。`}</span>}
        </div>
        {provider.platform_available ? <p className="note">删除你的密钥后，会改用平台提供的密钥。</p> : null}
        {save.isError ? <span className="inline-error" role="alert">{dataErrorText(save.error)}</span> : null}
        <button type="submit" hidden />
      </form>
    </Panel>
  );
}

function FmpKeyPanel({ fmp, config, onClose }: { fmp: EarningsProviderSummary; config: MarketDataConfig; onClose: () => void }) {
  const ids = useId();
  const save = useSaveMarketDataConfig();
  const [apiKey, setApiKey] = useState("");
  const [touched, setTouched] = useState(false);
  const problem = apiKeyProblem(apiKey, true);
  const submit = () => {
    setTouched(true);
    if (problem || save.isPending) return;
    save.mutate(marketUpdateFrom(config, { fmp: { enabled: true, apiKey } }), { onSuccess: onClose });
  };
  const clear = () => save.mutate(marketUpdateFrom(config, { fmp: { enabled: fmp.enabled, clear: true } }), { onSuccess: onClose });

  return (
    <Panel
      title="FMP（一致预期与电话会纪要）BYOK API Key"
      onClose={onClose}
      footerClassName="split"
      footer={
        <>
          {fmp.personal_key_configured ? <button type="button" className="btn plain" onClick={clear} disabled={save.isPending}>删除我的密钥</button> : <span />}
          <span className="actions-row">
            <button type="button" className="btn plain" onClick={onClose}>取消</button>
            <button type="button" className="btn primary" onClick={submit} disabled={save.isPending}>{save.isPending ? "正在保存…" : "保存"}</button>
          </span>
        </>
      }
    >
      <form className="add-source" onSubmit={(event) => { event.preventDefault(); submit(); }} noValidate>
        <div className="field">
          <label htmlFor={`${ids}-fmp-key`}>FMP BYOK API Key</label>
          <input
            id={`${ids}-fmp-key`}
            type="password"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            autoComplete="off"
            autoFocus
            placeholder={fmp.personal_key_configured ? `已保存 ${fmp.key_hint ?? ""} · 输入新值即可替换` : "输入 FMP API Key"}
            aria-invalid={touched && problem ? true : undefined}
          />
          {touched && problem ? (
            <span className="err">{problem}</span>
          ) : (
            <span className="help">密钥加密保存并仅显示掩码提示（••••last4），只用于你的账户获取一致预期与电话会纪要。</span>
          )}
        </div>
        <p className="note">
          BYOK 与合规策略：通过 FMP（user_byok）获取的数据默认设置 export=False，仅用于本人研究展示与分析输入，不会进入 Markdown / CSV / Excel 对外导出文件。
        </p>
        {save.isError ? <span className="inline-error" role="alert">{dataErrorText(save.error)}</span> : null}
        <button type="submit" hidden />
      </form>
    </Panel>
  );
}

export function DataSection() {
  const query = useMarketDataConfig();
  const save = useSaveMarketDataConfig();
  const { setPanelOpen } = useLayout();
  const [keyFor, setKeyFor] = useState<MarketProviderId | null>(null);
  const [fmpPanelOpen, setFmpPanelOpen] = useState(false);
  const [tests, setTests] = useState<Partial<Record<MarketProviderId, TestState>>>({});
  const config = query.data;

  useEffect(() => {
    setPanelOpen(keyFor !== null || fmpPanelOpen);
    return () => setPanelOpen(false);
  }, [keyFor, fmpPanelOpen, setPanelOpen]);

  const providers = config ? config.fallback_order.map((id) => config.providers.find((item) => item.provider_id === id)!).filter(Boolean) : [];
  const fmp = config ? (config.earnings_providers?.find((item) => item.provider_id === "fmp") ?? DEFAULT_FMP_SUMMARY) : undefined;

  const move = (index: number, delta: number) => {
    if (!config) return;
    const order = [...config.fallback_order];
    const target = index + delta;
    if (target < 0 || target >= order.length) return;
    [order[index], order[target]] = [order[target]!, order[index]!];
    save.mutate(marketUpdateFrom(config, { order }));
  };

  const toggle = (provider: MarketProviderSummary) => {
    if (!config) return;
    setTests((current) => ({ ...current, [provider.provider_id]: undefined }));
    save.mutate(marketUpdateFrom(config, { enabled: { [provider.provider_id]: !provider.enabled } }));
  };

  const toggleFmp = () => {
    if (!config || !fmp) return;
    save.mutate(marketUpdateFrom(config, { fmp: { enabled: !fmp.enabled } }));
  };

  const clearFmpKey = () => {
    if (!config || !fmp || save.isPending) return;
    save.mutate(marketUpdateFrom(config, { fmp: { enabled: fmp.enabled, clear: true } }));
  };

  const test = async (id: MarketProviderId) => {
    setTests((current) => ({ ...current, [id]: "testing" }));
    try {
      const result = await testMarketProvider(id);
      setTests((current) => ({ ...current, [id]: result.status }));
    } catch {
      setTests((current) => ({ ...current, [id]: "error" }));
    }
  };

  const openProviderPanel = (id: MarketProviderId) => {
    save.reset();
    setFmpPanelOpen(false);
    setKeyFor(id);
  };

  const openFmpPanel = () => {
    save.reset();
    setKeyFor(null);
    setFmpPanelOpen(true);
  };

  const keyProvider = keyFor && config ? config.providers.find((item) => item.provider_id === keyFor) : undefined;
  return (
    <>
      <section>
        <div className="gtitle">行情数据源</div>
        <div className="group-box">
          {query.isLoading ? <div className="prow"><div className="skeleton line grow" /></div> : null}
          {query.isError ? <div className="prow"><span className="meta">数据源设置暂时无法加载。</span></div> : null}
          {providers.map((provider, index) => (
            <div key={provider.provider_id} className={`prow provider${provider.enabled ? "" : " off"}`}>
              <span className="order" aria-hidden="true">{index + 1}</span>
              <div className="grow">
                <b>{provider.display_name}</b>
                <div className="meta">{keyStatus(provider)}{provider.enabled ? "" : " · 已停用"}</div>
              </div>
              <span className="prow-actions">
                <TestStatus state={tests[provider.provider_id] ?? null} />
                <button type="button" className="icon-btn" aria-label={`上移 ${provider.display_name}`} disabled={index === 0 || save.isPending} onClick={() => move(index, -1)}><Icon name="up" size={16} /></button>
                <button type="button" className="icon-btn" aria-label={`下移 ${provider.display_name}`} disabled={index === providers.length - 1 || save.isPending} onClick={() => move(index, 1)}><Icon name="down" size={16} /></button>
                <button type="button" className="btn" onClick={() => void test(provider.provider_id)} disabled={!usable(provider) || tests[provider.provider_id] === "testing"}>测试</button>
                {provider.requires_key ? (
                  <button type="button" className={`btn${!provider.personal_key_configured && !provider.platform_available ? " primary" : ""}`} onClick={() => openProviderPanel(provider.provider_id)}>
                    {provider.personal_key_configured ? "更换密钥" : "设置密钥"}
                  </button>
                ) : null}
              </span>
              <label className="switch" title={provider.enabled ? "停用" : "启用"}>
                <input type="checkbox" role="switch" checked={provider.enabled} disabled={save.isPending} onChange={() => toggle(provider)} aria-label={`启用 ${provider.display_name}`} />
                <span aria-hidden="true" />
              </label>
            </div>
          ))}
        </div>
        <p className="note inset">研究需要历史行情时，按上面的顺序依次尝试，前一个不可用时使用下一个。只在研究没有设置截止时间，并且开启了联网搜索和自动获取官方资料时使用。</p>
        <p className="note inset">Tushare Pro 用于 A 股的日线行情和财务报表，不参与上面的顺序；没有 Token 时，A 股会改用免费的 BaoStock（若已启用，仅提供净利润、主营收入和比率类指标）。</p>
      </section>

      <section aria-label="FMP（一致预期与电话会纪要）BYOK API Key">
        <div className="gtitle">财报增强数据源（可选）</div>
        <div className="group-box">
          {query.isLoading ? <div className="prow"><div className="skeleton line grow" /></div> : null}
          {query.isError ? <div className="prow"><span className="meta">财报数据源设置暂时无法加载。</span></div> : null}
          {fmp && config ? (
            <div className={`prow provider${fmp.enabled ? "" : " off"}`} data-provider="fmp">
              <div className="grow">
                <b>FMP（一致预期与电话会纪要）BYOK API Key</b>
                <div className="meta">
                  {fmpKeyStatus(fmp)}{fmp.enabled ? "" : " · 已停用"}
                </div>
              </div>
              <span className="prow-actions">
                {fmp.personal_key_configured ? (
                  <>
                    <button type="button" className="btn" aria-label="更换 FMP 密钥" onClick={openFmpPanel} disabled={save.isPending}>
                      更换密钥
                    </button>
                    <button type="button" className="btn plain" aria-label="清除 FMP 密钥" onClick={clearFmpKey} disabled={save.isPending}>
                      清除密钥
                    </button>
                  </>
                ) : (
                  <button type="button" className="btn" aria-label="设置 FMP 密钥" onClick={openFmpPanel} disabled={save.isPending}>
                    设置 FMP 密钥
                  </button>
                )}
              </span>
              <label className="switch" title={fmp.enabled ? "停用" : "启用"}>
                <input
                  type="checkbox"
                  role="switch"
                  checked={fmp.enabled}
                  disabled={save.isPending}
                  onChange={toggleFmp}
                  aria-label="启用 FMP（一致预期与电话会纪要）"
                />
                <span aria-hidden="true" />
              </label>
            </div>
          ) : null}
        </div>
        <p className="note inset">
          与 Twelve Data 类似，FMP 采用 BYOK（个人自带密钥）模式补充美股一致预期（Consensus）与电话会纪要（Transcript）。BYOK 与外部受限数据源默认设置 export=False，仅限本人研究展示与模型分析输入，不会进入 Markdown / CSV / Excel 对外导出文件；密钥加密保存并仅以掩码提示（••••last4）显示。
        </p>
        {save.isError && keyFor === null && !fmpPanelOpen ? <p className="inline-error" role="alert">{dataErrorText(save.error)}</p> : null}
      </section>

      {keyProvider && config ? <KeyPanel provider={keyProvider} config={config} onClose={() => setKeyFor(null)} /> : null}
      {fmpPanelOpen && fmp && config ? <FmpKeyPanel fmp={fmp} config={config} onClose={() => setFmpPanelOpen(false)} /> : null}
    </>
  );
}
