import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useModelDiscovery } from "./useModelDiscovery";
import { ApiError } from "../../api/http";
import { testModelConfig, useAccountSummary, useModelConfig, useSaveModelConfig, type AccountSummary, type ChatGenerationParameters, type ConnectionStatus, type ModelConfig, type MessagesOptions, type ModelProtocol } from "../../api/settings";
import { apiKeyProblem, baseUrlProblem, hasSavedPersonal, keyRequired, modelNameProblem, PROTOCOL_LABEL } from "../../lib/settings";
import { ConfirmDialog } from "../../ui/ConfirmDialog";
import { Segmented } from "../../ui/controls";
import { useLayout } from "../../ui/AppLayout";
import { Panel } from "../../ui/Panel";
import { describeError } from "../research/errors";

type TestState = ConnectionStatus | "testing" | "error" | null;

/** The model platform research actually runs on; the billing label may name another model. */
function platformModel(summary: AccountSummary | undefined): string | null {
  const name = summary?.research_model ?? summary?.provider_model ?? null;
  return name && name !== "unknown" ? name : null;
}

function modelErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 503) return "密钥存储暂时不可用，请稍后重试。";
    if (error.status === 422 && /origin or protocol/i.test(error.message)) return "更换服务地址的域名或协议时，需要重新输入 API Key。";
    if (error.status === 422 && /incomplete/i.test(error.message)) return "配置不完整：服务地址、模型名称和 API Key 都需要填写。";
    if (error.status === 422 && error.details.length === 0) return "服务地址无法使用：只支持可以从公网访问的 HTTPS 地址（443 端口）。";
  }
  return describeError(error);
}

export function TestStatus({ state }: { state: TestState }) {
  if (state === null) return null;
  if (state === "testing") return <span className="role light neutral">正在测试…</span>;
  if (state === "ready") return <span className="role support">可用</span>;
  if (state === "unavailable") return <span className="role error">不可用</span>;
  return <span className="role error">测试失败</span>;
}

