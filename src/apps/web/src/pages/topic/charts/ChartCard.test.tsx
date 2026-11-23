import type { ResearchV2 } from "@foresightnine/contracts";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TopicContext, type TopicView } from "../context";
import { ChartCard } from "./ChartCard";

const COMBO_CHART: ResearchV2.ResearchChart = {
  chart_id: "chart-combo-1",
  turn_id: "turn_1",
  chart_type: "combo_bar_line",
  title: "Northwind 收入与营业利润率",
  subtitle: "左轴：USD millions · 右轴：percent",
  x_label: "财年",
  axes: [
    { id: "left", label: "总收入", unit: "USD millions", currency: "USD", scale: "millions" },
    { id: "right", label: "营业利润率", unit: "percent", currency: null, scale: "ones" },
  ],
  series: [
    {
      name: "总收入",
      mark: "bar",
      y_axis_id: "left",
      is_projected: false,
      points: [
        {
          x: "FY2025",
          y: "41270",
          display_value: "USD 41,270 millions",
          is_projected: false,
          provenance: { fact_id: "F1", source_id: "src_annual", character_start: 100, character_end: 150 },
        },
        {
          x: "FY2026E",
          y: "52000",
          display_value: "USD 52,000 millions",
          is_projected: true,
          provenance: { fact_id: "F2", source_id: "src_sim", character_start: null, character_end: null },
        },
      ],
    },
    {
      name: "营业利润率",
      mark: "line",
      y_axis_id: "right",
      is_projected: false,
      points: [
        {
          x: "FY2025",
          y: "31.45",
          display_value: "31.45%",
          is_projected: false,
          provenance: { calculation_id: "C1", source_id: "src_annual", character_start: 220, character_end: 260 },
        },
      ],
    },
  ],
  is_simulated: true,
  has_projected_points: true,
  replaces_chart_id: null,
  source_ids: ["src_annual", "src_sim"],
};

const PRICE_CHART: ResearchV2.ResearchChart = {
  chart_id: "chart-price-1",
  turn_id: "turn_1",
  chart_type: "price_series",
  title: "Northwind 日线收盘走势",
  subtitle: "单位：USD",
  x_label: "交易日",
  axes: [{ id: "left", label: "收盘价", unit: "USD", currency: "USD", scale: "ones" }],
  series: [
    {
      name: "NWD",
      mark: "area",
      y_axis_id: "left",
      is_projected: false,
      points: [
        {
          x: "2026-08-01",
          y: "130.5",
          display_value: "USD 130.5",
          is_projected: false,
          provenance: { source_id: "src_annual", character_start: 30, character_end: 75 },
        },
      ],
    },
  ],
  is_simulated: false,
  has_projected_points: false,
  replaces_chart_id: null,
  source_ids: ["src_annual"],
};

function makeTopicView(openPanel = vi.fn()): TopicView {
  const titles: Record<string, string> = {
    src_annual: "Northwind FY2026 年报",
    src_sim: "场景假设表（模拟）",
  };
  return {
    topicId: "t1",
    topic: {
      topic_id: "t1",
      owner_id: "acc",
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
      request: { question: "q" },
      versions: [],
    },
    version: {
      version_id: "v1",
      created_at: "2026-08-01T00:00:00Z",
      created_by: "agent",
      review: "draft",
      content: {},
    },
    content: {},
    turns: [],
    citations: { numberFor: () => 0, entries: [] },
    sources: new Map(),
    live: null,
    locked: false,
    turnNumber: () => 1,
    sourceTitle: (id) => titles[id] ?? id,
    openPanel,
    deepDive: vi.fn(),
    gotoClaim: vi.fn(),
    reextractPdf: vi.fn(async () => undefined),
    draft: {},
    setDraft: vi.fn(),
    dirty: false,
    saveDraft: vi.fn(async () => "v2"),
    saving: false,
    notify: vi.fn(),
    evidence: new Map(),
    setEvidence: vi.fn(),
  };
}

describe("ChartCard", () => {
  it("renders title, subtitle, simulated/projected badges, lazy ECharts container, and toggles to data table with provenance links", async () => {
    const openPanel = vi.fn();
    const view = makeTopicView(openPanel);
    const { container } = render(
      <TopicContext.Provider value={view}>
        <ChartCard chart={COMBO_CHART} />
      </TopicContext.Provider>,
    );

    const figure = screen.getByRole("figure", { name: "Northwind 收入与营业利润率" });
    expect(within(figure).getByText("模拟数据")).toBeTruthy();
    expect(within(figure).getByText("含推演点")).toBeTruthy();
    expect(within(figure).getByText("左轴：USD millions · 右轴：percent")).toBeTruthy();
    expect(within(figure).getByText("来源：Northwind FY2026 年报、场景假设表（模拟）")).toBeTruthy();

    await waitFor(
      () => expect(container.querySelector('[data-chart-engine="echarts"]')).toBeTruthy(),
      { timeout: 5000 },
    );

    // Switch to accessible data table and click provenance cells with/without char offsets.
    fireEvent.click(within(figure).getByRole("button", { name: "数据表" }));
    expect(within(figure).getByRole("table", { name: "Northwind 收入与营业利润率 数据表" })).toBeTruthy();
    expect(within(figure).getByText("USD 41,270 millions")).toBeTruthy();

    fireEvent.click(within(figure).getByRole("button", { name: /USD 41,270 millions/ }));
    expect(openPanel).toHaveBeenCalledWith({
      kind: "reader",
      sourceId: "src_annual",
      range: [100, 150],
    });

    fireEvent.click(within(figure).getByRole("button", { name: /USD 52,000 millions/ }));
    expect(openPanel).toHaveBeenCalledWith({
      kind: "reader",
      sourceId: "src_sim",
      range: null,
    });
  });

  it("lazy-loads PriceSeriesChart for price_series charts and shows CSV export + zoom modal without injecting <style>", async () => {
    const stylesBefore = document.querySelectorAll("style").length;
    const capturedBlobs: Blob[] = [];
    const createObjectUrl = vi.fn((blob: Blob) => {
      capturedBlobs.push(blob);
      return "blob:chart-csv";
    });
    const revokeObjectUrl = vi.fn();
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    vi.stubGlobal("URL", { ...URL, createObjectURL: createObjectUrl, revokeObjectURL: revokeObjectUrl });

    const view = makeTopicView();
    const { container } = render(
      <TopicContext.Provider value={view}>
        <ChartCard chart={PRICE_CHART} />
      </TopicContext.Provider>,
    );

    await waitFor(
      () => expect(container.querySelector('[data-chart-engine="lightweight-charts"]')).toBeTruthy(),
      { timeout: 5000 },
    );
    expect(screen.getByText("USD 130.5")).toBeTruthy();

    // CSV export
    fireEvent.click(screen.getByRole("button", { name: "导出 CSV" }));
    expect(createObjectUrl).toHaveBeenCalledTimes(1);
    const csvText = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result ?? ""));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(capturedBlobs[0]!);
    });
    expect(csvText).toContain("NWD,2026-08-01,130.5,USD 130.5,src_annual,30-75,否");

    // Zoom modal
    fireEvent.click(screen.getByRole("button", { name: "放大查看" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Northwind 日线收盘走势")).toBeTruthy();
    await waitFor(
      () => expect(dialog.querySelector(".chart-canvas.is-modal")).toBeTruthy(),
      { timeout: 5000 },
    );

    fireEvent.click(within(dialog).getByRole("button", { name: "关闭放大视图" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.querySelectorAll("style").length).toBe(stylesBefore);
    anchorClick.mockRestore();
  });
});
