import { modelPhaseLabel, toolLabel } from "../../lib/copy";
import { Icon } from "../../ui/icons";
import type { LiveRun } from "./context";

export function currentActivity(live: LiveRun): string {
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

/**
 * Spec v6 运行中: the status lives in the live turn itself and stopping in the composer, so the
 * only floating element is this narrow capsule, shown while the reader has scrolled away from the
 * live process. It names what the run is doing and brings the reader back.
 */
export function LiveCapsule({ live, onJump }: { live: LiveRun; onJump: () => void }) {
  return (
    <button type="button" className="live-capsule glass" onClick={onJump} aria-label="回到实时过程" title="回到实时过程">
      <span className="dot running" aria-hidden="true" />
      <span className="what">{currentActivity(live)}</span>
      <Icon name="down" size={14} />
    </button>
  );
}
