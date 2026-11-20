import type { ResearchV2 } from "@foresightnine/contracts";
import { useEffect, useState } from "react";
import { CAUSAL_DIRECTION_LABEL } from "../../../lib/copy";
import { saveBlob } from "../../../lib/download";

export interface ChartTheme {
  isDark: boolean;
  palette: readonly [string, string, string, string, string, string];
  onSurface: string;
  onVariant: string;
  onTertiary: string;
  separator: string;
  grid: string;
  surface: string;
  group: string;
  warning: string;
  positive: string;
  negative: string;
  total: string;
  font: string;
  mono: string;
}

const LIGHT_DEFAULTS: Omit<ChartTheme, "isDark"> = {
  palette: ["#2b5aa8", "#2f7a5b", "#b86b1b", "#6d59a8", "#2c7a7b", "#9e4b5b"],
  onSurface: "#1d1f24",
  onVariant: "#5d636e",
  onTertiary: "#8d929c",
  separator: "rgba(60, 66, 80, 0.13)",
  grid: "rgba(60, 66, 80, 0.10)",
  surface: "#ffffff",
  group: "rgba(255, 255, 255, 0.8)",
  warning: "#9a6200",
  positive: "#2f7a42",
  negative: "#c0262d",
  total: "#2b5aa8",
  font: 'Inter, "PingFang SC", "Hiragino Sans GB", "Noto Sans SC", system-ui, sans-serif',
  mono: 'ui-monospace, "SFMono-Regular", "JetBrains Mono", Consolas, monospace',
};

const DARK_DEFAULTS: Omit<ChartTheme, "isDark"> = {
  palette: ["#7ca7f2", "#6ec49c", "#e3a754", "#a896e0", "#65b8b8", "#df8594"],
  onSurface: "#f5f5f7",
  onVariant: "#b3b5bb",
  onTertiary: "#8a8c92",
  separator: "rgba(255, 255, 255, 0.11)",
  grid: "rgba(255, 255, 255, 0.09)",
  surface: "#000000",
  group: "rgba(255, 255, 255, 0.06)",
  warning: "#f5bd4f",
  positive: "#8fd19a",
  negative: "#ffb4ab",
  total: "#7ca7f2",
  font: 'Inter, "PingFang SC", "Hiragino Sans GB", "Noto Sans SC", system-ui, sans-serif',
  mono: 'ui-monospace, "SFMono-Regular", "JetBrains Mono", Consolas, monospace',
};

function detectDarkMode(): boolean {
  if (typeof document === "undefined") return false;
  const themeAttr = document.documentElement.dataset.theme;
  if (themeAttr === "dark") return true;
  if (themeAttr === "light") return false;
  return typeof window !== "undefined" && Boolean(window.matchMedia?.("(prefers-color-scheme: dark)")?.matches);
}

function readToken(styles: CSSStyleDeclaration | null, name: string, fallback: string): string {
  const value = styles?.getPropertyValue(name).trim();
  return value || fallback;
}

export function resolveChartTheme(): ChartTheme {
  const isDark = detectDarkMode();
  const base = isDark ? DARK_DEFAULTS : LIGHT_DEFAULTS;
  if (typeof document === "undefined" || typeof getComputedStyle !== "function") {
    return { isDark, ...base };
  }
  const styles = getComputedStyle(document.documentElement);
  return {
    isDark,
    palette: [
      readToken(styles, "--chart-1", base.palette[0]),
      readToken(styles, "--chart-2", base.palette[1]),
      readToken(styles, "--chart-3", base.palette[2]),
      readToken(styles, "--chart-4", base.palette[3]),
      readToken(styles, "--chart-5", base.palette[4]),
      readToken(styles, "--chart-6", base.palette[5]),
    ],
    onSurface: readToken(styles, "--on-surface", base.onSurface),
    onVariant: readToken(styles, "--on-variant", base.onVariant),
    onTertiary: readToken(styles, "--on-tertiary", base.onTertiary),
    separator: readToken(styles, "--separator", base.separator),
    grid: readToken(styles, "--chart-grid", base.grid),
    surface: readToken(styles, "--surface", base.surface),
    group: readToken(styles, "--group", base.group),
    warning: readToken(styles, "--warning", base.warning),
    positive: readToken(styles, "--chart-pos", base.positive),
    negative: readToken(styles, "--chart-neg", base.negative),
    total: readToken(styles, "--chart-total", base.total),
    font: readToken(styles, "--font", base.font),
    mono: readToken(styles, "--mono", base.mono),
  };
}

export function useChartTheme(): ChartTheme {
  const [theme, setTheme] = useState<ChartTheme>(() => resolveChartTheme());

  useEffect(() => {
    const sync = () => setTheme(resolveChartTheme());
    sync();

    const mq = typeof window !== "undefined" ? window.matchMedia?.("(prefers-color-scheme: dark)") : undefined;
    mq?.addEventListener?.("change", sync);

    let observer: MutationObserver | undefined;
    if (typeof MutationObserver !== "undefined" && typeof document !== "undefined") {
      observer = new MutationObserver(sync);
      observer.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["data-theme"],
      });
    }

    return () => {
      mq?.removeEventListener?.("change", sync);
      observer?.disconnect();
    };
  }, []);

  return theme;
}

