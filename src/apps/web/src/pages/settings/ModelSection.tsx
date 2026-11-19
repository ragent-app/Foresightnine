import { useEffect, useId, useState } from "react";
import { ApiError } from "../../api/http";
import { testModelConfig, useAccountSummary, useModelConfig, useSaveModelConfig, type ConnectionStatus, type ModelConfig, type ModelProtocol } from "../../api/settings";
import { apiKeyProblem, baseUrlProblem, canTestProtocol, hasSavedPersonal, keyRequired, modelNameProblem, PROTOCOL_LABEL } from "../../lib/settings";
import { ConfirmDialog } from "../../ui/ConfirmDialog";
import { Segmented } from "../../ui/controls";
import { useLayout } from "../../ui/AppLayout";
import { Panel } from "../../ui/Panel";
import { describeError } from "../research/errors";

type TestState = ConnectionStatus | "testing" | "error" | null;

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
  const save = useSaveModelConfig();
  // Prefill from the saved configuration whether or not it is active.
  const personal = hasSavedPersonal(saved) ? saved : undefined;
  const [protocol, setProtocol] = useState<ModelProtocol>(personal?.protocol ?? "openai_chat_completions");
  const [baseUrl, setBaseUrl] = useState(personal?.base_url ?? "");
  const [model, setModel] = useState(personal?.model ?? "");
  const [apiKey, setApiKey] = useState("");
  const [touched, setTouched] = useState(false);
  const needsKey = keyRequired(saved, protocol, baseUrl);
  const problems = { baseUrl: baseUrlProblem(baseUrl), model: modelNameProblem(model), apiKey: apiKeyProblem(apiKey, needsKey) };
  const valid = !problems.baseUrl && !problems.model && !problems.apiKey;
  const show = (problem: string | null) => (touched ? problem : null);

  const submit = () => {
    setTouched(true);
    if (!valid || save.isPending) return;
    save.mutate(
      { mode: "personal", protocol, base_url: baseUrl, model, ...(apiKey ? { api_key: apiKey } : {}) },
      { onSuccess: (config) => { onSaved(config); onClose(); } },
    );
  };

  return (
    <Panel
      title="我的模型"
      onClose={onClose}
      footer={<><button type="button" className="btn plain" onClick={onClose}>取消</button><button type="button" className="btn primary" onClick={submit} disabled={save.isPending || (touched && !valid)}>{save.isPending ? "正在保存…" : "保存"}</button></>}
    >
      <form className="add-source" onSubmit={(event) => { event.preventDefault(); submit(); }} noValidate>
        <div className="field">
          <label htmlFor={`${ids}-protocol`}>协议</label>
          <select id={`${ids}-protocol`} value={protocol} onChange={(event) => setProtocol(event.target.value as ModelProtocol)}>
            {(Object.keys(PROTOCOL_LABEL) as ModelProtocol[]).map((value) => <option key={value} value={value}>{PROTOCOL_LABEL[value]}</option>)}
          </select>
          <span className="help">按服务实际支持的接口选择。研究需要模型支持工具调用。</span>
        </div>
        <div className="field">
          <label htmlFor={`${ids}-url`}>服务地址</label>
          <input id={`${ids}-url`} type="url" inputMode="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://api.example.com/v1"
            autoCapitalize="off" spellCheck={false} aria-invalid={show(problems.baseUrl) ? true : undefined} />
          {show(problems.baseUrl) ? <span className="err">{problems.baseUrl}</span> : <span className="help">只支持可以从公网访问的 HTTPS 地址（443 端口）。</span>}
        </div>
        <div className="field">
          <label htmlFor={`${ids}-model`}>模型名称</label>
          <input id={`${ids}-model`} value={model} onChange={(event) => setModel(event.target.value)} placeholder="服务商提供的模型 ID" autoCapitalize="off" spellCheck={false}
            aria-invalid={show(problems.model) ? true : undefined} />
          {show(problems.model) ? <span className="err">{problems.model}</span> : null}
        </div>
        <div className="field">
          <label htmlFor={`${ids}-key`}>API Key</label>
          <input id={`${ids}-key`} type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} autoComplete="off"
            placeholder={personal?.key_hint && !needsKey ? `已保存 ${personal.key_hint} · 输入新值即可替换` : "输入 API Key"} aria-invalid={show(problems.apiKey) ? true : undefined} />
          {show(problems.apiKey) ? <span className="err">{problems.apiKey}</span> : (
            <span className="help">{personal?.key_hint && needsKey ? "更换服务地址的域名或协议后，需要重新输入 API Key。" : "密钥加密保存，只用于调用你配置的模型服务。"}</span>
          )}
        </div>
        {!canTestProtocol(protocol) ? <p className="callout-lite ok" role="note">Anthropic Messages 暂不支持连接测试，保存后可以用一次研究来验证。</p> : null}
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
  const [test, setTest] = useState<TestState>(null);
  const data = config.data;

  useEffect(() => {
    setPanelOpen(panelOpen);
    return () => setPanelOpen(false);
  }, [panelOpen, setPanelOpen]);

  const runTest = async () => {
    setTest("testing");
    try {
      setTest((await testModelConfig()).status);
    } catch {
      setTest("error");
    }
  };

  const personal = data?.mode === "personal";
  const saved = hasSavedPersonal(data);
  const switchTo = (mode: "platform" | "personal") => {
    if (!data || mode === data.mode || save.isPending) return;
    setTest(null);
    if (mode === "platform") {
      save.mutate({ mode: "platform" });
    } else if (saved) {
      // Reuse the saved configuration and key; nothing needs to be re-entered.
      save.mutate({ mode: "personal" }, { onSuccess: (next) => { if (canTestProtocol(next.protocol)) void runTest(); } });
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
            <div className="grow"><b>平台模型</b><div className="meta">{account.data?.provider_model ? `${account.data.provider_model} · ` : ""}按本期额度计费</div></div>
            <span className="role light support">使用中</span>
          </div>
        ) : null}
        {data && saved ? (
          <div className="prow">
            <div className="grow">
              <b>{PROTOCOL_LABEL[data.protocol ?? "openai_chat_completions"]} · {data.model}</b>
              <div className="meta">{data.base_url} · 密钥 {data.key_hint}{personal ? "" : " · 已保存，未使用"}</div>
            </div>
            {personal ? <TestStatus state={canTestProtocol(data.protocol) ? test : null} /> : null}
            {personal && canTestProtocol(data.protocol) ? <button type="button" className="btn" onClick={() => void runTest()} disabled={test === "testing"}>测试连接</button> : null}
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
        {personal && !canTestProtocol(data?.protocol) ? " Anthropic Messages 暂不支持连接测试，可以用一次研究来验证。" : ""}
      </p>
      {save.isError && !confirmDelete ? <p className="inline-error" role="alert">{modelErrorText(save.error)}</p> : null}
      {panelOpen ? (
        <MyModelPanel saved={data} onClose={() => setOpen(false)} onSaved={(next) => { setTest(null); if (canTestProtocol(next.protocol)) void runTest(); }} />
      ) : null}
      {confirmDelete ? (
        <ConfirmDialog
          title="删除我的模型配置？"
          description={`会删除保存的服务地址、模型名称和 API Key${personal ? "，并改用平台模型" : ""}。以后再用自己的模型时需要重新填写。`}
          onCancel={() => setConfirmDelete(false)}
          actions={[{
            key: "delete", label: save.isPending ? "正在删除…" : "删除", tone: "danger", disabled: save.isPending,
            onSelect: () => save.mutate("delete", { onSuccess: () => { setConfirmDelete(false); setTest(null); } }),
          }]}
          error={save.isError ? modelErrorText(save.error) : null}
        />
      ) : null}
    </section>
  );
}