/** 我的模型 panel (spec §3.2). Saving switches the account to its own model. */
function MyModelPanel({ saved, onClose, onSaved }: { saved: ModelConfig | undefined; onClose: () => void; onSaved: (config: ModelConfig) => void }) {
  const ids = useId();
  const form = useRef<HTMLFormElement>(null);
  const save = useSaveModelConfig();
  // Prefill from the saved configuration whether or not it is active.
  const personal = hasSavedPersonal(saved) ? saved : undefined;
  const [protocol, setProtocol] = useState<ModelProtocol>(personal?.protocol ?? "openai_responses");
  const [baseUrl, setBaseUrl] = useState(personal?.base_url ?? "");
  const [model, setModel] = useState(personal?.model ?? "");
  const [version, setVersion] = useState(personal?.messages_options?.version ?? "");
  const [authScheme, setAuthScheme] = useState<MessagesOptions["auth_scheme"]>(personal?.messages_options?.auth_scheme ?? "x-api-key");
  const [maxTokens, setMaxTokens] = useState(String(personal?.messages_options?.max_tokens ?? ""));
  const numericParams = protocol === "openai_responses" ? ["temperature", "top_p"] as const : ["temperature", "top_p", "seed"] as const;
  const messages = protocol === "anthropic_messages";
  const sameConnection = personal && protocol === personal.protocol && baseUrl === personal.base_url && model === personal.model;
  const savedParameters = sameConnection ? personal.generation_parameters : undefined;
  const validOptions = /^[!-~]{1,64}$/.test(version) && Number.isSafeInteger(Number(maxTokens)) && Number(maxTokens) > 0;
  const [apiKey, setApiKey] = useState("");
  const [touched, setTouched] = useState(false);
  const needsKey = keyRequired(saved, protocol, baseUrl);
  const problems = { baseUrl: baseUrlProblem(baseUrl), model: modelNameProblem(model), apiKey: apiKeyProblem(apiKey, needsKey) };
  const catalogAuth = messages ? authScheme : undefined;
  const catalogVersion = messages ? version : undefined;
  const canDiscover = !problems.baseUrl && !problems.apiKey && (apiKey !== "" || personal?.base_url === baseUrl)
    && (!messages || /^[!-~]{1,64}$/.test(version));
  const discoveryRequest = useMemo(() => canDiscover ? {
    protocol, base_url: baseUrl, ...(apiKey ? { api_key: apiKey } : {}),
    ...(catalogAuth && catalogVersion ? { auth_scheme: catalogAuth, version: catalogVersion } : {}),
  } : null, [apiKey, baseUrl, canDiscover, catalogAuth, catalogVersion, protocol]);
  const discovery = useModelDiscovery(discoveryRequest, model);
  const valid = !problems.baseUrl && !problems.model && !problems.apiKey && (!messages || validOptions);
  const show = (problem: string | null) => (touched ? problem : null);

  const submit = () => {
    setTouched(true);
    if (!valid || save.isPending || !form.current || !form.current.reportValidity()) return;
    const parameters: ChatGenerationParameters = {};
    const fields = new FormData(form.current);
    const effort = fields.get("effort");
    if (!messages) {
      if (typeof effort === "string" && effort) parameters.reasoning_effort = effort;
      for (const name of numericParams) {
        const value = fields.get(name);
        if (typeof value === "string" && value) parameters[name] = Number(value);
      }
    }
    save.mutate(
      { mode: "personal", protocol, base_url: baseUrl, model,
        ...(!messages && (savedParameters || Object.keys(parameters).length) ? { generation_parameters: parameters } : {}),
        ...(messages ? { messages_options: { version, auth_scheme: authScheme, max_tokens: Number(maxTokens), ...(typeof effort === "string" && effort ? { effort } : {}) } } : {}), ...(apiKey ? { api_key: apiKey } : {}) },
      { onSuccess: (config) => { onSaved(config); onClose(); } },
    );
  };

  return (
    <Panel
      title="我的模型"
      onClose={onClose}
      footer={<><button type="button" className="btn plain" onClick={onClose}>取消</button><button type="button" className="btn primary" onClick={submit} disabled={save.isPending || (touched && !valid)}>{save.isPending ? "正在保存…" : "保存"}</button></>}
    >
      <form ref={form} className="add-source" onSubmit={(event) => { event.preventDefault(); submit(); }} noValidate>
        <div className="field">
          <label htmlFor={`${ids}-protocol`}>协议</label>
          <select id={`${ids}-protocol`} value={protocol} onChange={(event) => { setProtocol(event.target.value as ModelProtocol); setApiKey(""); }}>
            {(Object.keys(PROTOCOL_LABEL) as ModelProtocol[]).map((value) => <option key={value} value={value}>{PROTOCOL_LABEL[value]}</option>)}
          </select>
          <span className="help">按服务实际支持的接口选择。保存后将用一条测试消息验证研究工具调用，服务商可能计费。</span>
        </div>
        <div className="field">
          <label htmlFor={`${ids}-url`}>服务地址</label>
          <input id={`${ids}-url`} type="url" inputMode="url" value={baseUrl} onChange={(event) => { if (baseUrl && !baseUrlProblem(baseUrl)) setApiKey(""); setBaseUrl(event.target.value); }} placeholder="https://api.example.com/v1"
            autoCapitalize="off" spellCheck={false} aria-invalid={show(problems.baseUrl) ? true : undefined} />
          {show(problems.baseUrl) ? <span className="err">{problems.baseUrl}</span> : <span className="help">只支持可以从公网访问的 HTTPS 地址（443 端口）。</span>}
        </div>
        <div className="field">
          <label htmlFor={`${ids}-key`}>API Key</label>
          <input id={`${ids}-key`} type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} autoComplete="off"
            placeholder={personal?.key_hint && !needsKey ? `已保存 ${personal.key_hint} · 输入新值即可替换` : "输入 API Key"} aria-invalid={show(problems.apiKey) ? true : undefined} />
          {show(problems.apiKey) ? <span className="err">{problems.apiKey}</span> : (
            <span className="help">{personal?.key_hint && needsKey ? "更换服务地址的域名或协议后，需要重新输入 API Key。" : "密钥加密保存，只用于调用你配置的模型服务。"}</span>
          )}
        </div>
        <div className="field">
          <label htmlFor={`${ids}-model`}>模型名称</label>
          <input id={`${ids}-model`} list={`${ids}-models`} value={model} onChange={(event) => setModel(event.target.value)} placeholder="服务商提供的模型 ID" autoCapitalize="off" spellCheck={false}
            aria-invalid={show(problems.model) ? true : undefined} />
          <datalist id={`${ids}-models`}>{discovery.models.map((item) => <option key={item.id} value={item.id} />)}</datalist>
          {discovery.message && <span className="help" role="status">{discovery.message}</span>}
          {show(problems.model) ? <span className="err">{problems.model}</span> : null}
        </div>
        <div className="field" key={JSON.stringify([protocol, baseUrl, model])}>
          <label htmlFor={`${ids}-effort`}>推理强度（effort）</label>
          <input id={`${ids}-effort`} name="effort" list={`${ids}-efforts`} placeholder="服务商默认"
            defaultValue={messages && sameConnection ? personal.messages_options?.effort : savedParameters?.reasoning_effort}
            maxLength={64} pattern="[a-zA-Z0-9_\-]{1,64}" spellCheck={false} />
          <datalist id={`${ids}-efforts`}>
            {(discovery.efforts ?? (messages ? ["low", "medium", "high", "max"] : ["none", "minimal", "low", "medium", "high", "xhigh"])).map((level) => <option key={level} value={level} />)}
          </datalist>
          <span className="help">{discovery.efforts?.length ? `${discovery.source === "official_docs" ? "官方模型文档" : "模型接口"}列出：${discovery.efforts.join("、")}。当前协议的工具兼容性以验证结果为准。`
            : discovery.efforts ? "服务商未列出可用档位。留空使用默认值。" : "可手动设置或留空。通用候选值需由当前模型验证。"}</span>
        </div>
        {messages ? <>
          <div className="field">
            <label htmlFor={`${ids}-version`}>Messages 协议版本</label>
            <input id={`${ids}-version`} value={version} onChange={(event) => setVersion(event.target.value)} placeholder="服务商要求的 anthropic-version" />
          </div>
          <div className="field">
            <label htmlFor={`${ids}-auth`}>认证方式</label>
            <select id={`${ids}-auth`} value={authScheme} onChange={(event) => setAuthScheme(event.target.value as MessagesOptions["auth_scheme"])}>
              <option value="x-api-key">x-api-key</option><option value="bearer">Bearer</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor={`${ids}-max`}>每次请求的最大输出 Token</label>
            <input id={`${ids}-max`} type="number" min="1" step="1" value={maxTokens} onChange={(event) => setMaxTokens(event.target.value)} />
          </div>
          {touched && !validOptions ? <span className="err">请填写协议版本和大于零的整数输出上限。</span> : null}
        </> : <details key={JSON.stringify([baseUrl, model])}>
          <summary>生成参数（可选）</summary>
          <p className="help">只填写当前模型支持的值。留空时由服务商决定。</p>
          {numericParams.map((name) => (
            <div className="field" key={name}>
              <label htmlFor={`${ids}-${name}`}>{name}</label>
              <input id={`${ids}-${name}`} name={name} type="number" step={name === "seed" ? "1" : "any"}
                defaultValue={savedParameters?.[name]} />
            </div>
          ))}
        </details>}
        {save.isError ? <span className="inline-error" role="alert">{modelErrorText(save.error)}</span> : null}
        <button type="submit" hidden />
      </form>
    </Panel>
  );
}

