import type { ResearchV2 } from "@foresightnine/contracts";
import * as echarts from "echarts/core";
import { BarChart, LineChart, ScatterChart } from "echarts/charts";
import type { BarSeriesOption, LineSeriesOption, ScatterSeriesOption } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import type { GridComponentOption, LegendComponentOption, TooltipComponentOption } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { ComposeOption, ECElementEvent } from "echarts/core";
import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef } from "react";
import {
  buildWaterfallSeries,
  formatCompactAxisTick,
  toChartNumber,
  withAlpha,
  type ChartTheme,
} from "./chartTheme";
import type { ChartCanvasHandle } from "./PriceSeriesChart";

echarts.use([BarChart, LineChart, ScatterChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

type EChartsOption = ComposeOption<
  BarSeriesOption | LineSeriesOption | ScatterSeriesOption | GridComponentOption | LegendComponentOption | TooltipComponentOption
>;

interface EChartsCanvasChartProps {
  chart: ResearchV2.ResearchChart;
  theme: ChartTheme;
  size?: "card" | "modal";
  onSelectProvenance: (provenance: ResearchV2.ChartProvenance) => void;
}

interface DataItemMeta {
  provenance: ResearchV2.ChartProvenance;
  displayValue: string;
  xLabel: string;
  seriesName: string;
  isProjected: boolean;
}

function canUseCanvas2d(): boolean {
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext?.("2d"));
  } catch {
    return false;
  }
}

/** Strips ECharts richText brace syntax from untrusted data strings so tooltip formatting stays plain text. */
function richSafe(text: string): string {
  return String(text ?? "").replace(/\{/g, "（").replace(/\}/g, "）");
}

function axisTitle(axis: ResearchV2.ChartAxis): string {
  return axis.unit ? `${axis.label} (${axis.unit})` : axis.label;
}

