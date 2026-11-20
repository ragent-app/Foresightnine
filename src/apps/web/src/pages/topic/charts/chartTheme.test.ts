import type { ResearchV2 } from "@foresightnine/contracts";
import { afterEach, describe, expect, it } from "vitest";
import { buildWaterfallSeries, formatChartCsv, resolveChartTheme, withAlpha } from "./chartTheme";

const SAMPLE_CHART: ResearchV2.ResearchChart = {
  chart_id: "chart-1",
  turn_id: "turn-1",
  chart_type: "combo_bar_line",
  title: "Northwind 收入与利润率",
  subtitle: "左轴：USD billions · 右轴：percent",
  x_label: "财年",
  axes: [
    { id: "left", label: "总收入", unit: "USD billions", currency: "USD", scale: "billions" },
    { id: "right", label: "营业利润率", unit: "percent", currency: null, scale: "ones" },
  ],
  series: [
    {
      name: '总收入, "主营"',
      mark: "bar",
      y_axis_id: "left",
      is_projected: false,
      points: [
        {
          x: "FY2025",
          y: "41.27",
          display_value: "USD 41.27 billions",
          is_projected: false,
          provenance: { fact_id: "f1", source_id: "src_1", character_start: 10, character_end: 45 },
        },
        {
          x: "FY2026 (预估)",
          y: "55.10",
          display_value: "USD 55.10 billions",
          is_projected: true,
          provenance: { fact_id: "f2", source_id: "src_1", character_start: null, character_end: null },
        },
      ],
    },
    {
      name: "营业利润率",
      mark: "line",
      y_axis_id: "right",
      is_projected: true,
      points: [
        {
          x: "FY2025",
          y: "31.45",
          display_value: "31.45%",
          is_projected: false,
          provenance: { calculation_id: "c1", source_id: "src_2", character_start: 120, character_end: 160 },
        },
      ],
    },
  ],
  is_simulated: false,
  has_projected_points: true,
  replaces_chart_id: null,
  source_ids: ["src_1", "src_2"],
};

describe("chartTheme", () => {
  afterEach(() => {
    delete document.documentElement.dataset.theme;
  });

  it("formats standard RFC-4180 CSV with exact values, sources, ranges, and projection flags", () => {
    const csv = formatChartCsv(SAMPLE_CHART);
    const lines = csv.split("\n");
    expect(lines).toHaveLength(4);
    expect(lines[0]).toBe("系列,X轴,数值,显示值,来源ID,字符范围,是否推演");
    expect(lines[1]).toBe('"总收入, ""主营""",FY2025,41.27,USD 41.27 billions,src_1,10-45,否');
    expect(lines[2]).toBe('"总收入, ""主营""",FY2026 (预估),55.10,USD 55.10 billions,src_1,,是');
    expect(lines[3]).toBe("营业利润率,FY2025,31.45,31.45%,src_2,120-160,是");
  });

  it("converts waterfall bridge series deterministically with transparent bases and closing totals", () => {
    const bridge: ResearchV2.ChartSeries = {
      name: "利润桥",
      mark: "waterfall",
      y_axis_id: "left",
      is_projected: false,
      points: [
        { x: "FY2025", y: "100", display_value: "100", is_projected: false, provenance: { source_id: "s1" } },
        { x: "毛利增长", y: "30", display_value: "+30", is_projected: false, provenance: { source_id: "s1" } },
        { x: "研发增加", y: "-10", display_value: "-10", is_projected: true, provenance: { source_id: "s1" } },
        { x: "FY2026 合计", y: "120", display_value: "120", is_projected: false, provenance: { source_id: "s1" } },
      ],
    };

    const result = buildWaterfallSeries(bridge);
    expect(result.map((item) => ({
      x: item.x,
      base: item.base,
      span: item.span,
      rawValue: item.rawValue,
      runningTotal: item.runningTotal,
      kind: item.kind,
      isProjected: item.isProjected,
    }))).toEqual([
      { x: "FY2025", base: 0, span: 100, rawValue: 100, runningTotal: 100, kind: "total", isProjected: false },
      { x: "毛利增长", base: 100, span: 30, rawValue: 30, runningTotal: 130, kind: "positive", isProjected: false },
      { x: "研发增加", base: 120, span: 10, rawValue: -10, runningTotal: 120, kind: "negative", isProjected: true },
      { x: "FY2026 合计", base: 0, span: 120, rawValue: 120, runningTotal: 120, kind: "total", isProjected: false },
    ]);
  });

  it("keeps pure-delta ending bars as negative/positive when not matching cumulative sum", () => {
    const deltas: ResearchV2.ChartSeries = {
      name: "增量",
      mark: "waterfall",
      y_axis_id: "left",
      points: [
        { x: "期初", y: "100", display_value: "100", provenance: { source_id: "s1" } },
        { x: "增项", y: "25", display_value: "+25", provenance: { source_id: "s1" } },
        { x: "减项", y: "-15", display_value: "-15", provenance: { source_id: "s1" } },
      ],
    };
    const result = buildWaterfallSeries(deltas);
    expect(result[2]).toMatchObject({ base: 110, span: 15, runningTotal: 110, kind: "negative" });
  });

  it("resolves distinct light and dark palettes and applies alpha cleanly", () => {
    document.documentElement.dataset.theme = "light";
    const light = resolveChartTheme();
    document.documentElement.dataset.theme = "dark";
    const dark = resolveChartTheme();

    expect(light.isDark).toBe(false);
    expect(dark.isDark).toBe(true);
    expect(light.palette).toHaveLength(6);
    expect(light.surface).not.toBe(dark.surface);
    expect(withAlpha("#2b5aa8", 0.2)).toBe("rgba(43, 90, 168, 0.2)");
  });
});