export function ModelSection() {
  const config = useModelConfig();
  const account = useAccountSummary();
  const save = useSaveModelConfig();
  const { setPanelOpen } = useLayout();
  const [panelOpen, setOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [lastTest, setTest] = useState<{ revision: string | undefined; state: Exclude<TestState, null>; detail?: string | undefined } | null>(null);
  const testSequence = useRef(0);
  const data = config.data;
  const test = lastTest && lastTest.revision === data?.updated_at ? lastTest.state : null;
  const needsMessagesOptions = data?.protocol === "anthropic_messages" && !data.messages_options;
  let toolStatus = data?.research_tools_verified_at ? "研究工具已验证" : "研究工具待验证";
  if (needsMessagesOptions) toolStatus = "请编辑并填写 Messages 协议参数";
  else if (test === "ready") toolStatus = "研究工具已验证";
  else if (test === "testing") toolStatus = "正在验证…";
  else if (test === "unavailable" || test === "error") toolStatus = lastTest?.detail ?? "本次工具验证未通过";

  useEffect(() => {
    setPanelOpen(panelOpen);
    return () => setPanelOpen(false);
  }, [panelOpen, setPanelOpen]);

  const runTest = async (selected: ModelConfig) => {
    if (selected.protocol === "anthropic_messages" && !selected.messages_options) return;
    const revision = selected.updated_at;
    const sequence = ++testSequence.current;
    setTest({ revision, state: "testing" });
    try {
      const { status, upstream_status, parameter } = await testModelConfig();
      if (sequence === testSequence.current) {
        const detail = upstream_status
          ? `模型服务返回 HTTP ${upstream_status}${parameter ? `，请检查参数 ${parameter}` : ""}。`
          : undefined;
        setTest({ revision, state: status, detail });
        void config.refetch();
      }
    } catch (error) {
      if (sequence === testSequence.current) setTest({ revision, state: "error", detail: describeError(error) });
    }
  };

  const personal = data?.mode === "personal";
  const saved = hasSavedPersonal(data);
  const switchTo = (mode: "platform" | "personal") => {
    if (!data || mode === data.mode || save.isPending) return;
    testSequence.current++;
    setTest(null);
    if (mode === "platform") {
      save.mutate({ mode: "platform" });
    } else if (saved) {
      // Reuse the saved configuration and key; nothing needs to be re-entered.
      save.mutate({ mode: "personal" }, { onSuccess: (next) => { if (!next.research_tools_verified_at) void runTest(next); } });
    } else {
      setOpen(true);
    }
  };

  return (
    <section>
      <div className="gtitle">模型</div>
      <div className="group-box">
        <div className="prow">
          <div className="grow"><b>模型来源</b><div className="meta">平台模型按账户额度计费；我的模型使用你自己的服务和密钥。</div></div>
          <Segmented
            inline
            label="模型来源"
            options={[{ value: "platform", label: "平台模型" }, { value: "personal", label: "我的模型" }]}
            value={data?.mode ?? "platform"}
            onChange={switchTo}
          />
        </div>
        {config.isLoading ? <div className="prow"><div className="skeleton line grow" /></div> : null}
        {config.isError ? <div className="prow"><span className="meta">模型配置暂时无法加载。</span></div> : null}
        {data && !personal ? (
          <div className="prow">
            <div className="grow"><b>平台模型</b><div className="meta">{platformModel(account.data) ? `研究使用 ${platformModel(account.data)} · ` : ""}按本期额度计费</div></div>
            <span className="role light support">使用中</span>
          </div>
        ) : null}
        {data && saved ? (
          <div className="prow">
            <div className="grow">
              <b>{PROTOCOL_LABEL[data.protocol ?? "openai_chat_completions"]} · {data.model}</b>
              <div className="meta">{data.base_url} · 密钥 {data.key_hint}{personal ? "" : " · 已保存，未使用"}</div>
            </div>
            {personal ? <span className="meta" role="status">{toolStatus}</span> : null}
            {personal && !needsMessagesOptions ? <button type="button" className="btn" onClick={() => void runTest(data)} disabled={test === "testing"}>验证研究工具</button> : null}
            <button type="button" className="btn" onClick={() => setOpen(true)}>编辑</button>
          </div>
        ) : null}
        {data && saved ? (
          <div className="prow">
            <div className="grow"><b>删除我的模型配置</b><div className="meta">删除保存的服务地址、模型名称和 API Key{personal ? "，并改用平台模型" : ""}。</div></div>
            <button type="button" className="btn danger" onClick={() => setConfirmDelete(true)}>删除</button>
          </div>
        ) : null}
      </div>
      <p className="note inset">
        密钥加密保存，只用于调用你配置的模型服务，不会出现在研究记录或导出中。切换到平台模型时会保留你的配置，切回时不用重新填写。
      </p>
      {save.isError && !confirmDelete ? <p className="inline-error" role="alert">{modelErrorText(save.error)}</p> : null}
      {panelOpen ? (
        <MyModelPanel saved={data} onClose={() => setOpen(false)} onSaved={(next) => { testSequence.current++; setTest(null); if (!next.research_tools_verified_at) void runTest(next); }} />
      ) : null}
      {confirmDelete ? (
        <ConfirmDialog
          title="删除我的模型配置？"
          description={`会删除保存的服务地址、模型名称和 API Key${personal ? "，并改用平台模型" : ""}。以后再用自己的模型时需要重新填写。`}
          onCancel={() => setConfirmDelete(false)}
          actions={[{
            key: "delete", label: save.isPending ? "正在删除…" : "删除", tone: "danger", disabled: save.isPending,
            onSelect: () => save.mutate("delete", { onSuccess: () => { testSequence.current++; setConfirmDelete(false); setTest(null); } }),
          }]}
          error={save.isError ? modelErrorText(save.error) : null}
        />
      ) : null}
    </section>
  );
}
