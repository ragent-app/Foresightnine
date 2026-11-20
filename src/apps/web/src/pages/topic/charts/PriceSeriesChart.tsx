import type { ResearchV2 } from "@foresightnine/contracts";
import {
  AreaSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
  type BusinessDay,
  type IChartApi,
  type ISeriesApi,
  type SeriesType,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { toChartNumber, withAlpha, type ChartTheme } from "./chartTheme";

export interface ChartCanvasHandle {
  getPngDataUrl: () => string | null;
}

interface PriceSeriesChartProps {
  chart: ResearchV2.ResearchChart;
  theme: ChartTheme;
  size?: "card" | "modal";
  onSelectProvenance: (provenance: ResearchV2.ChartProvenance) => void;
}

interface HoverReadoutItem {
  name: string;
  color: string;
  displayValue: string;
  isProjected: boolean;
}

interface HoverReadout {
  x: string;
  items: HoverReadoutItem[];
}

const DATE_YMD = /^(\d{4})-(\d{2})-(\d{2})$/;
const DATE_YM = /^(\d{4})-(\d{2})$/;
const SYNTHETIC_BASE_SEC = 1_704_067_200; // 2024-01-01T00:00:00Z

function timeKey(time: Time | undefined): string {
  if (time === undefined) return "";
  if (typeof time === "string") return time;
  if (typeof time === "number") return String(time);
  const bd = time as BusinessDay;
  return `${bd.year}-${String(bd.month).padStart(2, "0")}-${String(bd.day).padStart(2, "0")}`;
}

function parseIsoDay(raw: string): string | null {
  const trimmed = raw.trim();
  if (DATE_YMD.test(trimmed)) return trimmed;
  if (DATE_YM.test(trimmed)) return `${trimmed}-01`;
  return null;
}

function compareTimeKeys(a: Time, b: Time): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  return timeKey(a).localeCompare(timeKey(b));
}

function canUseCanvas2d(): boolean {
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext?.("2d"));
  } catch {
    return false;
  }
}

