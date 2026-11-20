import type { ResearchV2 } from "@foresightnine/contracts";
import type { ComputeFmResult } from "@foresightnine/fm-engine";

export const FM_CLIENT_SOURCE_ID = "client_fm_simulation";
export const FM_SIMULATION_SUBTITLE = "模拟数据 · 客户端 FM 计算";

export interface FmSeriesObservation {
  date: string;
  value: number;
}

export interface BuildFmWeightsInput {
  chartId?: string;
  title?: string;
  assets: readonly string[];
  fmResult: Pick<ComputeFmResult, "cutoff" | "rawClientResult">;
  constrainedWeights: Float64Array | readonly number[];
  sourceId?: string;
}

export interface BuildFmNavInput {
  chartId?: string;
  title?: string;
  points: readonly FmSeriesObservation[];
  benchmarkPoints?: readonly FmSeriesObservation[];
  seriesName?: string;
  benchmarkName?: string;
  cutoff?: string;
  sourceId?: string;
}

export interface BuildFmDrawdownInput {
  chartId?: string;
  title?: string;
  points: readonly FmSeriesObservation[];
  seriesName?: string;
  cutoff?: string;
  sourceId?: string;
}

export interface FmStressScenario {
  scenario: string;
  impactPercent: number;
  isProjected?: boolean;
}

export interface BuildFmStressTestInput {
  chartId?: string;
  title?: string;
  scenarios: readonly FmStressScenario[];
  cutoff?: string;
  sourceId?: string;
}

function formatFixed(value: number, decimals = 2): string {
  if (!Number.isFinite(value)) throw new RangeError("FM chart value must be finite.");
  const fixed = value.toFixed(decimals);
  return fixed === "-0.00" || fixed === "-0.0000" ? fixed.slice(1) : fixed;
}

function makeSubtitle(parts: (string | undefined)[]): string {
  return [...parts.filter(Boolean), FM_SIMULATION_SUBTITLE].join(" · ");
}

/**
 * Builds a grouped bar chart strictly separating FM Raw target weights from
 * portfolio-constrained weights. Always marked `is_simulated: true`.
 */
export function buildFmWeightsChart({
  chartId = "fm-weights",
  title = "组合权重对比：FM Raw 与约束后配置",
  assets,
  fmResult,
  constrainedWeights,
  sourceId = FM_CLIENT_SOURCE_ID,
}: BuildFmWeightsInput): ResearchV2.ResearchChart {
  const rawWeights = fmResult.rawClientResult.targetWeights;
  if (assets.length === 0) throw new RangeError("assets must not be empty.");
  if (rawWeights.length !== assets.length || constrainedWeights.length !== assets.length) {
    throw new RangeError("assets, raw targetWeights, and constrainedWeights must have matching lengths.");
  }

  const rawPoints: ResearchV2.ChartPoint[] = [];
  const constrainedPoints: ResearchV2.ChartPoint[] = [];

  for (let i = 0; i < assets.length; i += 1) {
    const asset = assets[i]!;
    const rawPct = rawWeights[i]! * 100;
    const constrainedPct = constrainedWeights[i]! * 100;
    const rawStr = formatFixed(rawPct, 2);
    const conStr = formatFixed(constrainedPct, 2);

    rawPoints.push({
      x: asset,
      y: rawStr,
      display_value: `${rawStr}%`,
      is_projected: false,
      provenance: { source_id: sourceId },
    });
    constrainedPoints.push({
      x: asset,
      y: conStr,
      display_value: `${conStr}%`,
      is_projected: false,
      provenance: { source_id: sourceId },
    });
  }

  return {
    chart_id: chartId,
    chart_type: "grouped_bar",
    title,
    subtitle: makeSubtitle([
      "单位：percent",
      fmResult.cutoff ? `截止 ${fmResult.cutoff}` : undefined,
    ]),
    x_label: "资产",
    axes: [{ id: "left", label: "配置权重", unit: "percent", currency: null, scale: "ones" }],
    series: [
      { name: "FM Raw 权重", mark: "bar", y_axis_id: "left", is_projected: false, points: rawPoints },
      { name: "约束后权重", mark: "bar", y_axis_id: "left", is_projected: false, points: constrainedPoints },
    ],
    is_simulated: true,
    has_projected_points: false,
    replaces_chart_id: null,
    source_ids: [sourceId],
  };
}

