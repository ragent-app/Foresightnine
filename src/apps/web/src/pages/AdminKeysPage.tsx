import { useCallback, useEffect, useState } from "react";
import { apiRequest } from "../api/http";
import { Toolbar, NetworkBanner } from "../ui/Toolbar";
import { ConfirmDialog } from "../ui/ConfirmDialog";

type Status = { enabled: boolean; eligible: boolean; passphrase_required: boolean; elevated: boolean; expires_at: string | null };
type KeyStatus = {
  name: string; label: string; category: string; provider_id: string; signup_url: string | null; docs_url: string | null;
  notes: string | null; region: string | null; configured: boolean; source: "db" | "env" | "none";
  key_hint: string | null; updated_at: string | null; last_test: { ok: boolean; reason: string | null; at: string } | null;
};
type AuditEntry = { at: string; actor: string; action: string; name: string; result: string };

function date(value: string | null): string { return value ? new Date(value).toLocaleString() : "—"; }
function externalUrl(value: string | null): string | undefined {
  if (!value) return undefined;
  try { const url = new URL(value); return url.protocol === "https:" ? url.href : undefined; } catch { return undefined; }
}

export function AdminKeysPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [passphrase, setPassphrase] = useState("");
  const [keys, setKeys] = useState<KeyStatus[] | null>(null);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [clearing, setClearing] = useState<KeyStatus | null>(null);
  const headers = token ? { "X-Admin-Elevation": token } : undefined;

  useEffect(() => {
    let active = true;
    void apiRequest<Status>("/v1/admin/status").then((result) => { if (active) setStatus(result); }).catch(() => { if (active) setError("无法验证管理员权限 / Unable to verify administrator access."); });
    return () => { active = false; };
  }, []);

  const load = useCallback(async () => {
    const options = { headers: token ? { "X-Admin-Elevation": token } : undefined };
    const [keyData, auditData] = await Promise.all([
      apiRequest<{ keys: KeyStatus[] }>("/v1/admin/keys", options),
      apiRequest<{ entries: AuditEntry[] }>("/v1/admin/audit", options),
    ]);
    setKeys(keyData.keys);
    setAudit(auditData.entries);
  }, [token]);

  useEffect(() => {
    if (!status?.enabled || !status.eligible || (status.passphrase_required && !token)) return;
    let active = true;
    void load().catch(() => { if (active) { setKeys(null); setError("无法加载管理数据 / Unable to load administrator data."); } });
    return () => { active = false; };
  }, [status, token, load]);

  async function elevate(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true); setError("");
    try {
      const result = await apiRequest<{ elevation_token: string; expires_at: string }>("/v1/admin/elevation", { method: "POST", body: { passphrase } });
      setPassphrase(""); setToken(result.elevation_token);
    } catch { setPassphrase(""); setError("验证失败 / Verification failed."); }
    finally { setBusy(false); }
  }

  async function action(key: KeyStatus, kind: "replace" | "clear" | "test") {
    setBusy(true); setError(""); setMessage("");
    const path = `/v1/admin/keys/${encodeURIComponent(key.name)}`;
    try {
      if (kind === "replace") {
        await apiRequest(path, { method: "PUT", headers, body: { value: values[key.name] } });
        setValues((old) => ({ ...old, [key.name]: "" }));
      } else if (kind === "clear") {
        await apiRequest(path, { method: "DELETE", headers }); setClearing(null);
      } else {
        const result = await apiRequest<{ ok: boolean }>(`${path}/test`, { method: "POST", headers });
        setMessage(result.ok ? "测试通过 / Test passed." : "测试未通过 / Test failed.");
      }
      await load();
    } catch { setError("操作失败，请检查权限和服务状态 / Action failed. Check access and service status."); }
    finally { setBusy(false); }
  }

  return <>
    <Toolbar scrolled={false} /><NetworkBanner />
    <div className="scroll" data-page="AdminKeysPage"><div className="prefs">
      <h1>平台密钥 / Platform keys</h1>
      <p className="note">仅限授权管理员。密钥只在提交时发送，保存后不会返回。 / Authorized administrators only. Saved credentials are never returned.</p>
      {!status && !error ? <p>正在验证权限… / Checking access…</p> : null}
      {status && (!status.enabled || !status.eligible) ? <p role="alert">无权访问此页面 / Administrator access unavailable.</p> : null}
      {status?.enabled && status.eligible && status.passphrase_required && !token ? <form className="group-box" onSubmit={(event) => void elevate(event)}>
        <div className="prow stack"><div className="field"><label htmlFor="admin-passphrase">管理员口令 / Admin passphrase</label><input id="admin-passphrase" type="password" autoComplete="off" value={passphrase} onChange={(event) => setPassphrase(event.target.value)} /></div>
          <button type="submit" className="btn primary" disabled={busy || !passphrase}>验证 / Verify</button></div>
      </form> : null}
      {error ? <p className="inline-error" role="alert">{error}</p> : null}
      {message ? <p role="status">{message}</p> : null}
      {keys ? <><section><div className="gtitle">服务商 / Providers</div><div className="group-box">
        {keys.map((key) => <div className="prow stack" key={key.name}>
          <div><b>{key.label}</b> <span className="meta">{key.category} · {key.provider_id}{key.region ? ` · ${key.region}` : ""}</span>
            <div className="meta">{key.source === "db" ? "数据库 / Database" : key.source === "env" ? "环境变量 / Environment" : "未设置 / Unset"}{key.key_hint ? ` · ${key.key_hint}` : ""} · 更新 / Updated: {date(key.updated_at)}</div>
            <div className="meta">上次测试 / Last test: {key.last_test ? `${key.last_test.ok ? "通过 / Passed" : "未通过 / Failed"} · ${date(key.last_test.at)}` : "—"}</div>
            {key.notes ? <div className="meta">{key.notes}</div> : null}
            {externalUrl(key.signup_url) ? <a href={externalUrl(key.signup_url)} target="_blank" rel="noreferrer">注册 / Sign up</a> : null}{" "}
            {externalUrl(key.docs_url) ? <a href={externalUrl(key.docs_url)} target="_blank" rel="noreferrer">文档 / Docs</a> : null}
          </div>
          <div className="field"><label htmlFor={`key-${key.name}`}>{key.label} 密钥 / Key</label><input id={`key-${key.name}`} type="password" autoComplete="off" spellCheck={false} value={values[key.name] ?? ""} onChange={(event) => setValues((old) => ({ ...old, [key.name]: event.target.value }))} /></div>
          <div className="prow-actions"><button type="button" className="btn primary" disabled={busy || !values[key.name]} onClick={() => void action(key, "replace")}>替换 / Replace</button><button type="button" className="btn" disabled={busy || !key.configured} onClick={() => void action(key, "test")}>测试 / Test</button><button type="button" className="btn danger" disabled={busy || key.source !== "db"} onClick={() => setClearing(key)}>清除 / Clear</button></div>
        </div>)}
      </div></section><section><div className="gtitle">审计记录 / Audit</div><div className="group-box">{audit.length ? audit.map((entry, index) => <div className="prow" key={`${entry.at}-${index}`}><div className="grow"><b>{entry.action} · {entry.name}</b><div className="meta">{date(entry.at)} · {entry.actor} · {entry.result}</div></div></div>) : <div className="prow">暂无记录 / No entries.</div>}</div></section></> : null}
      {clearing ? <ConfirmDialog title="清除平台密钥？ / Clear platform key?" description={`${clearing.label}：将清除数据库中的密钥；如果配置了环境变量，将回退到环境变量。`} onCancel={() => setClearing(null)} actions={[{ key: "clear", label: "清除 / Clear", tone: "danger", disabled: busy, onSelect: () => void action(clearing, "clear") }]} /> : null}
    </div></div>
  </>;
}