/** Adds alpha to a CSS `#rgb` / `#rrggbb` or `rgb(...)` color for Canvas fills. */
export function withAlpha(color: string, alpha: number): string {
  const clamped = Math.max(0, Math.min(1, alpha));
  const trimmed = color.trim();
  if (trimmed.startsWith("#")) {
    const hex = trimmed.slice(1);
    if (hex.length === 3) {
      const r = parseInt(hex[0]! + hex[0]!, 16);
      const g = parseInt(hex[1]! + hex[1]!, 16);
      const b = parseInt(hex[2]! + hex[2]!, 16);
      if (Number.isFinite(r) && Number.isFinite(g) && Number.isFinite(b)) {
        return `rgba(${r}, ${g}, ${b}, ${clamped})`;
      }
    }
    if (hex.length === 6) {
      const r = parseInt(hex.slice(0, 2), 16);
      const g = parseInt(hex.slice(2, 4), 16);
      const b = parseInt(hex.slice(4, 6), 16);
      if (Number.isFinite(r) && Number.isFinite(g) && Number.isFinite(b)) {
        return `rgba(${r}, ${g}, ${b}, ${clamped})`;
      }
    }
  }
  const rgbMatch = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/.exec(trimmed);
  if (rgbMatch) {
    return `rgba(${rgbMatch[1]}, ${rgbMatch[2]}, ${rgbMatch[3]}, ${clamped})`;
  }
  return trimmed;
}

