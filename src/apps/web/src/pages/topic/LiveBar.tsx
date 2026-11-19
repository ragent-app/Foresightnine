import { modelPhaseLabel, OPERATION_STATUS_LABEL, toolLabel } from "../../lib/copy";
import { formatClock } from "../../lib/format";
import { Icon } from "../../ui/icons";
import { useNow } from "../../ui/media";
import { LIVE_TURN, useTopicView, type LiveRun } from "./context";

function currentActivity(live: LiveRun): string {
  const progress = live.progress;
  // The runs list can lag behind; the progress endpoint is the fresher source of status.
  if (progress ? progress.operation_status === "pending" : live.operation.status === "pending") return "等待开始";
  if (!progress) return "处理中";
  const current = progress.current;
  if (current?.kind === "tool") {
    const label = toolLabel(current.name);
    const detail = current.rationale || current.target || null;
    return detail ? `${label}：${detail}` : label;
  }
  if (current?.kind === "model") return modelPhaseLabel(current.phase, current.retry_attempt);
  return "处理中";
}

/** Spec §4.3 运行中: glass status bar above the composer; the only place with “取消”. */
export function LiveBar({ live, cancelling, onCancel, awayFromLive = false, onJumpToLive }: {
  live: LiveRun;
  cancelling: boolean;
  onCancel: () => void;
  awayFromLive?: boolean;
  onJumpToLive?: () => void;
}) {
  const view = useTopicView();
  const now = useNow(true);
  const progress = live.progress;
  const status = progress?.operation_status ?? live.operation.status;
  const startedAt = progress?.started_at ? Date.parse(progress.started_at) : Date.parse(live.operation.created_at);
  const usage = progress?.usage;
  const usageText = usage
    ? [`${usage.model_calls} 次调用`, ...(usage.search_calls > 0 ? [`${usage.search_calls} 次搜索`] : []), Number.isNaN(startedAt) ? "" : formatClock(Math.max(0, now - startedAt))].filter(Boolean).join(" · ")
    : "";
  const openSteps = () => view.openPanel({ kind: "record", tab: "steps", turnKey: LIVE_TURN });
  return (
    <div className="livebar glass">
      <span className="dot running" aria-hidden="true" />
      <button
        type="button"
        className="what"
        onClick={awayFromLive && onJumpToLive ? onJumpToLive : openSteps}
        title={awayFromLive ? "回到实时过程" : "查看研究过程"}
      >
        <b>{OPERATION_STATUS_LABEL[status] ?? "研究中"}</b> · {currentActivity(live)}
      </button>
      {usageText ? <span className="meta hide-narrow">{usageText}</span> : null}
      <button type="button" className="icon-btn livebar-steps" onClick={openSteps} aria-label="打开过程面板" title="打开过程面板">
        <Icon name="inspector" size={15} />
      </button>
      <button type="button" className="btn plain" onClick={onCancel} disabled={cancelling}>{cancelling ? "正在取消…" : "取消"}</button>
    </div>
  );
}
