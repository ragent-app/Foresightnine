import { OPERATION_STATUS_LABEL, toolLabel } from "../../lib/copy";
import { formatClock } from "../../lib/format";
import { useNow } from "../../ui/media";
import { LIVE_TURN, useTopicView, type LiveRun } from "./context";

function currentActivity(live: LiveRun): string {
  const progress = live.progress;
  // The runs list can lag behind; the progress endpoint is the fresher source of status.
  if (progress ? progress.operation_status === "pending" : live.operation.status === "pending") return "等待开始";
  if (!progress) return "处理中";
  const current = progress.current;
  const lastTool = [...live.events].reverse().find((event) => event.kind === "tool");
  if (current?.kind === "tool") {
    const rationale = lastTool && lastTool.kind === "tool" && lastTool.name === current.name ? lastTool.rationale : null;
    return rationale ? `${toolLabel(current.name)}：${rationale}` : toolLabel(current.name);
  }
  if (current?.kind === "model") return "模型思考中";
  return "处理中";
}

/** Spec §4.3 运行中: glass status bar above the composer; the only place with “取消”. */
export function LiveBar({ live, cancelling, onCancel }: { live: LiveRun; cancelling: boolean; onCancel: () => void }) {
  const view = useTopicView();
  const now = useNow(true);
  const progress = live.progress;
  const status = progress?.operation_status ?? live.operation.status;
  const startedAt = progress?.started_at ? Date.parse(progress.started_at) : Date.parse(live.operation.created_at);
  const usage = progress?.usage;
  const usageText = usage
    ? [`${usage.model_calls} 次调用`, ...(usage.search_calls > 0 ? [`${usage.search_calls} 次搜索`] : []), Number.isNaN(startedAt) ? "" : formatClock(Math.max(0, now - startedAt))].filter(Boolean).join(" · ")
    : "";
  return (
    <div className="livebar glass">
      <span className="dot running" aria-hidden="true" />
      <button type="button" className="what" onClick={() => view.openPanel({ kind: "record", tab: "steps", turnKey: LIVE_TURN })} title="查看研究过程">
        <b>{OPERATION_STATUS_LABEL[status] ?? "研究中"}</b> · {currentActivity(live)}
      </button>
      {usageText ? <span className="meta hide-narrow">{usageText}</span> : null}
      <button type="button" className="btn plain" onClick={onCancel} disabled={cancelling}>{cancelling ? "正在取消…" : "取消"}</button>
    </div>
  );
}
