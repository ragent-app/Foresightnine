import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useModelDiscovery } from "./useModelDiscovery";
import { ApiError } from "../../api/http";
import {
  testModelConfig,
  useAccountSummary,
  useModelConfig,
  useSaveModelConfig,
  type AccountSummary,
  type ChatGenerationParameters,
  type ConnectionStatus,
  type MessagesOptions,
  type ModelConfig,
  type ModelProtocol,
  type ModelSlot,
  type ModelSlotBinding,
  type ModelSlotConfig,
} from "../../api/settings";
import { apiKeyProblem, baseUrlProblem, hasSavedPersonal, keyRequired, modelNameProblem, PROTOCOL_LABEL } from "../../lib/settings";
import { ConfirmDialog } from "../../ui/ConfirmDialog";
import { Segmented } from "../../ui/controls";
import { useLayout } from "../../ui/AppLayout";
import { Panel } from "../../ui/Panel";
import { describeError } from "../research/errors";

type TestState = ConnectionStatus | "testing" | "error" | null;

const RAW_SECRET_PREFIX_RE = /^(?:bearer\s+|basic\s+|apikey\s+|sk-[a-z0-9_-]{8,}|gh[po]_[a-z0-9]{8,}|akia[0-9a-z]{8,}|aiza[0-9a-z_-]{8,}|xox[bp]-[a-z0-9-]{8,})/i;
const SAFE_CREDENTIAL_REF_RE = /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/;

const SLOT_META: ReadonlyArray<{
  slot: ModelSlot;
  role: string;
  note: string;
}> = [
  {
    slot: "fast_extractor",
    role: "取证执行 Worker A/B/C，需支持原生工具调用",
    note: "负责并行只读拆表、附注核查与电话会取证，必须支持原生工具调用。",
  },
  {
    slot: "deep_reasoner",
    role: "Lead 规划与交叉补证调度",
    note: "负责 Stage 1 任务派发与 Stage 2.5 跨 Worker 交叉补证调度。",
  },
  {
    slot: "bear_reviewer",
    role: "独立空头对抗审查，支持跨家族模型互审",
    note: "隔离多头叙事并输出证据锚定反证；配置不同模型/协议可启用跨家族互审。",
  },
  {
    slot: "writer",
    role: "Memo / Deep Report 底稿撰稿",
    note: "基于已锁定证据底稿渲染买方备忘录（Memo）或完整深研报告。",
  },
];

function credentialRefProblem(raw: string): string | null {
  if (!raw) return null;
  if (raw !== raw.trim()) return "凭据引用前后不能有空格";
  if (RAW_SECRET_PREFIX_RE.test(raw)) return "切勿填写明文 API Key 或令牌，请仅填非密钥引用标识";
  if (!SAFE_CREDENTIAL_REF_RE.test(raw)) return "凭据引用格式无效，仅允许字母、数字及 _ . : / -";
  return null;
}

function slotModelProblem(raw: string): string | null {
  const base = modelNameProblem(raw);
  if (base) return base;
  if (RAW_SECRET_PREFIX_RE.test(raw)) return "模型名称不能包含 API Key 或令牌";
  return null;
}

function compactSlots(slots: ModelSlotConfig | null | undefined): ModelSlotConfig {
  const result: ModelSlotConfig = {};
  if (!slots) return result;
  for (const { slot } of SLOT_META) {
    const binding = slots[slot];
    if (binding) result[slot] = binding;
  }
  return result;
}

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
    if (error.status === 422 && /raw credentials|credential_ref/i.test(error.message)) return "槽位配置不能包含明文 API Key，请只填协议、服务地址、模型名称或非密钥引用。";
    if (error.status === 422 && /public https/i.test(error.message)) return "槽位服务地址只支持公网 HTTPS 地址（443 端口）。";
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

