import { useId, useState } from "react";
import { deleteAccount, signOut } from "../auth/account";
import { useSession } from "../auth/session";
import { NetworkBanner, Toolbar } from "../ui/Toolbar";
import { describeError } from "./research/errors";

const CONFIRM_WORD = "删除";

/**
 * Spec §4.8, account section. The model and data-source sections arrive with M5;
 * until then the page shows only what works.
 */
export function SettingsPage() {
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
      <Toolbar scrolled={false} />
      <NetworkBanner />
      <div className="scroll" data-page="SettingsPage">
        <div className="prefs">
          <h1>设置</h1>
          <section aria-labelledby={`${ids}-account`}>
            <div className="gtitle" id={`${ids}-account`}>账户</div>
            <div className="group-box">
              <div className="prow">
                <div className="grow"><b>{session?.account.email}</b><div className="meta">{method}</div></div>
                <button type="button" className="btn" onClick={() => void signOut()}>退出登录</button>
              </div>
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
        </div>
      </div>
    </>
  );
}
