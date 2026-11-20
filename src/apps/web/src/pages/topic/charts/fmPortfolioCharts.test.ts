import { computeFm } from "@foresightnine/fm-engine";
import { render, screen } from "@testing-library/react";
import { createElement } from "react";
import { describe, expect, it } from "vitest";
import { ChartCard } from "./ChartCard";
import {
  buildFmDrawdownChart,
  buildFmNavChart,
  buildFmStressTestChart,
  buildFmWeightsChart,
  computeNavDrawdowns,
} from "./fmPortfolioCharts";

describe("fmPortfolioCharts client-side adapters", () => {
  it("separates FM Raw weights from constrained weights and forces is_simulated: true across all FM charts", () => {
    const fmResult = computeFm({
      cutoff: "2026-08-01T00:00:00Z",
      observations: [
        { asOf: "2026-06-01T00:00:00Z", fields: { mom: [0.1, 0.25, 0.05], close: [100, 50, 200] } },
        { asOf: "2026-07-01T00:00:00Z", fields: { mom: [0.15, 0.3, -0.02], close: [104, 52, 198] } },
        { asOf: "2026-08-01T00:00:00Z", fields: { mom: [0.22, 0.08, -0.1], close: [110, 53, 195] } },
      ],
      factors: [{ id: 1, icMean: 0.05, icir: 0.8, admitted: true, expression: { op: "field", name: "mom" } }],
      currentValues: new Float64Array([1000, 1000, 1000]),
    });

    const weightsChart = buildFmWeightsChart({
      assets: ["NVDA", "MSFT", "TSM"],
      fmResult,
      constrainedWeights: [0.4, 0.35, 0.25],
    });

    expect(weightsChart.chart_type).toBe("grouped_bar");
    expect(weightsChart.is_simulated).toBe(true);
    expect(weightsChart.subtitle).toContain("模拟数据 · 客户端 FM 计算");
    expect(weightsChart.series).toHaveLength(2);
    expect(weightsChart.series![0]!.name).toBe("FM Raw 权重");
    expect(weightsChart.series![1]!.name).toBe("约束后权重");
    expect(weightsChart.series![0]!.points.map((p) => p.y)).not.toEqual(
      weightsChart.series![1]!.points.map((p) => p.y),
    );
    expect(weightsChart.series![1]!.points.map((p) => p.y)).toEqual(["40.00", "35.00", "25.00"]);

    const navPoints = [
      { date: "2026-06-01", value: 1.0 },
      { date: "2026-07-01", value: 1.08 },
      { date: "2026-08-01", value: 1.026 },
    ];
    const navChart = buildFmNavChart({ points: navPoints, cutoff: "2026-08-01" });
    expect(navChart.chart_type).toBe("price_series");
    expect(navChart.is_simulated).toBe(true);
    expect(navChart.subtitle).toContain("模拟数据 · 客户端 FM 计算");

    const drawdowns = computeNavDrawdowns(navPoints);
    expect(drawdowns.map((d) => Number(d.value.toFixed(2)))).toEqual([0, 0, -5]);
    const drawdownChart = buildFmDrawdownChart({ points: drawdowns, cutoff: "2026-08-01" });
    expect(drawdownChart.chart_type).toBe("price_series");
    expect(drawdownChart.is_simulated).toBe(true);
    expect(drawdownChart.subtitle).toContain("模拟数据 · 客户端 FM 计算");

    const stressChart = buildFmStressTestChart({
      scenarios: [
        { scenario: "算力资本开支放缓 -20%", impactPercent: -12.4, isProjected: true },
        { scenario: "降息 50bps", impactPercent: 6.2, isProjected: true },
      ],
      cutoff: "2026-08-01",
    });
    expect(stressChart.chart_type).toBe("bar");
    expect(stressChart.is_simulated).toBe(true);
    expect(stressChart.has_projected_points).toBe(true);
    expect(stressChart.subtitle).toContain("模拟数据 · 客户端 FM 计算");

    // Verify ChartCard renders all 4 simulated FM charts with explicit "模拟数据" warning badge
    render(
      createElement(
        "div",
        null,
        createElement(ChartCard, { chart: weightsChart }),
        createElement(ChartCard, { chart: navChart }),
        createElement(ChartCard, { chart: drawdownChart }),
        createElement(ChartCard, { chart: stressChart }),
      ),
    );
    for (const c of [weightsChart, navChart, drawdownChart, stressChart]) {
      const fig = screen.getByRole("figure", { name: c.title });
      expect(fig.textContent).toContain("模拟数据");
      expect(fig.textContent).toContain("客户端 FM 计算");
    }
  });

  it("fails closed on invalid FM vector shapes", () => {
    expect(() =>
      buildFmWeightsChart({
        assets: ["A", "B"],
        fmResult: { cutoff: "2026-08-01", rawClientResult: { targetWeights: [0.5] } as never },
        constrainedWeights: [0.5, 0.5],
      }),
    ).toThrow(RangeError);
  });
});