function SlotPanel({
  slot,
  saved,
  onClose,
  onSaveBinding,
  onClearSlot,
  isPending,
  error,
}: {
  slot: ModelSlot;
  saved: ModelConfig;
  onClose: () => void;
  onSaveBinding: (slot: ModelSlot, binding: ModelSlotBinding) => void;
  onClearSlot: (slot: ModelSlot) => void;
  isPending: boolean;
  error: unknown;
}) {
  const ids = useId();
  const meta = SLOT_META.find((item) => item.slot === slot)!;
  const existing = saved.model_slots?.[slot] ?? undefined;
  const [protocol, setProtocol] = useState<ModelProtocol>(existing?.protocol ?? saved.protocol ?? "openai_chat_completions");
  const [baseUrl, setBaseUrl] = useState(existing?.base_url ?? "");
  const [model, setModel] = useState(existing?.model ?? "");
  const [credentialRef, setCredentialRef] = useState(existing?.credential_ref ?? "");
  const [toolsVerified, setToolsVerified] = useState(Boolean(existing?.tools_verified));
  const [endpointVersion, setEndpointVersion] = useState(existing?.endpoint_version ?? "");
  const [authScheme, setAuthScheme] = useState<"" | MessagesOptions["auth_scheme"]>(existing?.messages_auth_scheme ?? "");
  const [maxTokens, setMaxTokens] = useState(existing?.messages_max_tokens ? String(existing.messages_max_tokens) : "");
  const [effort, setEffort] = useState(existing?.messages_effort ?? "");
  const [touched, setTouched] = useState(false);

  const isMessages = protocol === "anthropic_messages";
  const isFastExtractor = slot === "fast_extractor";
  const maxTokensNum = Number(maxTokens);
  const maxTokensProblem = isMessages && maxTokens !== "" && (!Number.isSafeInteger(maxTokensNum) || maxTokensNum <= 0)
    ? "最大输出 Token 必须为大于 0 的整数"
    : null;
  const effortProblem = isMessages && effort !== "" && !/^[a-zA-Z0-9_-]{1,64}$/.test(effort)
    ? "推理强度只能包含字母、数字、下划线或连字符"
    : null;
  const problems = {
    baseUrl: baseUrlProblem(baseUrl),
    model: slotModelProblem(model),
    credentialRef: credentialRefProblem(credentialRef),
    maxTokens: maxTokensProblem,
    effort: effortProblem,
  };
  const valid = !problems.baseUrl && !problems.model && !problems.credentialRef && !problems.maxTokens && !problems.effort;
  const show = (problem: string | null) => (touched ? problem : null);

  const submit = () => {
    setTouched(true);
    if (!valid || isPending) return;
    const binding: ModelSlotBinding = {
      protocol,
      base_url: baseUrl,
      model,
      ...(credentialRef ? { credential_ref: credentialRef } : {}),
      ...(isFastExtractor && toolsVerified ? { tools_verified: true } : {}),
      ...(isMessages && endpointVersion.trim() ? { endpoint_version: endpointVersion.trim() } : {}),
      ...(isMessages && authScheme ? { messages_auth_scheme: authScheme } : {}),
      ...(isMessages && maxTokens ? { messages_max_tokens: maxTokensNum } : {}),
      ...(isMessages && effort ? { messages_effort: effort } : {}),
    };
    onSaveBinding(slot, binding);
  };

  return (
    <Panel
      title={`配置槽位 · ${slot}`}
      onClose={onClose}
      {...(existing ? { footerClassName: "split" } : {})}
      footer={
        <>
          {existing ? (
            <button type="button" className="btn plain" onClick={() => onClearSlot(slot)} disabled={isPending}>
              清除此槽位
            </button>
          ) : null}
          <span className="actions-row">
            <button type="button" className="btn plain" onClick={onClose}>取消</button>
            <button type="button" className="btn primary" onClick={submit} disabled={isPending || (touched && !valid)}>
              {isPending ? "正在保存…" : "保存槽位"}
            </button>
          </span>
        </>
      }
    >
      <form className="add-source" onSubmit={(event) => { event.preventDefault(); submit(); }} noValidate>
        <p className="note">{meta.role}。{meta.note}</p>
        <div className="field">
          <label htmlFor={`${ids}-protocol`}>协议</label>
          <select id={`${ids}-protocol`} value={protocol} onChange={(event) => setProtocol(event.target.value as ModelProtocol)}>
            {(Object.keys(PROTOCOL_LABEL) as ModelProtocol[]).map((value) => (
              <option key={value} value={value}>{PROTOCOL_LABEL[value]}</option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor={`${ids}-url`}>服务地址</label>
          <input
            id={`${ids}-url`}
            type="url"
            inputMode="url"
            value={baseUrl}
            onChange={(event) => setBaseUrl(event.target.value)}
            placeholder="https://api.example.com/v1"
            autoCapitalize="off"
            spellCheck={false}
            aria-invalid={show(problems.baseUrl) ? true : undefined}
          />
          {show(problems.baseUrl) ? <span className="err">{problems.baseUrl}</span> : <span className="help">只支持可以从公网访问的 HTTPS 地址（443 端口）。</span>}
        </div>
        <div className="field">
          <label htmlFor={`${ids}-model`}>模型名称</label>
          <input
            id={`${ids}-model`}
            value={model}
            onChange={(event) => setModel(event.target.value)}
            placeholder="服务商提供的模型 ID"
            autoCapitalize="off"
            spellCheck={false}
            aria-invalid={show(problems.model) ? true : undefined}
          />
          {show(problems.model) ? <span className="err">{problems.model}</span> : null}
        </div>
        <div className="field">
          <label htmlFor={`${ids}-cred-ref`}>非密钥凭据引用（可选）</label>
          <input
            id={`${ids}-cred-ref`}
            value={credentialRef}
            onChange={(event) => setCredentialRef(event.target.value)}
            placeholder="例如 account_primary 或 env:SLOT_KEY"
            autoCapitalize="off"
            autoComplete="off"
            spellCheck={false}
            aria-invalid={show(problems.credentialRef) ? true : undefined}
          />
          {show(problems.credentialRef) ? (
            <span className="err">{problems.credentialRef}</span>
          ) : (
            <span className="help">仅填非密钥引用标识（同域服务地址留空即可复用主模型凭据），切勿填写明文 API Key。</span>
          )}
        </div>
        {isFastExtractor ? (
          <div className="field">
            <label className="check-row" htmlFor={`${ids}-tools-verified`}>
              <input
                id={`${ids}-tools-verified`}
                type="checkbox"
                checked={toolsVerified}
                onChange={(event) => setToolsVerified(event.target.checked)}
              />
              <span className="grow">
                <span>已确认支持原生工具调用（tools_verified）</span>
                <span className="meta">Worker A/B/C 需要原生工具调用；未通过部署端能力表匹配时勾选此项。</span>
              </span>
            </label>
          </div>
        ) : null}
        {isMessages ? (
          <details className="more">
            <summary>Anthropic Messages 参数（可选）</summary>
            <div className="inner">
              <div className="field">
                <label htmlFor={`${ids}-version`}>Messages 协议版本</label>
                <input id={`${ids}-version`} value={endpointVersion} onChange={(event) => setEndpointVersion(event.target.value)} placeholder="2023-06-01" />
              </div>
              <div className="field">
                <label htmlFor={`${ids}-auth`}>认证方式</label>
                <select id={`${ids}-auth`} value={authScheme} onChange={(event) => setAuthScheme(event.target.value as "" | MessagesOptions["auth_scheme"])}>
                  <option value="">默认（x-api-key）</option>
                  <option value="x-api-key">x-api-key</option>
                  <option value="bearer">Bearer</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor={`${ids}-max-tokens`}>每次请求的最大输出 Token</label>
                <input
                  id={`${ids}-max-tokens`}
                  type="number"
                  min="1"
                  step="1"
                  value={maxTokens}
                  onChange={(event) => setMaxTokens(event.target.value)}
                  placeholder="4096"
                  aria-invalid={show(problems.maxTokens) ? true : undefined}
                />
                {show(problems.maxTokens) ? <span className="err">{problems.maxTokens}</span> : null}
              </div>
              <div className="field">
                <label htmlFor={`${ids}-effort`}>推理强度（effort）</label>
                <input
                  id={`${ids}-effort`}
                  value={effort}
                  onChange={(event) => setEffort(event.target.value)}
                  placeholder="例如 high"
                  aria-invalid={show(problems.effort) ? true : undefined}
                />
                {show(problems.effort) ? <span className="err">{problems.effort}</span> : null}
              </div>
            </div>
          </details>
        ) : null}
        {error ? <span className="inline-error" role="alert">{modelErrorText(error)}</span> : null}
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
  const [editingSlot, setEditingSlot] = useState<ModelSlot | null>(null);
  const [slotsOpen, setSlotsOpen] = useState(true);
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
    setPanelOpen(panelOpen || editingSlot !== null);
    return () => setPanelOpen(false);
  }, [panelOpen, editingSlot, setPanelOpen]);

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
  const activePrimaryModel = personal ? (data?.model ?? null) : platformModel(account.data);
  const configuredSlots = compactSlots(data?.model_slots);
  const configuredSlotCount = Object.keys(configuredSlots).length;
  const slotSummary = data?.model_slots_summary;

  const openPrimaryPanel = () => {
    save.reset();
    setEditingSlot(null);
    setOpen(true);
  };

  const openSlotPanel = (slot: ModelSlot) => {
    save.reset();
    setOpen(false);
    setEditingSlot(slot);
  };

  const saveSingleSlot = (slot: ModelSlot, binding: ModelSlotBinding) => {
    if (!data || save.isPending) return;
    const nextSlots: ModelSlotConfig = { ...configuredSlots, [slot]: binding };
    save.mutate(
      { mode: data.mode, model_slots: nextSlots },
      { onSuccess: () => setEditingSlot(null) },
    );
  };

  const clearSingleSlot = (slot: ModelSlot) => {
    if (!data || save.isPending) return;
    const nextSlots: ModelSlotConfig = { ...configuredSlots };
    delete nextSlots[slot];
    const hasRemaining = Object.keys(nextSlots).length > 0;
    save.mutate(
      hasRemaining ? { mode: data.mode, model_slots: nextSlots } : { mode: data.mode, clear_model_slots: true },
      { onSuccess: () => { if (editingSlot === slot) setEditingSlot(null); } },
    );
  };

  const clearAllSlots = () => {
    if (!data || save.isPending || configuredSlotCount === 0) return;
    save.mutate(
      { mode: data.mode, clear_model_slots: true },
      { onSuccess: () => setEditingSlot(null) },
    );
  };

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
      openPrimaryPanel();
    }
  };

  return (
    <>
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
              <button type="button" className="btn" onClick={openPrimaryPanel}>编辑</button>
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
        {save.isError && !confirmDelete && !panelOpen && editingSlot === null ? (
          <p className="inline-error" role="alert">{modelErrorText(save.error)}</p>
        ) : null}
      </section>

      <section aria-label="买方投研小组模型槽位（可选）">
        <div className="gtitle">
          <span>买方投研小组模型槽位（可选）</span>
          <span className="grow" />
          <button
            type="button"
            className="link"
            aria-expanded={slotsOpen}
            onClick={() => setSlotsOpen((prev) => !prev)}
          >
            {slotsOpen ? "收起" : "展开"}
          </button>
        </div>
        <div className="group-box">
          {config.isLoading ? <div className="prow"><div className="skeleton line grow" /></div> : null}
          {config.isError ? <div className="prow"><span className="meta">槽位配置暂时无法加载。</span></div> : null}
          {data && !slotsOpen ? (
            <div className="prow">
              <div className="grow">
                <b>{configuredSlotCount > 0 ? `已覆盖 ${configuredSlotCount} / 4 个槽位` : "默认回落主研究模型"}</b>
                <div className="meta">
                  {configuredSlotCount > 0
                    ? `未配置的槽位默认回落主研究模型${activePrimaryModel ? `（${activePrimaryModel}）` : ""}`
                    : `4 个逻辑槽位均默认回落主研究模型${activePrimaryModel ? `（${activePrimaryModel}）` : ""}`}
                </div>
              </div>
              {slotSummary?.cross_family_bear_review ? <span className="role light support">跨家族互审已启用</span> : null}
            </div>
          ) : null}
          {data && slotsOpen ? (
            <>
              {SLOT_META.map(({ slot, role }) => {
                const binding = configuredSlots[slot];
                const itemSummary = slotSummary?.slots?.[slot];
                const isBearCrossFamily = slot === "bear_reviewer" && Boolean(slotSummary?.cross_family_bear_review);
                const fallbackHint = itemSummary?.model ?? activePrimaryModel;
                const validationErrors = itemSummary?.validation_errors ?? [];
                return (
                  <div key={slot} className="prow" data-slot={slot}>
                    <div className="grow">
                      <div>
                        <b>{slot}</b>
                        <span className="meta"> · {role}</span>
                      </div>
                      {binding ? (
                        <>
                          <div className="meta">
                            {PROTOCOL_LABEL[binding.protocol ?? "openai_chat_completions"]} · {binding.model ?? binding.deployment_ref}
                          </div>
                          <div className="meta">
                            {binding.base_url}
                            {binding.credential_ref ? ` · 引用 ${binding.credential_ref}` : ""}
                            {binding.tools_verified ? " · 已确认原生工具" : ""}
                          </div>
                        </>
                      ) : (
                        <div className="meta">
                          默认回落主研究模型{fallbackHint ? `（${fallbackHint}）` : ""}
                        </div>
                      )}
                      {validationErrors.length > 0 ? (
                        <div className="inline-error" role="alert">{validationErrors.join(" ")}</div>
                      ) : null}
                    </div>
                    <span className="prow-actions">
                      {isBearCrossFamily ? <span className="role light support">跨家族互审</span> : null}
                      {binding ? (
                        <>
                          <button
                            type="button"
                            className="btn"
                            aria-label={`修改 ${slot}`}
                            onClick={() => openSlotPanel(slot)}
                            disabled={save.isPending}
                          >
                            修改
                          </button>
                          <button
                            type="button"
                            className="btn plain"
                            aria-label={`清除 ${slot}`}
                            onClick={() => clearSingleSlot(slot)}
                            disabled={save.isPending}
                          >
                            清除
                          </button>
                        </>
                      ) : (
                        <>
                          <span className="role light neutral">默认回落</span>
                          <button
                            type="button"
                            className="btn"
                            aria-label={`配置 ${slot}`}
                            onClick={() => openSlotPanel(slot)}
                            disabled={save.isPending}
                          >
                            配置
                          </button>
                        </>
                      )}
                    </span>
                  </div>
                );
              })}
              {configuredSlotCount > 0 ? (
                <div className="prow">
                  <div className="grow">
                    <b>全部恢复默认</b>
                    <div className="meta">清除所有槽位覆盖配置，恢复使用主研究模型。</div>
                  </div>
                  <button
                    type="button"
                    className="btn danger"
                    aria-label="清除全部模型槽位"
                    onClick={clearAllSlots}
                    disabled={save.isPending}
                  >
                    全部清除
                  </button>
                </div>
              ) : null}
            </>
          ) : null}
        </div>
        <p className="note inset">
          未单独配置的槽位将自动回落至主研究模型（当前为{personal ? "我的模型" : "平台模型"}）。槽位仅保存协议、服务地址、模型名称与非密钥引用，绝不存储或暴露明文 API Key。
        </p>
      </section>

      {panelOpen ? (
        <MyModelPanel saved={data} onClose={() => setOpen(false)} onSaved={(next) => { testSequence.current++; setTest(null); if (!next.research_tools_verified_at) void runTest(next); }} />
      ) : null}
      {editingSlot && data ? (
        <SlotPanel
          key={editingSlot}
          slot={editingSlot}
          saved={data}
          onClose={() => setEditingSlot(null)}
          onSaveBinding={saveSingleSlot}
          onClearSlot={clearSingleSlot}
          isPending={save.isPending}
          error={save.isError ? save.error : null}
        />
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
    </>
  );
}
