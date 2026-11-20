import type { ResearchV2 } from "@foresightnine/contracts";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChartDataTable } from "./ChartDataTable";
import { resolveChartTheme } from "./chartTheme";

const echartsState = vi.hoisted(() => ({
  options: [] as Record<string, unknown>[],
  clickHandler: null as ((params: { seriesIndex?: number; dataIndex?: number }) => void) | null,
  disposeSpy: vi.fn(),
  resizeSpy: vi.fn(),
}));

const lwcState = vi.hoisted(() => ({
  createOptions: [] as Record<string, unknown>[],
  seriesConfigs: [] as Record<string, unknown>[],
  seriesData: [] as unknown[],
  crosshairHandler: null as ((param: { time?: unknown }) => void) | null,
  clickHandler: null as ((param: { time?: unknown; logical?: number; hoveredInfo?: { series?: unknown } }) => void) | null,
  removeSpy: vi.fn(),
  createdSeries: [] as Record<string, unknown>[],
}));

vi.mock("echarts/core", () => ({
  use: vi.fn(),
  init: vi.fn(() => ({
    setOption: vi.fn((opt: Record<string, unknown>) => {
      echartsState.options.push(opt);
    }),
    on: vi.fn((event: string, fn: (params: { seriesIndex?: number; dataIndex?: number }) => void) => {
      if (event === "click") echartsState.clickHandler = fn;
    }),
    off: vi.fn(),
    resize: echartsState.resizeSpy,
    dispose: echartsState.disposeSpy,
    getDataURL: vi.fn(() => "data:image/png;base64,iVBORw0KGgo="),
  })),
}));

vi.mock("lightweight-charts", () => ({
  ColorType: { Solid: "solid" },
  CrosshairMode: { Magnet: 1 },
  LineStyle: { Solid: 0, Dashed: 2 },
  LineSeries: { type: "Line" },
  AreaSeries: { type: "Area" },
  createChart: vi.fn((_el: HTMLElement, options: Record<string, unknown>) => {
    lwcState.createOptions.push(options);
    return {
      addSeries: vi.fn((_def: unknown, opts: Record<string, unknown>) => {
        const seriesObj = {
          setData: vi.fn((rows: unknown) => {
            lwcState.seriesData.push(rows);
          }),
        };
        lwcState.seriesConfigs.push(opts);
        lwcState.createdSeries.push(seriesObj);
        return seriesObj;
      }),
      timeScale: () => ({ fitContent: vi.fn() }),
      subscribeCrosshairMove: vi.fn((fn: (param: { time?: unknown }) => void) => {
        lwcState.crosshairHandler = fn;
      }),
      unsubscribeCrosshairMove: vi.fn(),
      subscribeClick: vi.fn((fn: (param: { time?: unknown; logical?: number; hoveredInfo?: { series?: unknown } }) => void) => {
        lwcState.clickHandler = fn;
      }),
      unsubscribeClick: vi.fn(),
      resize: vi.fn(),
      remove: lwcState.removeSpy,
      takeScreenshot: vi.fn(() => null),
    };
  }),
}));

import EChartsCanvasChart from "./EChartsCanvasChart";
import PriceSeriesChart from "./PriceSeriesChart";