function csvCell(value: string): string {
  const text = String(value ?? "");
  if (!/[",\n\r]/.test(text)) return text;
  return `"${text.replace(/"/g, '""')}"`;
}

/** Standard RFC-4180 CSV export including series, X axis, exact Decimal value string, display value, and provenance. */
export function formatChartCsv(chart: ResearchV2.ResearchChart): string {
  if (chart.chart_type === "causal_graph" && chart.causal_graph) {
    const nodeLabelById = new Map((chart.causal_graph.nodes ?? []).map((n) => [n.id, n.label]));
    const header = ["起点", "方向", "终点", "传导关系", "状态", "关联结论ID"].map(csvCell).join(",");
    const rows: string[] = [header];
    for (const edge of chart.causal_graph.edges ?? []) {
      const fromLabel = nodeLabelById.get(edge.from_id) ?? edge.from_id;
      const toLabel = nodeLabelById.get(edge.to_id) ?? edge.to_id;
      const dir = CAUSAL_DIRECTION_LABEL[edge.direction ?? "uncertain"] ?? (edge.direction ?? "不确定");
      const status = edge.unverified_assumption || (edge.claim_ids?.length ?? 0) === 0 ? "未核实假设" : "已关联引证";
      rows.push([fromLabel, dir, toLabel, edge.relation, status, (edge.claim_ids ?? []).join("; ")].map(csvCell).join(","));
    }
    return rows.join("\n");
  }

  const header = ["系列", "X轴", "数值", "显示值", "来源ID", "字符范围", "是否推演"].map(csvCell).join(",");
  const rows: string[] = [header];
  for (const series of chart.series ?? []) {
    for (const point of series.points) {
      const prov = point.provenance;
      const range =
        prov.character_start != null && prov.character_end != null
          ? `${prov.character_start}-${prov.character_end}`
          : "";
      const projected = series.is_projected || point.is_projected ? "是" : "否";
      rows.push(
        [series.name, point.x, point.y, point.display_value, prov.source_id, range, projected]
          .map(csvCell)
          .join(","),
      );
    }
  }
  return rows.join("\n");
}

export type WaterfallKind = "positive" | "negative" | "total";

export interface WaterfallDatum {
  x: string;
  base: number;
  span: number;
  rawValue: number;
  prevTotal: number;
  runningTotal: number;
  kind: WaterfallKind;
  isProjected: boolean;
  point: ResearchV2.ChartPoint;
}

export function toChartNumber(raw: string): number {
  const n = Number(raw);
  return Number.isFinite(n) ? n : 0;
}

/**
 * Deterministic conversion of a single ChartSeries into stacked waterfall steps
 * (`base` transparent offset + signed/positive/negative/total bars).
 */
export function buildWaterfallSeries(series: ResearchV2.ChartSeries): WaterfallDatum[] {
  const points = series.points;
  const result: WaterfallDatum[] = [];
  let running = 0;

  for (let i = 0; i < points.length; i += 1) {
    const point = points[i]!;
    const val = toChartNumber(point.y);
    const isProjected = Boolean(series.is_projected || point.is_projected);
    const prevTotal = running;

    const isClosingTotal =
      i === points.length - 1 &&
      i >= 2 &&
      Math.abs(val - running) <= 1e-6 * Math.max(1, Math.abs(running));

    if (i === 0 || isClosingTotal) {
      running = val;
      result.push({
        x: point.x,
        base: 0,
        span: val,
        rawValue: val,
        prevTotal,
        runningTotal: running,
        kind: "total",
        isProjected,
        point,
      });
      continue;
    }

    const next = prevTotal + val;
    running = next;
    const kind: WaterfallKind = val >= 0 ? "positive" : "negative";

    let base: number;
    let span: number;
    if (prevTotal >= 0 && next >= 0) {
      base = Math.min(prevTotal, next);
      span = Math.abs(val);
    } else if (prevTotal <= 0 && next <= 0) {
      base = Math.max(prevTotal, next);
      span = -Math.abs(val);
    } else {
      // Zero-crossing transition: anchor at 0 toward the resulting cumulative side.
      base = 0;
      span = next;
    }

    result.push({
      x: point.x,
      base,
      span,
      rawValue: val,
      prevTotal,
      runningTotal: running,
      kind,
      isProjected,
      point,
    });
  }

  return result;
}

/** Formats axis tick values concisely without modifying stored chart data. */
export function formatCompactAxisTick(value: number): string {
  if (!Number.isFinite(value)) return "";
  if (Math.abs(value) >= 1_000_000) {
    return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(value);
  }
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(value);
}

/** Converts a PNG `data:` URL or canvas with solid background into a downloadable Blob via `saveBlob`. */
export async function exportPngFromDataUrl(dataUrl: string, filename: string): Promise<boolean> {
  if (!dataUrl.startsWith("data:image/png")) return false;
  try {
    const comma = dataUrl.indexOf(",");
    if (comma < 0) return false;
    const binary = atob(dataUrl.slice(comma + 1));
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    saveBlob(new Blob([bytes], { type: "image/png" }), filename);
    return true;
  } catch {
    return false;
  }
}

export function exportCanvasWithBackground(
  sourceCanvas: HTMLCanvasElement | null,
  background: string,
  filename: string,
): boolean {
  if (!sourceCanvas || sourceCanvas.width <= 0 || sourceCanvas.height <= 0) return false;
  try {
    const out = document.createElement("canvas");
    out.width = sourceCanvas.width;
    out.height = sourceCanvas.height;
    const ctx = out.getContext("2d");
    if (!ctx) return false;
    ctx.fillStyle = background || "#ffffff";
    ctx.fillRect(0, 0, out.width, out.height);
    ctx.drawImage(sourceCanvas, 0, 0);
    const dataUrl = out.toDataURL("image/png");
    void exportPngFromDataUrl(dataUrl, filename);
    return true;
  } catch {
    return false;
  }
}

const SVG_ATTRS = [
  "fill",
  "stroke",
  "stroke-width",
  "stroke-dasharray",
  "stroke-linecap",
  "opacity",
  "font-family",
  "font-size",
  "font-weight",
  "text-anchor",
  "dominant-baseline",
] as const;

/** Clones an SVG element offscreen, captures computed vector presentation attrs cleanly, and saves as a standalone `.svg` file. */
export function exportSvgElement(
  svgEl: SVGSVGElement | null,
  background: string,
  filename: string,
): boolean {
  if (!svgEl || typeof XMLSerializer === "undefined") return false;
  try {
    const clone = svgEl.cloneNode(true) as SVGSVGElement;
    clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    const origNodes = [svgEl, ...svgEl.querySelectorAll("*")];
    const cloneNodes = [clone, ...clone.querySelectorAll("*")];
    if (typeof getComputedStyle === "function") {
      for (let i = 0; i < origNodes.length && i < cloneNodes.length; i += 1) {
        const orig = origNodes[i]!;
        const target = cloneNodes[i]!;
        if (!(orig instanceof SVGElement) || !(target instanceof SVGElement)) continue;
        const computed = getComputedStyle(orig);
        for (const attr of SVG_ATTRS) {
          const val = computed.getPropertyValue(attr).trim();
          if (val && val !== "none" && val !== "normal") {
            target.setAttribute(attr, val);
          } else if (attr === "fill" && val === "none") {
            target.setAttribute("fill", "none");
          }
        }
      }
    }
    const vb = svgEl.viewBox?.baseVal;
    const width = vb?.width || Number(svgEl.getAttribute("width")) || 600;
    const height = vb?.height || Number(svgEl.getAttribute("height")) || 320;
    const bgRect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    bgRect.setAttribute("x", "0");
    bgRect.setAttribute("y", "0");
    bgRect.setAttribute("width", String(width));
    bgRect.setAttribute("height", String(height));
    bgRect.setAttribute("fill", background || "#ffffff");
    clone.insertBefore(bgRect, clone.firstChild);

    const xml = new XMLSerializer().serializeToString(clone);
    saveBlob(new Blob([xml], { type: "image/svg+xml;charset=utf-8" }), filename);
    return true;
  } catch {
    return false;
  }
}