/** Builds a simulated client-side NAV path chart (`price_series`, `is_simulated: true`). */
export function buildFmNavChart({
  chartId = "fm-nav",
  title = "FM 模拟组合净值走势（NAV）",
  points,
  benchmarkPoints,
  seriesName = "模拟组合 NAV",
  benchmarkName = "等权基准 NAV",
  cutoff,
  sourceId = FM_CLIENT_SOURCE_ID,
}: BuildFmNavInput): ResearchV2.ResearchChart {
  if (points.length === 0) throw new RangeError("NAV points must not be empty.");
  const toPoints = (rows: readonly FmSeriesObservation[]): ResearchV2.ChartPoint[] =>
    rows.map((pt) => {
      const y = formatFixed(pt.value, 4);
      return {
        x: pt.date,
        y,
        display_value: y,
        is_projected: false,
        provenance: { source_id: sourceId },
      };
    });

  const series: ResearchV2.ChartSeries[] = [
    { name: seriesName, mark: "area", y_axis_id: "left", is_projected: false, points: toPoints(points) },
  ];
  if (benchmarkPoints && benchmarkPoints.length > 0) {
    series.push({
      name: benchmarkName,
      mark: "line",
      y_axis_id: "left",
      is_projected: false,
      points: toPoints(benchmarkPoints),
    });
  }

  return {
    chart_id: chartId,
    chart_type: "price_series",
    title,
    subtitle: makeSubtitle(["单位：净值指数", cutoff ? `截止 ${cutoff}` : undefined]),
    x_label: "日期",
    axes: [{ id: "left", label: "组合净值", unit: "NAV", currency: null, scale: "ones" }],
    series,
    is_simulated: true,
    has_projected_points: false,
    replaces_chart_id: null,
    source_ids: [sourceId],
  };
}

/** Computes exact percentage drawdowns (0 or negative %) from a simulated NAV series. */
export function computeNavDrawdowns(navPoints: readonly FmSeriesObservation[]): FmSeriesObservation[] {
  let peak = Number.NEGATIVE_INFINITY;
  return navPoints.map((pt) => {
    if (!Number.isFinite(pt.value) || pt.value <= 0) {
      throw new RangeError("NAV observation must be a positive finite number.");
    }
    if (pt.value > peak) peak = pt.value;
    const ddPct = ((pt.value / peak) - 1) * 100;
    return { date: pt.date, value: ddPct };
  });
}

/** Builds a simulated client-side drawdown series (`price_series`, `is_simulated: true`). */
export function buildFmDrawdownChart({
  chartId = "fm-drawdown",
  title = "FM 模拟组合历史回撤",
  points,
  seriesName = "组合回撤",
  cutoff,
  sourceId = FM_CLIENT_SOURCE_ID,
}: BuildFmDrawdownInput): ResearchV2.ResearchChart {
  if (points.length === 0) throw new RangeError("drawdown points must not be empty.");
  return {
    chart_id: chartId,
    chart_type: "price_series",
    title,
    subtitle: makeSubtitle(["单位：percent", cutoff ? `截止 ${cutoff}` : undefined]),
    x_label: "日期",
    axes: [{ id: "left", label: "回撤幅度", unit: "percent", currency: null, scale: "ones" }],
    series: [
      {
        name: seriesName,
        mark: "area",
        y_axis_id: "left",
        is_projected: false,
        points: points.map((pt) => {
          const y = formatFixed(pt.value, 2);
          return {
            x: pt.date,
            y,
            display_value: `${y}%`,
            is_projected: false,
            provenance: { source_id: sourceId },
          };
        }),
      },
    ],
    is_simulated: true,
    has_projected_points: false,
    replaces_chart_id: null,
    source_ids: [sourceId],
  };
}

/** Builds a simulated scenario stress-test bar chart (`bar`, `is_simulated: true`). */
export function buildFmStressTestChart({
  chartId = "fm-stress",
  title = "FM 模拟组合情景压力测试",
  scenarios,
  cutoff,
  sourceId = FM_CLIENT_SOURCE_ID,
}: BuildFmStressTestInput): ResearchV2.ResearchChart {
  if (scenarios.length === 0) throw new RangeError("stress scenarios must not be empty.");
  const chartPoints: ResearchV2.ChartPoint[] = scenarios.map((item) => {
    const y = formatFixed(item.impactPercent, 2);
    return {
      x: item.scenario,
      y,
      display_value: `${item.impactPercent > 0 ? "+" : ""}${y}%`,
      is_projected: Boolean(item.isProjected),
      provenance: { source_id: sourceId },
    };
  });

  return {
    chart_id: chartId,
    chart_type: "bar",
    title,
    subtitle: makeSubtitle(["单位：percent", cutoff ? `截止 ${cutoff}` : undefined]),
    x_label: "压力情景",
    axes: [{ id: "left", label: "组合收益影响", unit: "percent", currency: null, scale: "ones" }],
    series: [
      {
        name: "组合收益影响",
        mark: "bar",
        y_axis_id: "left",
        is_projected: chartPoints.some((p) => p.is_projected),
        points: chartPoints,
      },
    ],
    is_simulated: true,
    has_projected_points: chartPoints.some((p) => p.is_projected),
    replaces_chart_id: null,
    source_ids: [sourceId],
  };
}