function buildOption(
  chart: ResearchV2.ResearchChart,
  theme: ChartTheme,
): { option: EChartsOption; lookup: Map<string, DataItemMeta> } {
  const lookup = new Map<string, DataItemMeta>();
  const axesList = chart.axes ?? [];
  const seriesList = chart.series ?? [];
  const leftAxis = axesList.find((a) => a.id === "left") ?? axesList[0];
  const rightAxis = axesList.find((a) => a.id === "right");
  const hasRight = Boolean(rightAxis);
  const isScatter = chart.chart_type === "scatter";
  const isWaterfall = chart.chart_type === "waterfall";

  const yAxesConfig: NonNullable<EChartsOption["yAxis"]> = [];
  if (leftAxis) {
    yAxesConfig.push({
      type: "value",
      position: "left",
      name: axisTitle(leftAxis),
      nameGap: 10,
      nameTextStyle: { color: theme.onVariant, fontSize: 11, fontFamily: theme.font, align: "left" },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: theme.onVariant,
        fontSize: 11,
        fontFamily: theme.mono,
        formatter: (value: number) => formatCompactAxisTick(value),
      },
      splitLine: { lineStyle: { color: theme.grid } },
    });
  }
  if (rightAxis) {
    yAxesConfig.push({
      type: "value",
      position: "right",
      name: axisTitle(rightAxis),
      nameGap: 10,
      nameTextStyle: { color: theme.onVariant, fontSize: 11, fontFamily: theme.font, align: "right" },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: theme.onVariant,
        fontSize: 11,
        fontFamily: theme.mono,
        formatter: (value: number) => formatCompactAxisTick(value),
      },
      splitLine: { show: false },
    });
  }

  let xAxisConfig: NonNullable<EChartsOption["xAxis"]>;
  const seriesOptions: NonNullable<EChartsOption["series"]> = [];
  let showLegend = seriesList.length > 1;
  const xNameProps = chart.x_label ? { name: chart.x_label } : {};

  if (isWaterfall) {
    showLegend = false;
    const primary = seriesList[0];
    const items = primary ? buildWaterfallSeries(primary) : [];
    const categories = items.map((item) => item.x);
    xAxisConfig = {
      type: "category",
      data: categories,
      ...xNameProps,
      nameLocation: "middle",
      nameGap: 24,
      nameTextStyle: { color: theme.onVariant, fontSize: 11, fontFamily: theme.font },
      axisLine: { lineStyle: { color: theme.separator } },
      axisTick: { show: false },
      axisLabel: { color: theme.onVariant, fontSize: 11, fontFamily: theme.font, hideOverlap: true },
    };

    const yAxisIndex = primary?.y_axis_id === "right" && hasRight ? 1 : 0;
    const baseData = items.map((item) => item.base);
    const barData = items.map((item, idx) => {
      const baseColor =
        item.kind === "total" ? theme.total : item.kind === "positive" ? theme.positive : theme.negative;
      lookup.set(`1:${idx}`, {
        provenance: item.point.provenance,
        displayValue: item.point.display_value,
        xLabel: item.x,
        seriesName: primary?.name ?? "金额",
        isProjected: item.isProjected,
      });
      return {
        value: item.span,
        itemStyle: {
          color: item.isProjected ? withAlpha(baseColor, 0.55) : baseColor,
          borderRadius: [3, 3, 0, 0],
          ...(item.isProjected ? { borderColor: baseColor, borderWidth: 1.5, borderType: "dashed" as const } : {}),
        },
      };
    });

    seriesOptions.push(
      {
        name: "_waterfall_base",
        type: "bar",
        stack: "waterfall",
        yAxisIndex,
        silent: true,
        tooltip: { show: false },
        itemStyle: { borderColor: "transparent", color: "transparent" },
        emphasis: { itemStyle: { borderColor: "transparent", color: "transparent" } },
        data: baseData,
      },
      {
        name: primary?.name ?? "Bridge",
        type: "bar",
        stack: "waterfall",
        yAxisIndex,
        barMaxWidth: 38,
        data: barData,
      },
    );
  } else if (isScatter) {
    const numericX =
      seriesList.length > 0 &&
      seriesList.every((s) => s.points.every((p) => p.x.trim() !== "" && Number.isFinite(Number(p.x))));
    const categories = numericX
      ? []
      : [...new Set(seriesList.flatMap((s) => s.points.map((p) => p.x)))];

    xAxisConfig = numericX
      ? {
          type: "value",
          ...xNameProps,
          nameLocation: "middle",
          nameGap: 24,
          nameTextStyle: { color: theme.onVariant, fontSize: 11, fontFamily: theme.font },
          axisLine: { lineStyle: { color: theme.separator } },
          axisLabel: { color: theme.onVariant, fontSize: 11, fontFamily: theme.mono },
          splitLine: { lineStyle: { color: theme.grid } },
        }
      : {
          type: "category",
          data: categories,
          ...xNameProps,
          nameLocation: "middle",
          nameGap: 24,
          nameTextStyle: { color: theme.onVariant, fontSize: 11, fontFamily: theme.font },
          axisLine: { lineStyle: { color: theme.separator } },
          axisLabel: { color: theme.onVariant, fontSize: 11, fontFamily: theme.font, hideOverlap: true },
        };

    seriesList.forEach((s, sIdx) => {
      const color = theme.palette[sIdx % theme.palette.length]!;
      const yAxisIndex = s.y_axis_id === "right" && hasRight ? 1 : 0;
      const data = s.points.map((pt, pIdx) => {
        const isProjected = Boolean(s.is_projected || pt.is_projected);
        lookup.set(`${sIdx}:${pIdx}`, {
          provenance: pt.provenance,
          displayValue: pt.display_value,
          xLabel: pt.x,
          seriesName: s.name,
          isProjected,
        });
        const xVal = numericX ? Number(pt.x) : pt.x;
        return {
          value: [xVal, toChartNumber(pt.y)] as [string | number, number],
          itemStyle: isProjected
            ? { color: withAlpha(color, 0.55), borderColor: color, borderWidth: 1.5, borderType: "dashed" as const }
            : { color },
        };
      });
      seriesOptions.push({
        name: s.name,
        type: "scatter",
        yAxisIndex,
        symbolSize: 10,
        data,
      });
    });
  } else {
    const categories: string[] = [];
    const catSeen = new Set<string>();
    for (const s of seriesList) {
      for (const p of s.points) {
        if (!catSeen.has(p.x)) {
          catSeen.add(p.x);
          categories.push(p.x);
        }
      }
    }

    xAxisConfig = {
      type: "category",
      data: categories,
      ...xNameProps,
      nameLocation: "middle",
      nameGap: 24,
      nameTextStyle: { color: theme.onVariant, fontSize: 11, fontFamily: theme.font },
      axisLine: { lineStyle: { color: theme.separator } },
      axisTick: { show: false },
      axisLabel: { color: theme.onVariant, fontSize: 11, fontFamily: theme.font, hideOverlap: true },
    };

    const stacked = chart.chart_type === "stacked_bar";

    seriesList.forEach((s, sIdx) => {
      const color = theme.palette[sIdx % theme.palette.length]!;
      const yAxisIndex = s.y_axis_id === "right" && hasRight ? 1 : 0;
      const byX = new Map(s.points.map((pt) => [pt.x, pt]));
      const useBar =
        s.mark === "bar" ||
        (chart.chart_type !== "combo_bar_line" &&
          (chart.chart_type === "bar" || chart.chart_type === "grouped_bar" || chart.chart_type === "stacked_bar"));

      if (useBar) {
        const data = categories.map((cat, idx) => {
          const pt = byX.get(cat);
          if (!pt) return null;
          const isProjected = Boolean(s.is_projected || pt.is_projected);
          lookup.set(`${sIdx}:${idx}`, {
            provenance: pt.provenance,
            displayValue: pt.display_value,
            xLabel: pt.x,
            seriesName: s.name,
            isProjected,
          });
          return {
            value: toChartNumber(pt.y),
            itemStyle: isProjected
              ? {
                  color: withAlpha(color, 0.55),
                  borderColor: color,
                  borderWidth: 1.5,
                  borderType: "dashed" as const,
                  borderRadius: stacked ? [0, 0, 0, 0] : [4, 4, 0, 0],
                }
              : {
                  color,
                  borderRadius: stacked ? [0, 0, 0, 0] : [4, 4, 0, 0],
                },
          };
        });

        seriesOptions.push({
          name: s.name,
          type: "bar",
          yAxisIndex,
          ...(stacked ? { stack: "total" } : {}),
          barMaxWidth: 36,
          itemStyle: { color },
          data,
        });
      } else {
        const isArea = s.mark === "area";
        const isSeriesProjected = Boolean(s.is_projected);
        const data = categories.map((cat, idx) => {
          const pt = byX.get(cat);
          if (!pt) return null;
          const isProjected = Boolean(isSeriesProjected || pt.is_projected);
          lookup.set(`${sIdx}:${idx}`, {
            provenance: pt.provenance,
            displayValue: pt.display_value,
            xLabel: pt.x,
            seriesName: s.name,
            isProjected,
          });
          return {
            value: toChartNumber(pt.y),
            symbol: isProjected ? "emptyCircle" : "circle",
            symbolSize: 7,
            itemStyle: isProjected ? { color: withAlpha(color, 0.72), borderColor: color } : { color },
          };
        });

        seriesOptions.push({
          name: s.name,
          type: "line",
          yAxisIndex,
          smooth: false,
          symbol: "circle",
          symbolSize: 7,
          lineStyle: {
            color,
            width: 2,
            type: isSeriesProjected ? "dashed" : "solid",
          },
          itemStyle: { color },
          ...(isArea ? { areaStyle: { color: withAlpha(color, isSeriesProjected ? 0.08 : 0.14) } } : {}),
          data,
        });
      }
    });
  }

  const option: EChartsOption = {
    animation: false,
    grid: {
      left: 12,
      right: hasRight ? 14 : 12,
      top: showLegend ? 46 : 30,
      bottom: chart.x_label ? 34 : 16,
      containLabel: true,
    },
    ...(showLegend
      ? {
          legend: {
            type: "scroll",
            top: 0,
            left: 0,
            right: 0,
            icon: "roundRect",
            itemWidth: 10,
            itemHeight: 10,
            textStyle: { color: theme.onVariant, fontSize: 11.5, fontFamily: theme.font },
          },
        }
      : {}),
    tooltip: {
      trigger: isScatter ? "item" : "axis",
      // Strict CSP style-src 'self': draw tooltip purely inside Canvas via richText.
      renderMode: "richText",
      confine: true,
      backgroundColor: theme.surface,
      borderColor: theme.separator,
      borderWidth: 1,
      textStyle: {
        color: theme.onSurface,
        fontSize: 12,
        fontFamily: theme.font,
      },
      axisPointer: {
        type:
          chart.chart_type === "bar" ||
          chart.chart_type === "grouped_bar" ||
          chart.chart_type === "stacked_bar" ||
          isWaterfall
            ? "shadow"
            : "line",
        lineStyle: { color: theme.onTertiary },
      },
      formatter: (params: unknown) => {
        const list = Array.isArray(params) ? params : [params];
        const lines: string[] = [];
        let header = "";
        for (const rawItem of list) {
          if (!rawItem || typeof rawItem !== "object") continue;
          const item = rawItem as { seriesIndex?: number; dataIndex?: number; seriesName?: string; name?: string };
          if (item.seriesName === "_waterfall_base") continue;
          const meta = lookup.get(`${item.seriesIndex ?? -1}:${item.dataIndex ?? -1}`);
          if (!meta) continue;
          if (!header) header = richSafe(meta.xLabel);
          const projSuffix = meta.isProjected ? " [推演]" : "";
          lines.push(`${richSafe(meta.seriesName)}: ${richSafe(meta.displayValue)}${projSuffix}`);
        }
        if (lines.length === 0) return "";
        return `${header ? `${header}\n` : ""}${lines.join("\n")}\n点击可查看原文依据`;
      },
    },
    xAxis: xAxisConfig,
    yAxis: yAxesConfig.length > 0 ? yAxesConfig : [{ type: "value" }],
    series: seriesOptions,
  };

  return { option, lookup };
}

