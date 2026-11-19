import { useSyncExternalStore, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useLayout } from "./AppLayout";
import { Icon } from "./icons";

/**
 * Spec §3.1: transparent until the large title scrolls away, then glass with a small title.
 * The expand-sidebar and new-research buttons only show while the sidebar is collapsed or a drawer.
 */
export function Toolbar({ title, scrolled, children }: { title?: string; scrolled: boolean; children?: ReactNode }) {
  const { toggleSide } = useLayout();
  const navigate = useNavigate();
  return (
    <div className={`toolbar${scrolled ? " scrolled" : ""}`}>
      <button type="button" className="icon-btn when-collapsed" onClick={toggleSide} aria-label="展开侧栏" title="展开侧栏">
        <Icon name="sidebar" />
      </button>
      <button type="button" className="icon-btn when-collapsed" onClick={() => navigate("/research")} aria-label="新研究" title="新研究">
        <Icon name="compose" />
      </button>
      <div className="tb-title" aria-hidden={!scrolled}>{title ?? ""}</div>
      {children}
    </div>
  );
}

function subscribeOnline(onChange: () => void) {
  window.addEventListener("online", onChange);
  window.addEventListener("offline", onChange);
  return () => {
    window.removeEventListener("online", onChange);
    window.removeEventListener("offline", onChange);
  };
}

/** Spec §4.9: a thin notice under the toolbar while the browser reports no network. */
export function NetworkBanner() {
  const online = useSyncExternalStore(subscribeOnline, () => navigator.onLine, () => true);
  if (online) return null;
  return <div className="net-banner" role="status">网络连接已断开，恢复后会自动重试。</div>;
}
