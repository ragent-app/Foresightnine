import { useId, useState } from "react";
import { useAccountSummary } from "../../api/settings";
import { deleteAccount, signOut } from "../../auth/account";
import { useSession } from "../../auth/session";
import { formatDateTime, formatInteger } from "../../lib/format";
import { quotaShare } from "../../lib/settings";
import { describeError } from "../research/errors";

const CONFIRM_WORD = "删除";
const PLAN_LABEL: Record<string, string> = { free: "免费方案" };

function UsageBar({ consumed, reserved, limit }: { consumed: number; reserved: number; limit: number }) {
  const share = quotaShare(consumed, reserved, limit);
  // SVG geometry instead of inline style attributes, which the deployed CSP blocks.
  return (
    <div className="usage-bar" role="img" aria-label={`已用 ${Math.round(share.used * 100)}%`}>
      <svg viewBox="0 0 1000 1" preserveAspectRatio="none" aria-hidden="true">
        <rect className="used" x={0} y={0} width={share.used * 1000} height={1} />
        <rect className="reserved" x={share.used * 1000} y={0} width={share.reserved * 1000} height={1} />
      </svg>
    </div>
  );
}

function Usage() {
  const summary = useAccountSummary();
  const data = summary.data;
  return (
    <section>
      <div className="gtitle">本期用量</div>
      <div className="group-box">
        {summary.isLoading ? <div className="prow"><div className="skeleton line grow" /></div> : null}
        {summary.isError ? <div className="prow"><span className="meta">用量暂时无法加载。</span></div> : null}
        {data ? (
          <div className="prow stack">
            <div className="line">
              <b className="grow">{PLAN_LABEL[data.plan] ?? data.plan}{data.entitlement_status !== "active" ? <span className="flag">（{data.entitlement_status}）</span> : null}</b>
              <span className="value">{formatInteger(data.quota_consumed)} <span className="meta">/ {formatInteger(data.quota_limit)} Token</span></span>
            </div>
            <UsageBar consumed={data.quota_consumed} reserved={data.quota_reserved} limit={data.quota_limit} />
            <span className="meta">
              剩余 {formatInteger(data.quota_remaining)} Token
              {data.quota_reserved > 0 ? ` · 进行中的研究预留 ${formatInteger(data.quota_reserved)}` : ""}
              {data.quota_resets_at ? ` · ${formatDateTime(data.quota_resets_at)} 重置` : ""}
            </span>
          </div>
        ) : null}
      </div>
      <p className="note inset">平台模型按 Token 计入本期额度；使用“我的模型”时不消耗平台额度，费用由你的模型服务结算。</p>
    </section>
  );
}

export function AccountSection() {
  const session = useSession();
  const ids = useId();
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const remove = async () => {
    if (confirm !== CONFIRM_WORD || busy) return;
    setBusy(true);
    setError(null);
    try {
      await deleteAccount();
    } catch (failure) {
      setError(describeError(failure));
      setBusy(false);
    }
  };

  const method = session?.auth_mode === "local_mock" ? "开发环境本地账户" : session?.provider === "github" ? "GitHub 登录" : "Google 登录";
  return (
    <>
      <section aria-labelledby={`${ids}-account`}>
        <div className="gtitle" id={`${ids}-account`}>账户</div>
        <div className="group-box">
          <div className="prow">
            <div className="grow"><b>{session?.account.email}</b><div className="meta">{method}</div></div>
            <button type="button" className="btn" onClick={() => void signOut()}>退出登录</button>
          </div>
        </div>
      </section>
      <Usage />
      <section aria-label="删除账户">
        <div className="group-box">
          <div className="prow">
            <div className="grow">
              <b>删除账户</b>
              <div className="meta">研究、资料和版本会一并删除，无法恢复。输入“{CONFIRM_WORD}”后才能确认。</div>
            </div>
            <div className="field">
              <label className="sr-only" htmlFor={`${ids}-confirm`}>输入“{CONFIRM_WORD}”确认</label>
              <input id={`${ids}-confirm`} value={confirm} onChange={(event) => setConfirm(event.target.value)} placeholder={CONFIRM_WORD} autoComplete="off" />
            </div>
            <button type="button" className="btn" disabled={confirm !== CONFIRM_WORD || busy} onClick={() => void remove()}>{busy ? "正在删除…" : "删除账户"}</button>
          </div>
        </div>
        {error ? <p className="inline-error" role="alert">{error}</p> : null}
      </section>
    </>
  );
}