const EChartsCanvasChart = forwardRef<ChartCanvasHandle, EChartsCanvasChartProps>(function EChartsCanvasChart(
  { chart, theme, size = "card", onSelectProvenance },
  ref,
) {
  const containerRef = useRef<HTMLDivElement>(null);
  const instanceRef = useRef<echarts.ECharts | null>(null);
  const themeRef = useRef(theme);
  themeRef.current = theme;
  const onSelectRef = useRef(onSelectProvenance);
  onSelectRef.current = onSelectProvenance;

  const { option, lookup } = useMemo(() => buildOption(chart, theme), [chart, theme]);
  const lookupRef = useRef(lookup);
  lookupRef.current = lookup;

  useImperativeHandle(
    ref,
    () => ({
      getPngDataUrl() {
        const instance = instanceRef.current;
        if (!instance) return null;
        try {
          return instance.getDataURL({
            type: "png",
            pixelRatio: 2,
            backgroundColor: themeRef.current.surface || "#ffffff",
          });
        } catch {
          return null;
        }
      },
    }),
    [],
  );

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !canUseCanvas2d()) return;

    const fallbackHeight = size === "modal" ? 420 : 280;
    const width = container.clientWidth > 0 ? container.clientWidth : 600;
    const height = container.clientHeight > 0 ? container.clientHeight : fallbackHeight;

    const instance = echarts.init(container, undefined, {
      renderer: "canvas",
      width,
      height,
      devicePixelRatio: typeof window !== "undefined" ? Math.min(window.devicePixelRatio || 1, 2) : 1,
    });
    instanceRef.current = instance;

    const handleClick = (params: ECElementEvent) => {
      const meta = lookupRef.current.get(`${params.seriesIndex ?? -1}:${params.dataIndex ?? -1}`);
      if (meta) {
        onSelectRef.current(meta.provenance);
      }
    };
    instance.on("click", handleClick);

    let observer: ResizeObserver | undefined;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver((entries) => {
        const rect = entries[0]?.contentRect;
        const w = Math.floor(rect?.width ?? container.clientWidth);
        const h = Math.floor(rect?.height || container.clientHeight || fallbackHeight);
        if (w > 0 && h > 0) {
          instance.resize({ width: w, height: h });
        }
      });
      observer.observe(container);
    }

    return () => {
      observer?.disconnect();
      instance.off("click", handleClick);
      instance.dispose();
      instanceRef.current = null;
    };
  }, [size]);

  useEffect(() => {
    const instance = instanceRef.current;
    if (!instance) return;
    instance.setOption(option, { notMerge: true });
  }, [option]);

  return (
    <div
      ref={containerRef}
      className={`chart-canvas chart-echarts-canvas${size === "modal" ? " is-modal" : ""}`}
      data-chart-engine="echarts"
    />
  );
});

export default EChartsCanvasChart;