const PriceSeriesChart = forwardRef<ChartCanvasHandle, PriceSeriesChartProps>(function PriceSeriesChart(
  { chart, theme, size = "card", onSelectProvenance },
  ref,
) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartApiRef = useRef<IChartApi | null>(null);
  const themeRef = useRef(theme);
  themeRef.current = theme;
  const onSelectRef = useRef(onSelectProvenance);
  onSelectRef.current = onSelectProvenance;
  const [readout, setReadout] = useState<HoverReadout | null>(null);

  const seriesList = chart.series ?? [];
  const axesList = chart.axes ?? [];

  const prepared = useMemo(() => {
    const allDates = seriesList.every((s) => s.points.every((p) => parseIsoDay(p.x) !== null));
    const categoryOrder = new Map<string, number>();
    const labelByTime = new Map<string, string>();
    if (!allDates) {
      let idx = 0;
      for (const s of seriesList) {
        for (const p of s.points) {
          if (!categoryOrder.has(p.x)) {
            categoryOrder.set(p.x, idx);
            const ts = SYNTHETIC_BASE_SEC + idx * 86_400;
            labelByTime.set(String(ts), p.x);
            idx += 1;
          }
        }
      }
    }

    const mappedSeries = seriesList.map((series, index) => {
      const color = theme.palette[index % theme.palette.length]!;
      const byTime = new Map<string, { time: Time; point: ResearchV2.ChartPoint }>();
      for (const point of series.points) {
        const time: Time = allDates
          ? (parseIsoDay(point.x)! as Time)
          : ((SYNTHETIC_BASE_SEC + (categoryOrder.get(point.x) ?? 0) * 86_400) as UTCTimestamp);
        byTime.set(timeKey(time), { time, point });
      }
      const ordered = [...byTime.values()].sort((a, b) => compareTimeKeys(a.time, b.time));
      return { series, color, ordered, byTime };
    });

    return { allDates, labelByTime, mappedSeries };
  }, [seriesList, theme.palette]);

  useImperativeHandle(
    ref,
    () => ({
      getPngDataUrl() {
        const api = chartApiRef.current;
        if (!api) return null;
        try {
          const shot = api.takeScreenshot(true, false);
          if (!shot || shot.width <= 0 || shot.height <= 0) return null;
          const out = document.createElement("canvas");
          out.width = shot.width;
          out.height = shot.height;
          const ctx = out.getContext("2d");
          if (!ctx) return null;
          ctx.fillStyle = themeRef.current.surface || "#ffffff";
          ctx.fillRect(0, 0, out.width, out.height);
          ctx.drawImage(shot, 0, 0);
          return out.toDataURL("image/png");
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
    const initialWidth = Math.max(container.clientWidth || 0, 260);
    const initialHeight = Math.max(container.clientHeight || 0, fallbackHeight);
    const hasRightAxis = axesList.some((ax) => ax.id === "right");
    const hasLeftAxis = axesList.some((ax) => ax.id === "left");
    const dualAxes = hasLeftAxis && hasRightAxis;

    const api = createChart(container, {
      width: initialWidth,
      height: initialHeight,
      layout: {
        // Strict CSP style-src 'self' prohibits runtime <style> creation by AttributionLogoWidget.
        attributionLogo: false,
        background: { type: ColorType.Solid, color: theme.surface },
        textColor: theme.onVariant,
        fontFamily: theme.font,
        fontSize: 11,
      },
      grid: {
        vertLines: { color: theme.grid },
        horzLines: { color: theme.grid },
      },
      rightPriceScale: {
        visible: true,
        borderColor: theme.separator,
      },
      leftPriceScale: {
        visible: dualAxes,
        borderColor: theme.separator,
      },
      timeScale: {
        borderColor: theme.separator,
        timeVisible: false,
        fixLeftEdge: true,
        fixRightEdge: true,
        ...(prepared.allDates
          ? {}
          : {
              tickMarkFormatter: (time: Time) => prepared.labelByTime.get(timeKey(time)) ?? timeKey(time),
            }),
      },
      crosshair: {
        mode: CrosshairMode.Magnet,
        vertLine: { color: theme.onTertiary, labelBackgroundColor: theme.onSurface },
        horzLine: { color: theme.onTertiary, labelBackgroundColor: theme.onSurface },
      },
      ...(prepared.allDates
        ? {}
        : {
            localization: {
              timeFormatter: (time: Time) => prepared.labelByTime.get(timeKey(time)) ?? timeKey(time),
            },
          }),
    });

    chartApiRef.current = api;

    const seriesEntries: {
      api: ISeriesApi<SeriesType, Time>;
      name: string;
      color: string;
      isSeriesProjected: boolean;
      byTime: Map<string, { time: Time; point: ResearchV2.ChartPoint }>;
      ordered: { time: Time; point: ResearchV2.ChartPoint }[];
    }[] = [];

    for (const item of prepared.mappedSeries) {
      const priceScaleId = dualAxes ? item.series.y_axis_id ?? "left" : "right";
      const dashed = Boolean(item.series.is_projected);
      const isArea = item.series.mark === "area" || (seriesList.length === 1 && item.series.mark !== "line");
      const baseOptions = {
        color: item.color,
        lineWidth: 2 as const,
        lineStyle: dashed ? LineStyle.Dashed : LineStyle.Solid,
        priceScaleId,
        priceLineVisible: false,
        lastValueVisible: true,
      };
      const seriesApi = isArea
        ? api.addSeries(AreaSeries, {
            ...baseOptions,
            lineColor: item.color,
            topColor: withAlpha(item.color, dashed ? 0.1 : 0.18),
            bottomColor: withAlpha(item.color, 0.01),
          })
        : api.addSeries(LineSeries, baseOptions);

      seriesApi.setData(
        item.ordered.map(({ time, point }) => ({
          time,
          value: toChartNumber(point.y),
          ...(point.is_projected ? { color: withAlpha(item.color, 0.65) } : {}),
        })),
      );

      seriesEntries.push({
        api: seriesApi as ISeriesApi<SeriesType, Time>,
        name: item.series.name,
        color: item.color,
        isSeriesProjected: Boolean(item.series.is_projected),
        byTime: item.byTime,
        ordered: item.ordered,
      });
    }

    api.timeScale().fitContent();

    const handleCrosshair = (param: { time?: Time }) => {
      const key = timeKey(param.time);
      if (!key) {
        setReadout(null);
        return;
      }
      const items: HoverReadoutItem[] = [];
      let labelX = prepared.labelByTime.get(key) ?? key;
      for (const entry of seriesEntries) {
        const hit = entry.byTime.get(key);
        if (!hit) continue;
        labelX = hit.point.x;
        items.push({
          name: entry.name,
          color: entry.color,
          displayValue: hit.point.display_value,
          isProjected: entry.isSeriesProjected || Boolean(hit.point.is_projected),
        });
      }
      setReadout(items.length > 0 ? { x: labelX, items } : null);
    };

    const handleClick = (param: { time?: Time; logical?: number; hoveredInfo?: { series?: ISeriesApi<SeriesType, Time> } }) => {
      const key = timeKey(param.time);
      const hoveredSeries = param.hoveredInfo?.series;
      if (hoveredSeries) {
        const entry = seriesEntries.find((s) => s.api === hoveredSeries);
        if (entry) {
          const hit = key
            ? entry.byTime.get(key)
            : typeof param.logical === "number"
              ? entry.ordered[Math.max(0, Math.min(entry.ordered.length - 1, Math.round(param.logical)))]
              : undefined;
          if (hit) {
            onSelectRef.current(hit.point.provenance);
            return;
          }
        }
      }
      for (const entry of seriesEntries) {
        const hit = key ? entry.byTime.get(key) : undefined;
        if (hit) {
          onSelectRef.current(hit.point.provenance);
          return;
        }
      }
    };

    api.subscribeCrosshairMove(handleCrosshair);
    api.subscribeClick(handleClick);

    let observer: ResizeObserver | undefined;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver((entries) => {
        const rect = entries[0]?.contentRect;
        const nextWidth = rect?.width ?? container.clientWidth;
        const nextHeight = rect?.height || container.clientHeight || initialHeight;
        if (nextWidth > 0 && nextHeight > 0) {
          api.resize(Math.floor(nextWidth), Math.floor(nextHeight));
        }
      });
      observer.observe(container);
    }

    return () => {
      observer?.disconnect();
      api.unsubscribeCrosshairMove(handleCrosshair);
      api.unsubscribeClick(handleClick);
      api.remove();
      chartApiRef.current = null;
    };
  }, [axesList, seriesList.length, prepared, size, theme]);

  const defaultSummary = useMemo(() => {
    const items: HoverReadoutItem[] = [];
    let latestX = "";
    for (const item of prepared.mappedSeries) {
      const last = item.ordered[item.ordered.length - 1];
      if (!last) continue;
      latestX = last.point.x;
      items.push({
        name: item.series.name,
        color: item.color,
        displayValue: last.point.display_value,
        isProjected: Boolean(item.series.is_projected || last.point.is_projected),
      });
    }
    return items.length > 0 ? { x: latestX, items } : null;
  }, [prepared.mappedSeries]);

  const activeReadout = readout ?? defaultSummary;

  return (
    <div className="chart-price-wrap">
      <div className="chart-readout" aria-live="off">
        {activeReadout ? (
          <>
            <span className="chart-readout-x">{activeReadout.x}</span>
            {activeReadout.items.map((item) => (
              <span key={item.name} className="chart-readout-item">
                <svg className="chart-dot-svg" width="8" height="8" viewBox="0 0 8 8" aria-hidden="true">
                  <circle cx="4" cy="4" r="4" fill={item.color} />
                </svg>
                <span className="chart-readout-k">{item.name}</span>
                <b className="chart-readout-v">{item.displayValue}</b>
                {item.isProjected ? <span className="chart-proj-pill">推演</span> : null}
              </span>
            ))}
          </>
        ) : (
          <span className="meta">悬停查看读数 · 点击数据点查看原文</span>
        )}
      </div>
      <div
        ref={containerRef}
        className={`chart-canvas chart-price-canvas${size === "modal" ? " is-modal" : ""}`}
        data-chart-engine="lightweight-charts"
      />
      <div className="chart-tv-attr">
        <a href="https://www.tradingview.com/" target="_blank" rel="noopener noreferrer" className="meta">
          Charts by TradingView
        </a>
      </div>
    </div>
  );
});

export default PriceSeriesChart;
