import { useEffect, useRef, useState } from "react";
import { Button, Field, Input, StatusDot, Tag } from "./Controls";
import { useLanguage } from "../i18n";
import type { OAuthSession } from "../oauth-client";
import {
  deleteModelConfig,
  createModelRequestGenerationGuard,
  fetchModelConfig,
  isLoadedModelConfigOwner,
  modelConfigSessionKey,
  normalizeModelBaseUrl,
  saveModelConfig,
  testModelConfig,
  type ModelConfig,
  type ModelConfigMode,
  type ModelConfigUpdate,
} from "../model-config-client";

export interface ModelConfigPanelProps {
  readonly apiBaseUrl: string | null;
  readonly session: OAuthSession | null;
}

type PanelStatus = "idle" | "loading" | "saving" | "testing" | "deleting" | "saved" | "deleted" | "tested-ready" | "tested-unavailable" | "test-error" | "error";

function copy(language: "zh" | "en", zh: string, en: string): string {
  return language === "en" ? en : zh;
}

function isPersonalKeyHint(value: string | undefined): value is string {
  return value !== undefined && /^••••[A-Za-z0-9]{4}$/u.test(value);
}

function noticeFor(status: PanelStatus, language: "zh" | "en"): string {
  switch (status) {
    case "loading": return copy(language, "正在读取模型配置…", "Loading model settings…");
    case "saving": return copy(language, "正在保存模型配置…", "Saving model settings…");
    case "testing": return copy(language, "已保存，正在测试个人模型连接…", "Saved. Testing the personal model connection…");
    case "deleting": return copy(language, "正在删除个人模型配置…", "Deleting the personal model configuration…");
    case "saved": return copy(language, "模型配置已保存；新的研究请求会使用当前选择。", "Model settings saved. New research requests will use this selection.");
    case "deleted": return copy(language, "个人模型配置已删除；研究请求已切回平台模型。", "Personal model settings deleted. Research requests now use the platform model.");
    case "tested-ready": return copy(language, "模型配置已保存，连接测试通过。", "Model settings saved and the connection test passed.");
    case "tested-unavailable": return copy(language, "模型配置已保存，但供应商同时拒绝了模型列表和最短聊天探测。请确认 Base URL 包含兼容 API 根路径、模型已授权且供应商账户有可用额度；不会回退到平台模型。", "Model settings were saved, but the provider rejected both model discovery and the minimal chat probe. Confirm that the Base URL includes the compatible API root, the model is authorized, and the provider account has available quota. The request will not fall back to the platform model.");
    case "test-error": return copy(language, "模型配置已保存，但连接测试请求失败；不会回退到平台模型。", "Model settings were saved, but the connection test request failed. The request will not fall back to the platform model.");
    case "error": return copy(language, "模型配置请求失败；服务端已有配置保持不变。", "The model settings request failed. Existing server settings were kept.");
    default: return "";
  }
}

