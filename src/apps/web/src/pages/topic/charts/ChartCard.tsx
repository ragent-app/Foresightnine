import type { ResearchV2 } from "@foresightnine/contracts";
import * as Dialog from "@radix-ui/react-dialog";
import {
  Component,
  Suspense,
  lazy,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { saveBlob } from "../../../lib/download";
import { Icon } from "../../../ui/icons";
import { TopicContext } from "../context";
import { CausalGraphView } from "./CausalGraphView";
import { ChartDataTable } from "./ChartDataTable";
import {
  exportPngFromDataUrl,
  exportSvgElement,
  formatChartCsv,
  useChartTheme,
  type ChartTheme,
} from "./chartTheme";
import type { ChartCanvasHandle } from "./PriceSeriesChart";

const LazyPriceSeriesChart = lazy(() => import("./PriceSeriesChart"));
const LazyEChartsCanvasChart = lazy(() => import("./EChartsCanvasChart"));

interface ChartCardProps {
  chart: ResearchV2.ResearchChart;
  onSelectProvenance?: (provenance: ResearchV2.ChartProvenance) => void;
  onSelectClaim?: (claimId: string) => void;
  sourceTitle?: (sourceId: string) => string;
}

type ViewMode = "chart" | "table";

class ChartErrorBoundary extends Component<
  { fallback: ReactNode; children: ReactNode },
  { hasError: boolean }
> {
  override state = { hasError: false };
  static getDerivedStateFromError(): { hasError: boolean } {
    return { hasError: true };
  }
  override render() {
    if (this.state.hasError) return this.props.fallback;
    return this.props.children;
  }
}

function safeFileSlug(title: string, chartId: string): string {
  const cleaned = title
    .trim()
    .replace(/[\\/:*?"<>|\s]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 48);
  return cleaned || chartId;
}

function ChartCanvasBody({
  chart,
  theme,
  size,
  canvasRef,
  sourceTitle,
  onSelectProvenance,
}: {
  chart: ResearchV2.ResearchChart;
  theme: ChartTheme;
  size: "card" | "modal";
  canvasRef: React.RefObject<ChartCanvasHandle>;
  sourceTitle: (sourceId: string) => string;
  onSelectProvenance: (provenance: ResearchV2.ChartProvenance) => void;
}) {
  const fallbackTable = (
    <ChartDataTable
      chart={chart}
      sourceTitle={sourceTitle}
      onSelectProvenance={onSelectProvenance}
    />
  );

  return (
    <ChartErrorBoundary fallback={fallbackTable}>
      <Suspense fallback={<div className="chart-skeleton">正在加载图表引擎…</div>}>
        {chart.chart_type === "price_series" ? (
          <LazyPriceSeriesChart
            ref={canvasRef}
            chart={chart}
            theme={theme}
            size={size}
            onSelectProvenance={onSelectProvenance}
          />
        ) : (
          <LazyEChartsCanvasChart
            ref={canvasRef}
            chart={chart}
            theme={theme}
            size={size}
            onSelectProvenance={onSelectProvenance}
          />
        )}
      </Suspense>
    </ChartErrorBoundary>
  );
}

export function ChartCard({
  chart,
  onSelectProvenance,
  onSelectClaim,
  sourceTitle: sourceTitleProp,
}: ChartCardProps) {
  const view = useContext(TopicContext);
  const theme = useChartTheme();
  const [mode, setMode] = useState<ViewMode>("chart");
  const [zoomOpen, setZoomOpen] = useState(false);
  const cardCanvasRef = useRef<ChartCanvasHandle>(null);
  const modalCanvasRef = useRef<ChartCanvasHandle>(null);
  const cardSvgRef = useRef<SVGSVGElement>(null);
  const modalSvgRef = useRef<SVGSVGElement>(null);

  const isCausalGraph = chart.chart_type === "causal_graph" && Boolean(chart.causal_graph);

  const resolveSourceTitle = useMemo(
    () => sourceTitleProp ?? view?.sourceTitle ?? ((id: string) => id),
    [sourceTitleProp, view],
  );

  const claimById = useMemo(() => {
    const map = new Map<string, string>();
    for (const claim of view?.content.claims ?? []) {
      if (claim.claim_id) map.set(claim.claim_id, claim.text);
    }
    return map;
  }, [view?.content.claims]);

  const resolveClaimText = useCallback((claimId: string) => claimById.get(claimId), [claimById]);

  const handleSelectProvenance = useCallback(
    (prov: ResearchV2.ChartProvenance) => {
      setZoomOpen(false);
      if (onSelectProvenance) {
        onSelectProvenance(prov);
        return;
      }
      const range =
        prov.character_start != null && prov.character_end != null && prov.character_end > prov.character_start
          ? ([prov.character_start, prov.character_end] as const)
          : null;
      view?.openPanel({ kind: "reader", sourceId: prov.source_id, range });
    },
    [onSelectProvenance, view],
  );

  const handleSelectClaim = useCallback(
    (claimId: string) => {
      setZoomOpen(false);
      if (onSelectClaim) {
        onSelectClaim(claimId);
        return;
      }
      view?.gotoClaim(claimId);
    },
    [onSelectClaim, view],
  );

  const handleExportCsv = useCallback(() => {
    const csv = `\uFEFF${formatChartCsv(chart)}`;
    saveBlob(new Blob([csv], { type: "text/csv;charset=utf-8" }), `${safeFileSlug(chart.title, chart.chart_id)}.csv`);
  }, [chart]);

  const handleExportPng = useCallback(
    async (preferModal: boolean) => {
      const target = (preferModal ? modalCanvasRef.current : cardCanvasRef.current) ?? cardCanvasRef.current;
      const dataUrl = target?.getPngDataUrl() ?? null;
      if (!dataUrl) {
        view?.notify("图表画布尚未就绪，可先导出 CSV 明细。", "info");
        return;
      }
      await exportPngFromDataUrl(dataUrl, `${safeFileSlug(chart.title, chart.chart_id)}.png`);
    },
    [chart.chart_id, chart.title, view],
  );

  const handleExportSvg = useCallback(
    (preferModal: boolean) => {
      const target = (preferModal ? modalSvgRef.current : cardSvgRef.current) ?? cardSvgRef.current;
      const ok = exportSvgElement(target, theme.surface, `${safeFileSlug(chart.title, chart.chart_id)}.svg`);
      if (!ok) {
        view?.notify("矢量图尚未就绪，可先导出 CSV 明细。", "info");
      }
    },
    [chart.chart_id, chart.title, theme.surface, view],
  );

  const sourceLabels = useMemo(() => {
    const ids = chart.source_ids && chart.source_ids.length > 0
      ? chart.source_ids
      : [...new Set((chart.series ?? []).flatMap((s) => s.points.map((p) => p.provenance.source_id)))];
    return ids.map((id) => resolveSourceTitle(id));
  }, [chart.series, chart.source_ids, resolveSourceTitle]);

  const renderGraphic = (size: "card" | "modal") => {
    if (isCausalGraph) {
      return (
        <CausalGraphView
          graph={chart.causal_graph!}
          title={chart.title}
          svgRef={size === "modal" ? modalSvgRef : cardSvgRef}
          onSelectClaim={handleSelectClaim}
          claimText={resolveClaimText}
          modal={size === "modal"}
        />
      );
    }
    return (
      <ChartCanvasBody
        chart={chart}
        theme={theme}
        size={size}
        canvasRef={size === "modal" ? modalCanvasRef : cardCanvasRef}
        sourceTitle={resolveSourceTitle}
        onSelectProvenance={handleSelectProvenance}
      />
    );
  };

  return (
    <>
      <figure className="chart-card" role="figure" aria-label={chart.title}>
        <div className="chart-head">
          <div className="chart-title-wrap">
            <div className="chart-title-row">
              <h4 className="chart-title">{chart.title}</h4>
              {chart.is_simulated ? <span className="chart-badge is-sim">模拟数据</span> : null}
              {chart.has_projected_points ? (
                <span className="chart-badge is-proj">{isCausalGraph ? "含假设分支" : "含推演点"}</span>
              ) : null}
              {chart.replaces_chart_id ? <span className="chart-badge is-updated">已更新</span> : null}
            </div>
            {chart.subtitle ? <p className="chart-subtitle">{chart.subtitle}</p> : null}
          </div>

          <div className="chart-tools" role="toolbar" aria-label={`${chart.title} 图表操作`}>
            <div className="chart-seg" role="group" aria-label="视图切换">
              <button
                type="button"
                className="chart-seg-btn"
                aria-pressed={mode === "chart"}
                onClick={() => setMode("chart")}
              >
                <Icon name="barChart" size={13} />
                <span>图表</span>
              </button>
              <button
                type="button"
                className="chart-seg-btn"
                aria-pressed={mode === "table"}
                onClick={() => setMode("table")}
              >
                <Icon name="table" size={13} />
                <span>数据表</span>
              </button>
            </div>

            <button
              type="button"
              className="chart-tool-btn"
              aria-label="放大查看"
              title="放大查看"
              onClick={() => setZoomOpen(true)}
            >
              <Icon name="maximize" size={13} />
              <span className="chart-tool-label">放大</span>
            </button>

            {isCausalGraph ? (
              <button
                type="button"
                className="chart-tool-btn"
                aria-label="导出 SVG"
                title={mode === "chart" ? "导出 SVG 矢量图" : "切换到图表视图可导出 SVG"}
                disabled={mode !== "chart"}
                onClick={() => handleExportSvg(false)}
              >
                <Icon name="download" size={13} />
                <span>SVG</span>
              </button>
            ) : (
              <button
                type="button"
                className="chart-tool-btn"
                aria-label="导出 PNG"
                title={mode === "chart" ? "导出 PNG 图片" : "切换到图表视图可导出 PNG"}
                disabled={mode !== "chart"}
                onClick={() => void handleExportPng(false)}
              >
                <Icon name="download" size={13} />
                <span>PNG</span>
              </button>
            )}

            <button
              type="button"
              className="chart-tool-btn"
              aria-label="导出 CSV"
              title="导出 CSV 数据明细"
              onClick={handleExportCsv}
            >
              <Icon name="download" size={13} />
              <span>CSV</span>
            </button>
          </div>
        </div>

        <div className="chart-body">
          {mode === "chart" ? (
            renderGraphic("card")
          ) : (
            <ChartDataTable
              chart={chart}
              sourceTitle={resolveSourceTitle}
              onSelectProvenance={handleSelectProvenance}
              onSelectClaim={handleSelectClaim}
              claimText={resolveClaimText}
            />
          )}
        </div>

        <figcaption className="chart-foot">
          {sourceLabels.length > 0 ? (
            <span className="meta">来源：{sourceLabels.join("、")}</span>
          ) : null}
          <span className="meta chart-foot-hint">
            {isCausalGraph ? "点击节点或连线可查看条件并跳转结论" : "点击数据点或表格行可定位原文引文"}
          </span>
        </figcaption>
      </figure>

      {zoomOpen ? (
        <Dialog.Root open onOpenChange={(open) => { if (!open) setZoomOpen(false); }}>
          <Dialog.Portal>
            {/* Avoid Dialog.Overlay because react-remove-scroll injects <style> blocked by strict CSP. */}
            <div className="scrim chart-scrim" aria-hidden="true" onClick={() => setZoomOpen(false)} />
            <Dialog.Content className="chart-modal glass" aria-describedby={undefined}>
              <div className="chart-modal-head">
                <div className="chart-title-wrap">
                  <div className="chart-title-row">
                    <Dialog.Title asChild>
                      <h3 className="chart-modal-title">{chart.title}</h3>
                    </Dialog.Title>
                    {chart.is_simulated ? <span className="chart-badge is-sim">模拟数据</span> : null}
                    {chart.has_projected_points ? (
                      <span className="chart-badge is-proj">{isCausalGraph ? "含假设分支" : "含推演点"}</span>
                    ) : null}
                  </div>
                  {chart.subtitle ? <p className="chart-subtitle">{chart.subtitle}</p> : null}
                </div>
                <div className="chart-modal-actions">
                  {isCausalGraph ? (
                    <button
                      type="button"
                      className="chart-tool-btn"
                      onClick={() => handleExportSvg(true)}
                    >
                      <Icon name="download" size={13} />
                      <span>SVG</span>
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="chart-tool-btn"
                      onClick={() => void handleExportPng(true)}
                    >
                      <Icon name="download" size={13} />
                      <span>PNG</span>
                    </button>
                  )}
                  <button
                    type="button"
                    className="chart-tool-btn"
                    onClick={handleExportCsv}
                  >
                    <Icon name="download" size={13} />
                    <span>CSV</span>
                  </button>
                  <button
                    type="button"
                    className="icon-btn close"
                    aria-label="关闭放大视图"
                    onClick={() => setZoomOpen(false)}
                  >
                    <Icon name="close" size={16} />
                  </button>
                </div>
              </div>
              <div className="chart-modal-body">{renderGraphic("modal")}</div>
              <div className="chart-foot">
                {sourceLabels.length > 0 ? <span className="meta">来源：{sourceLabels.join("、")}</span> : null}
                <span className="meta chart-foot-hint">
                  {isCausalGraph ? "点击节点或连线可定位对应结论" : "点击图形数据点可直接打开原文对照"}
                </span>
              </div>
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
      ) : null}
    </>
  );
}
