import type { BranchView } from "./types";

export const historical = [0.95, 0.96, 0.94, 0.98, 1.01, 1.0, 1.04, 1.08, 1.05, 1.1, 1.06, 1.08] as const;
export const future = {
  weak: [1.075, 1.05, 1.05, 1.04, 1.02, 1.03, 1.02, 1.04, 1.06, 1.08],
  main: [1.085, 1.1, 1.12, 1.13, 1.15, 1.16, 1.18, 1.2, 1.22, 1.27],
  strong: [1.095, 1.11, 1.16, 1.17, 1.21, 1.23, 1.27, 1.3, 1.34, 1.4],
} as const satisfies Record<BranchView["kind"], readonly number[]>;

export type BranchKind = BranchView["kind"];
export type PathMap = Readonly<Record<BranchKind, readonly number[]>>;

export interface NavExportData {
  readonly historical: readonly number[];
  readonly weak: readonly number[];
  readonly main: readonly number[];
  readonly strong: readonly number[];
  readonly interval: { readonly low: readonly number[]; readonly high: readonly number[] };
  readonly drawdown: PathMap;
  readonly boundary: { readonly historicalCount: number; readonly todayIndex: number; readonly simulatedStartIndex: number; readonly label: string };
}

export function fullPaths(): PathMap {
  return { weak: [...historical, ...future.weak], main: [...historical, ...future.main], strong: [...historical, ...future.strong] };
}

export function drawdownPaths(paths: PathMap): PathMap {
  return { weak: toDrawdown(paths.weak), main: toDrawdown(paths.main), strong: toDrawdown(paths.strong) };
}

export function toDrawdown(values: readonly number[]): readonly number[] {
  let peak = Number.NEGATIVE_INFINITY;
  return values.map((value) => { peak = Math.max(peak, value); return value / peak - 1; });
}

export function intervalFor(paths: PathMap): { readonly low: readonly number[]; readonly high: readonly number[] } {
  return {
    low: paths.weak.map((value, index) => Math.min(value, paths.main[index]!, paths.strong[index]!)),
    high: paths.strong.map((value, index) => Math.max(value, paths.main[index]!, paths.weak[index]!)),
  };
}

export function buildNavExport(): NavExportData {
  const paths = fullPaths();
  return { historical: [...historical], weak: paths.weak, main: paths.main, strong: paths.strong, interval: intervalFor(paths), drawdown: drawdownPaths(paths), boundary: { historicalCount: historical.length, todayIndex: historical.length - 1, simulatedStartIndex: historical.length, label: "历史 historical_only → 今天边界 → simulated fixture" } };
}