describe("Canvas chart engines & CSP guards", () => {
  beforeEach(() => {
    echartsState.options = [];
    echartsState.clickHandler = null;
    echartsState.disposeSpy.mockClear();
    lwcState.createOptions = [];
    lwcState.seriesConfigs = [];
    lwcState.seriesData = [];
    lwcState.crosshairHandler = null;
    lwcState.clickHandler = null;
    lwcState.createdSeries = [];

    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => ({}) as CanvasRenderingContext2D);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("configures ECharts with richText tooltips (strict CSP), dual Y axes, dashed projected lines, waterfall stacking, and provenance clicks", () => {
    const onSelect = vi.fn();
    const theme = resolveChartTheme();
    const combo: ResearchV2.ResearchChart = {
      chart_id: "c_combo",
      chart_type: "combo_bar_line",
      title: "Dual Axis",
      axes: [
        { id: "left", label: "Revenue", unit: "USD millions", currency: "USD", scale: "millions" },
        { id: "right", label: "Margin", unit: "percent", scale: "ones" },
      ],
      series: [
        {
          name: "Rev",
          mark: "bar",
          y_axis_id: "left",
          is_projected: false,
          points: [{ x: "FY2026", y: "48612", display_value: "USD 48,612 millions", provenance: { fact_id: "F1", source_id: "s1", character_start: 10, character_end: 40 } }],
        },
        {
          name: "Margin (Est)",
          mark: "line",
          y_axis_id: "right",
          is_projected: true,
          points: [{ x: "FY2026", y: "31.07", display_value: "31.07%", is_projected: true, provenance: { calculation_id: "C1", source_id: "s2", character_start: 50, character_end: 80 } }],
        },
      ],
    };

    const { rerender, unmount } = render(<EChartsCanvasChart chart={combo} theme={theme} onSelectProvenance={onSelect} />);

    const opt = echartsState.options.at(-1)!;
    expect((opt["tooltip"] as Record<string, unknown>)["renderMode"]).toBe("richText");
    const series = opt["series"] as Record<string, unknown>[];
    expect(series[0]!["yAxisIndex"]).toBe(0);
    expect(series[1]!["yAxisIndex"]).toBe(1);
    expect((series[1]!["lineStyle"] as Record<string, unknown>)["type"]).toBe("dashed");

    // Verify tooltip formatter strips {style|...} richText braces and omits HTML tags
    const formatter = (opt["tooltip"] as { formatter: (params: unknown) => string }).formatter;
    const tip = formatter([{ seriesIndex: 0, dataIndex: 0 }]);
    expect(tip).toContain("USD 48,612 millions");
    expect(tip).not.toMatch(/<br|<div|<span/);

    // Verify clicking bar/point forwards exact provenance
    echartsState.clickHandler?.({ seriesIndex: 1, dataIndex: 0 });
    expect(onSelect).toHaveBeenCalledWith({ calculation_id: "C1", source_id: "s2", character_start: 50, character_end: 80 });

    // Switch to waterfall chart & verify stacked series + provenance click
    const waterfall: ResearchV2.ResearchChart = {
      chart_id: "c_wf",
      chart_type: "waterfall",
      title: "Bridge",
      axes: [{ id: "left", label: "EBIT", unit: "USD millions", scale: "millions" }],
      series: [
        {
          name: "Bridge",
          mark: "waterfall",
          y_axis_id: "left",
          points: [
            { x: "FY25", y: "100", display_value: "100", provenance: { source_id: "s1", character_start: 1, character_end: 5 } },
            { x: "Rev+", y: "30", display_value: "+30", provenance: { source_id: "s1", character_start: 6, character_end: 10 } },
            { x: "FY26", y: "130", display_value: "130", provenance: { source_id: "s1", character_start: 11, character_end: 15 } },
          ],
        },
      ],
    };
    rerender(<EChartsCanvasChart chart={waterfall} theme={theme} onSelectProvenance={onSelect} />);
    const wfOpt = echartsState.options.at(-1)!;
    const wfSeries = wfOpt["series"] as Record<string, unknown>[];
    expect(wfSeries).toHaveLength(2);
    expect(wfSeries[0]!["name"]).toBe("_waterfall_base");
    echartsState.clickHandler?.({ seriesIndex: 1, dataIndex: 1 });
    expect(onSelect).toHaveBeenLastCalledWith({ source_id: "s1", character_start: 6, character_end: 10 });

    unmount();
    expect(echartsState.disposeSpy).toHaveBeenCalled();
  });

  it("configures lightweight-charts with attributionLogo: false (no <style>), renders crosshair readout, and opens provenance on click", () => {
    const onSelect = vi.fn();
    const theme = resolveChartTheme();
    const price: ResearchV2.ResearchChart = {
      chart_id: "c_px",
      chart_type: "price_series",
      title: "Prices",
      axes: [{ id: "left", label: "Close", unit: "USD", currency: "USD", scale: "ones" }],
      series: [
        {
          name: "NVDA",
          mark: "area",
          y_axis_id: "left",
          points: [
            { x: "2026-08-01", y: "120.0", display_value: "USD 120.0", provenance: { source_id: "s1", character_start: 0, character_end: 20 } },
            { x: "2026-08-04", y: "125.5", display_value: "USD 125.5", provenance: { source_id: "s1", character_start: 21, character_end: 45 } },
          ],
        },
      ],
    };

    render(<PriceSeriesChart chart={price} theme={theme} onSelectProvenance={onSelect} />);

    expect((lwcState.createOptions[0]!["layout"] as Record<string, unknown>)["attributionLogo"]).toBe(false);
    expect(screen.getByText("USD 125.5")).toBeTruthy();

    act(() => {
      lwcState.crosshairHandler?.({ time: "2026-08-01" });
    });
    expect(screen.getByText("USD 120.0")).toBeTruthy();

    act(() => {
      lwcState.clickHandler?.({ time: "2026-08-01", hoveredInfo: { series: lwcState.createdSeries[0] } });
    });
    expect(onSelect).toHaveBeenCalledWith({ source_id: "s1", character_start: 0, character_end: 20 });
  });

  it("renders flat provenance rows in ChartDataTable when scatter points share duplicate X values", () => {
    const onSelect = vi.fn();
    const scatter: ResearchV2.ResearchChart = {
      chart_id: "c_sc",
      chart_type: "scatter",
      title: "Valuation Scatter",
      x_label: "P/E",
      axes: [{ id: "left", label: "Growth", unit: "percent", scale: "ones" }],
      series: [
        {
          name: "Peers",
          mark: "scatter",
          y_axis_id: "left",
          points: [
            { x: "25", y: "15", display_value: "15%", provenance: { source_id: "s1", character_start: 1, character_end: 4 } },
            { x: "25", y: "28", display_value: "28%", provenance: { source_id: "s2", character_start: 5, character_end: 9 } },
          ],
        },
        {
          name: "Target",
          mark: "scatter",
          y_axis_id: "left",
          points: [{ x: "22", y: "30", display_value: "30%", provenance: { source_id: "s1", character_start: 10, character_end: 15 } }],
        },
      ],
    };

    render(<ChartDataTable chart={scatter} sourceTitle={(id) => id} onSelectProvenance={onSelect} />);
    expect(screen.getByText("15%")).toBeTruthy();
    expect(screen.getByText("28%")).toBeTruthy();
    expect(screen.getByText("30%")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "s2" }));
    expect(onSelect).toHaveBeenCalledWith({ source_id: "s2", character_start: 5, character_end: 9 });
  });
});
