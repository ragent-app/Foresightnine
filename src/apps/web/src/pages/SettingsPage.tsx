import { useSearchParams } from "react-router-dom";
import { Segmented } from "../ui/controls";
import { NetworkBanner, Toolbar } from "../ui/Toolbar";
import { AccountSection } from "./settings/AccountSection";
import { DataSection } from "./settings/DataSection";
import { ModelSection } from "./settings/ModelSection";

type Tab = "account" | "model" | "data";
const TABS: { value: Tab; label: string }[] = [
  { value: "account", label: "账户" },
  { value: "model", label: "模型" },
  { value: "data", label: "数据源" },
];

/** Spec §4.8. The tab lives in `?tab=` so a section can be linked to directly. */
export function SettingsPage() {
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const tab: Tab = raw === "model" || raw === "data" ? raw : "account";
  return (
    <>
      <Toolbar scrolled={false} />
      <NetworkBanner />
      <div className="scroll" data-page="SettingsPage">
        <div className="prefs">
          <h1>设置</h1>
          <div className="prefs-tabs">
            <Segmented asTabs inline label="设置分类" options={TABS} value={tab} onChange={(next) => setParams(next === "account" ? {} : { tab: next }, { replace: true })} />
          </div>
          {tab === "account" ? <AccountSection /> : null}
          {tab === "model" ? <ModelSection /> : null}
          {tab === "data" ? <DataSection /> : null}
        </div>
      </div>
    </>
  );
}