export function ModelConfigPanel({ apiBaseUrl, session }: ModelConfigPanelProps) {
  const { language } = useLanguage();
  const [config, setConfig] = useState<ModelConfig | null>(null);
  const [loadedSessionKey, setLoadedSessionKey] = useState<string | null>(null);
  const [mode, setMode] = useState<ModelConfigMode>("platform");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [status, setStatus] = useState<PanelStatus>("idle");
  const [reloadKey, setReloadKey] = useState(0);
  const requestController = useRef<AbortController | null>(null);
  const requestGeneration = useRef(createModelRequestGenerationGuard()).current;
  const sessionKey = session ? modelConfigSessionKey(session) : "anonymous";

  const applyConfig = (next: ModelConfig, ownerKey = sessionKey): void => {
    setConfig(next);
    setLoadedSessionKey(ownerKey);
    setMode(next.mode);
    setBaseUrl(next.base_url ?? "");
    setModel(next.model ?? "");
    // The API never returns the secret. Do not retain a submitted key after a
    // successful response, even in component state.
    setApiKey("");
  };

  useEffect(() => {
    const generation = requestGeneration.next();
    requestController.current?.abort();
    requestController.current = null;
    // Clear every account-bound field before a new account load starts. A
    // failed B load can therefore never leave A's endpoint/model/key hint in
    // the controls or combine it with B's server-side key.
    setConfig(null);
    setLoadedSessionKey(null);
    setMode("platform");
    setBaseUrl("");
    setModel("");
    setApiKey("");
    if (!apiBaseUrl || !session) {
      setStatus("idle");
      return;
    }

    const controller = new AbortController();
    requestController.current = controller;
    setStatus("loading");
    void fetchModelConfig(apiBaseUrl, session, controller.signal).then((next) => {
      if (controller.signal.aborted || !requestGeneration.isCurrent(generation)) return;
      applyConfig(next, sessionKey);
      setStatus("idle");
    }).catch(() => {
      if (!controller.signal.aborted && requestGeneration.isCurrent(generation)) setStatus("error");
    }).finally(() => {
      if (requestController.current === controller) requestController.current = null;
    });
    return () => {
      controller.abort();
      if (requestController.current === controller) requestController.current = null;
    };
  }, [apiBaseUrl, reloadKey, sessionKey]);

  useEffect(() => () => { requestController.current?.abort(); }, []);

  const beginRequest = (): AbortController => {
    requestGeneration.next();
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    return controller;
  };

  const buildUpdate = (): ModelConfigUpdate | null => {
    if (!isLoadedModelConfigOwner(loadedSessionKey, session)) return null;
    if (mode === "platform") return { mode: "platform" };
    const normalizedBaseUrl = normalizeModelBaseUrl(baseUrl.trim());
    if (!normalizedBaseUrl) {
      setStatus("error");
      return null;
    }
    const modelValue = model.trim();
    if (!modelValue) {
      setStatus("error");
      return null;
    }
    const keyValue = apiKey.trim();
    const hasSavedKey = config?.mode === "personal" && isPersonalKeyHint(config.key_hint);
    if (!keyValue && !hasSavedKey) {
      setStatus("error");
      return null;
    }
    if (keyValue && keyValue.length < 8) {
      setStatus("error");
      return null;
    }
    if (/^••••[A-Za-z0-9]{4}$/u.test(keyValue)) {
      setStatus("error");
      return null;
    }
    return {
      mode: "personal",
      base_url: normalizedBaseUrl,
      model: modelValue,
      ...(keyValue ? { api_key: keyValue } : {}),
    };
  };

  const save = (testAfterSave: boolean): void => {
    if (!apiBaseUrl || !session || !isLoadedModelConfigOwner(loadedSessionKey, session) || status === "loading" || status === "saving" || status === "testing" || status === "deleting") return;
    const update = buildUpdate();
    if (!update) return;
    const controller = beginRequest();
    setStatus("saving");
    void saveModelConfig(apiBaseUrl, session, update, controller.signal).then((next) => {
      if (controller.signal.aborted) return null;
      applyConfig(next);
      if (!testAfterSave || next.mode !== "personal") {
        setStatus("saved");
        return null;
      }
      setStatus("testing");
      return testModelConfig(apiBaseUrl, session, controller.signal).then((testResult) => {
        if (!controller.signal.aborted) setStatus(testResult === "ready" ? "tested-ready" : "tested-unavailable");
        return null;
      }).catch(() => {
        if (!controller.signal.aborted) setStatus("test-error");
        return null;
      });
    }).then((testResult) => {
      if (controller.signal.aborted || testResult === null) return;
      setStatus(testResult === "ready" ? "tested-ready" : "tested-unavailable");
    }).catch(() => {
      if (!controller.signal.aborted) setStatus("error");
    }).finally(() => {
      if (requestController.current === controller) requestController.current = null;
    });
  };

  const remove = (): void => {
    if (!apiBaseUrl || !session || !isLoadedModelConfigOwner(loadedSessionKey, session) || config?.mode !== "personal" || status === "loading" || status === "saving" || status === "testing" || status === "deleting") return;
    const controller = beginRequest();
    setStatus("deleting");
    void deleteModelConfig(apiBaseUrl, session, controller.signal).then((next) => {
      if (controller.signal.aborted) return;
      applyConfig(next);
      setStatus("deleted");
    }).catch(() => {
      if (!controller.signal.aborted) setStatus("error");
    }).finally(() => {
      if (requestController.current === controller) requestController.current = null;
    });
  };

  if (!session) return <section className="model-config-panel" aria-labelledby="model-config-title"><div className="model-config-heading"><div><p className="eyebrow">{copy(language, "API / 模型配置", "API / model configuration")}</p><h2 id="model-config-title">{copy(language, "登录后配置你的模型", "Sign in to configure your model")}</h2><p>{copy(language, "当前未登录。登录后可选择平台模型或我的模型，并填写 Base URL、模型名和 password Key；密钥不会写入 localStorage、日志或研究导出。", "You are not signed in. After sign-in, choose the platform model or your model, then enter a Base URL, model name, and password Key. The key is never written to localStorage, logs, or research exports.")}</p></div></div></section>;
  if (!apiBaseUrl) return <section className="model-config-panel" role="alert" aria-labelledby="model-config-title"><p className="eyebrow">{copy(language, "API / 模型配置", "API / model configuration")}</p><h2 id="model-config-title">{copy(language, "账户模型服务暂不可用", "Account model service is unavailable")}</h2><p>{copy(language, "当前构建没有配置账户 API 地址；请使用明确标注的离线/模拟模式。", "This build has no account API URL; use the explicitly labelled offline/mock mode.")}</p></section>;
  if (status === "error" && !config) return <section className="model-config-panel" role="alert" aria-labelledby="model-config-title"><p className="eyebrow">{copy(language, "API / 模型配置", "API / model configuration")}</p><h2 id="model-config-title">{copy(language, "模型配置读取失败", "Could not load model settings")}</h2><p>{noticeFor("error", language)}</p><Button variant="secondary" type="button" onClick={() => setReloadKey((value) => value + 1)}>{copy(language, "重新读取", "Retry loading")}</Button></section>;
  if (status === "loading" || !config || !isLoadedModelConfigOwner(loadedSessionKey, session)) return <section className="model-config-panel" aria-busy="true" aria-labelledby="model-config-title"><p className="eyebrow">{copy(language, "API / 模型配置", "API / model configuration")}</p><h2 id="model-config-title">{copy(language, "正在读取账户模型配置", "Loading account model settings")}</h2><p className="model-config-loading"><StatusDot /> {noticeFor("loading", language)}</p></section>;

  const hasSavedKey = config.mode === "personal" && isPersonalKeyHint(config.key_hint);
  const busy = status === "saving" || status === "testing" || status === "deleting";
  const canTest = mode === "personal" && Boolean(baseUrl.trim()) && Boolean(model.trim()) && (apiKey.trim().length >= 8 || hasSavedKey);
  const notice = noticeFor(status, language);
  const isErrorNotice = status === "error" || status === "tested-unavailable" || status === "test-error";

  return <section className="model-config-panel" aria-labelledby="model-config-title" aria-busy={busy}>
    <div className="model-config-heading"><div><p className="eyebrow">{copy(language, "API / 模型配置", "API / model configuration")}</p><h2 id="model-config-title">{copy(language, "平台模型 / 我的模型", "Platform model / My model")}</h2><p>{copy(language, "研究请求使用这里的账户级路由。个人模型失败时不会回退到平台模型；BYOK 模型请求不扣平台额度。", "Research requests use this account-level route. A personal-model failure never falls back to the platform model; BYOK model requests do not consume platform quota.")}</p></div><span className="model-config-current" aria-live="polite"><StatusDot tone={mode === "personal" ? "blue" : "sage"} />{mode === "personal" ? copy(language, "我的模型", "My model") : copy(language, "平台模型", "Platform model")}</span></div>

    <form onSubmit={(event) => { event.preventDefault(); save(false); }}>
      <fieldset className="model-config-modes" disabled={busy}>
        <legend>{copy(language, "选择模型路由", "Choose model route")}</legend>
        <label className={`model-config-mode ${mode === "platform" ? "is-selected" : ""}`}><input type="radio" name="model-config-mode" value="platform" checked={mode === "platform"} onChange={() => { setMode("platform"); setStatus("idle"); }} /><span><strong>{copy(language, "平台模型", "Platform model")}</strong><small>{copy(language, "使用服务端平台路由，不填写个人凭据。", "Use the server platform route; no personal credential is needed.")}</small></span></label>
        <label className={`model-config-mode ${mode === "personal" ? "is-selected" : ""}`}><input type="radio" name="model-config-mode" value="personal" checked={mode === "personal"} onChange={() => { setMode("personal"); setStatus("idle"); }} /><span><strong>{copy(language, "我的模型（BYOK）", "My model (BYOK)")}</strong><small>{copy(language, "使用你自己的 OpenAI-compatible Base URL、模型和 Key。", "Use your own OpenAI-compatible Base URL, model, and Key.")}</small></span></label>
      </fieldset>

      <div className={`model-config-fields ${mode === "platform" ? "is-platform" : ""}`}>
        <Field label={copy(language, "Base URL", "Base URL")} helper={copy(language, "仅允许使用 HTTPS 443 端口的公开 OpenAI-compatible endpoint。", "Only a public HTTPS OpenAI-compatible endpoint on port 443 is accepted.")}>
          <Input id="model-config-base-url" type="url" inputMode="url" autoComplete="url" spellCheck={false} value={baseUrl} disabled={busy || mode !== "personal"} onChange={(event) => { setBaseUrl(event.target.value); setStatus("idle"); }} placeholder="https://models.example.com/v1" aria-label={copy(language, "个人模型 Base URL", "Personal model Base URL")} />
        </Field>
        <Field label={copy(language, "模型名", "Model name")} helper={copy(language, "填写供应商账户中可用的模型标识。", "Enter the model identifier available in your provider account.")}>
          <Input id="model-config-model" type="text" autoComplete="off" spellCheck={false} value={model} disabled={busy || mode !== "personal"} onChange={(event) => { setModel(event.target.value); setStatus("idle"); }} placeholder="gpt-4.1-mini" aria-label={copy(language, "个人模型名", "Personal model name")} />
        </Field>
        <Field label={copy(language, "password Key", "password Key")} helper={copy(language, hasSavedKey ? "留空保留已保存 Key；填写新值会替换它。输入框不会回显密钥。" : "填写个人模型 Key；保存后输入框会清空，页面只显示固定四位掩码提示。", hasSavedKey ? "Leave blank to keep the saved Key; enter a new value to replace it. The input never echoes the key." : "Enter the personal model Key. The input clears after saving and only a fixed four-character masked hint is shown.")}>
          <Input id="model-config-api-key" type="password" minLength={8} maxLength={512} autoComplete="new-password" spellCheck={false} name="model-config-api-key" value={apiKey} disabled={busy || mode !== "personal"} onChange={(event) => { setApiKey(event.target.value); setStatus("idle"); }} placeholder={hasSavedKey ? copy(language, "留空保留；填写新 Key 以替换", "Leave blank to keep; enter a new Key to replace"): copy(language, "填写个人模型 Key", "Enter personal model Key")} aria-label={copy(language, "个人模型 password Key", "Personal model password Key")} />
        </Field>
      </div>

      <div className="model-config-key-state" aria-live="polite">
        {hasSavedKey ? <span><StatusDot tone="sage" />{copy(language, "已保存 Key 提示", "Saved Key hint")} <Tag tone="strong">{config.key_hint}</Tag></span> : <span><StatusDot tone="warning" />{copy(language, "尚未保存个人 Key", "No personal Key saved")}</span>}
        {config.updated_at ? <small>{copy(language, "最近更新", "Updated")} {config.updated_at}</small> : null}
      </div>

      <div className="model-config-actions">
        <Button variant="primary" type="submit" disabled={busy}>{status === "saving" ? copy(language, "保存中…", "Saving…") : copy(language, "保存 / 替换", "Save / replace")}</Button>
        <Button variant="secondary" type="button" disabled={busy || !canTest} onClick={() => save(true)}>{status === "testing" ? copy(language, "测试中…", "Testing…") : copy(language, "保存并测试", "Save & test")}</Button>
        <Button variant="danger" type="button" disabled={busy || config.mode !== "personal"} onClick={remove}>{status === "deleting" ? copy(language, "删除中…", "Deleting…") : copy(language, "删除我的模型", "Delete my model")}</Button>
      </div>
    </form>
    {notice ? <p className={`model-config-notice ${isErrorNotice ? "is-error" : ""}`} role={isErrorNotice ? "alert" : "status"} aria-live="polite">{notice}</p> : null}
    <p className="model-config-privacy">{copy(language, "Key 只发送到当前账户 API，由服务端加密保存；本页面不读取密钥原文，也不会把它带入研究记录。连接测试先读取模型列表；若供应商未提供该接口，会发送不含研究内容的一 token 最短聊天探测，可能产生极少量供应商用量。", "The Key is sent only to the current account API and encrypted server-side. This page never reads it back or puts it into research records. The connection test checks model discovery first; if the provider omits that endpoint, it sends a one-token chat probe with no research content, which may incur a minimal amount of provider usage.")}</p>
  </section>;
}
