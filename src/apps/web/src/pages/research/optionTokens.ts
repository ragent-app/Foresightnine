import type { ComposerToken } from "../../ui/Composer";
import { DEFAULT_OPTIONS, EMPTY_BUDGET, hasBudget, type RunOptions } from "./options";

/** Tokens for every non-default next-run option (spec §4.2); each × restores the default. */
export function optionTokens(options: RunOptions, setOptions: (options: RunOptions) => void): ComposerToken[] {
  const tokens: ComposerToken[] = [];
  if (options.selectedSourceIds !== null) {
    tokens.push({
      key: "selection",
      label: `本轮读取 ${options.selectedSourceIds.length} 份资料`,
      removeLabel: "改为读取全部资料",
      onRemove: () => setOptions({ ...options, selectedSourceIds: null }),
    });
  }
  if (options.discover === "off") {
    tokens.push({ key: "search", label: "不联网搜索", removeLabel: "开启联网搜索", onRemove: () => setOptions({ ...options, discover: DEFAULT_OPTIONS.discover }) });
  }
  if (options.autoFetch === "off") {
    tokens.push({ key: "fetch", label: "不自动获取官方资料", removeLabel: "恢复自动获取官方资料", onRemove: () => setOptions({ ...options, autoFetch: DEFAULT_OPTIONS.autoFetch }) });
  }
  if (hasBudget(options)) {
    tokens.push({ key: "budget", label: "已设置预算", removeLabel: "移除预算", onRemove: () => setOptions({ ...options, budget: EMPTY_BUDGET }) });
  }
  return tokens;
}
