import type { ResearchV2 } from "@foresightnine/contracts";
import { CAUSAL_DIRECTION_LABEL } from "../../../lib/copy";
import { Sparkline } from "../../../ui/charts/MicroGraphics";
import { Icon } from "../../../ui/icons";
import { toChartNumber } from "./chartTheme";

interface ChartDataTableProps {
  chart: ResearchV2.ResearchChart;
  sourceTitle: (sourceId: string) => string;
  onSelectProvenance: (provenance: ResearchV2.ChartProvenance) => void;
  onSelectClaim?: ((claimId: string) => void) | undefined;
  claimText?: ((claimId: string) => string | undefined) | undefined;
}

function pointRangeLabel(prov: ResearchV2.ChartProvenance, sourceTitle: (sourceId: string) => string): string {
  const title = sourceTitle(prov.source_id);
  if (prov.character_start != null && prov.character_end != null) {
    return `${title}（第 ${prov.character_start}–${prov.character_end} 字符）`;
  }
  return title;
}

function seriesTrendTone(points: readonly ResearchV2.ChartPoint[]): "positive" | "negative" | "neutral" {
  if (points.length < 2) return "neutral";
  const first = toChartNumber(points[0]!.y);
  const last = toChartNumber(points[points.length - 1]!.y);
  if (last > first) return "positive";
  if (last < first) return "negative";
  return "neutral";
}

function SeriesHeaderWithSpark({ series }: { series: ResearchV2.ChartSeries }) {
  const showSpark = series.points.length >= 2;
  return (
    <span className="chart-th-head">
      <span>{series.name}</span>
      {series.is_projected ? <span className="chart-proj-pill">推演</span> : null}
      {showSpark ? (
        <Sparkline
          values={series.points.map((pt) => toChartNumber(pt.y))}
          tone={seriesTrendTone(series.points)}
          ariaLabel={`${series.name} 趋势`}
          width={44}
          height={14}
        />
      ) : null}
    </span>
  );
}

export function ChartDataTable({
  chart,
  sourceTitle,
  onSelectProvenance,
  onSelectClaim,
  claimText,
}: ChartDataTableProps) {
  if (chart.chart_type === "causal_graph" && chart.causal_graph) {
    const nodesById = new Map((chart.causal_graph.nodes ?? []).map((n) => [n.id, n]));
    const edges = chart.causal_graph.edges ?? [];
    return (
      <div className="chart-table-wrap">
        <table className="chart-table" aria-label={`${chart.title} 数据表`}>
          <thead>
            <tr>
              <th scope="col">起点</th>
              <th scope="col">方向</th>
              <th scope="col">终点</th>
              <th scope="col">传导关系</th>
              <th scope="col">依据 / 状态</th>
            </tr>
          </thead>
          <tbody>
            {edges.map((edge, idx) => {
              const fromLabel = nodesById.get(edge.from_id)?.label ?? edge.from_id;
              const toLabel = nodesById.get(edge.to_id)?.label ?? edge.to_id;
              const dir = edge.direction ?? "uncertain";
              const dirLabel = CAUSAL_DIRECTION_LABEL[dir] ?? dir;
              const claimIds = edge.claim_ids ?? [];
              const unverified = Boolean(edge.unverified_assumption || claimIds.length === 0);
              return (
                <tr key={`${edge.from_id}-${edge.to_id}-${idx}`}>
                  <td>{fromLabel}</td>
                  <td>{dirLabel}</td>
                  <td>{toLabel}</td>
                  <td>{edge.relation}</td>
                  <td>
                    {unverified ? (
                      <span className="chart-proj-pill">未核实假设</span>
                    ) : (
                      <div className="cg-table-claims">
                        {claimIds.map((cid, cIdx) => {
                          const text = claimText?.(cid);
                          return (
                            <button
                              key={cid}
                              type="button"
                              className="link chart-src-btn"
                              title={text || `跳转结论 ${cid}`}
                              onClick={() => onSelectClaim?.(cid)}
                            >
                              <span>{text ? `${text.slice(0, 18)}${text.length > 18 ? "…" : ""}` : `结论 #${cIdx + 1}`}</span>
                              <Icon name="chev" size={13} />
                            </button>
                          );
                        })}
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  }

  const seriesList = chart.series ?? [];
  const singleSeries = seriesList.length <= 1;

  if (singleSeries) {
    const series = seriesList[0];
    const points = series?.points ?? [];
    return (
      <div className="chart-table-wrap">
        <table className="chart-table" aria-label={`${chart.title} 数据表`}>
          <thead>
            <tr>
              <th scope="col">{chart.x_label || "类别 / 时间"}</th>
              <th scope="col" className="md-num">
                {series ? <SeriesHeaderWithSpark series={series} /> : "数值"}
              </th>
              <th scope="col">原文依据</th>
            </tr>
          </thead>
          <tbody>
            {points.map((point, idx) => {
              const projected = Boolean(series?.is_projected || point.is_projected);
              const label = pointRangeLabel(point.provenance, sourceTitle);
              return (
                <tr key={`${point.x}-${idx}`}>
                  <td>{point.x}</td>
                  <td className="md-num">
                    <span className="chart-cell-val">{point.display_value}</span>
                    {projected ? <span className="chart-proj-pill">推演</span> : null}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="link chart-src-btn"
                      title={label}
                      onClick={() => onSelectProvenance(point.provenance)}
                    >
                      <span>{sourceTitle(point.provenance.source_id)}</span>
                      <Icon name="chev" size={13} />
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  }

  // Multi-series: pivot by X when X keys are unique per series, otherwise render flat rows so no point is dropped.
  const hasDupX = seriesList.some((s) => new Set(s.points.map((p) => p.x)).size !== s.points.length);
  if (hasDupX) {
    return (
      <div className="chart-table-wrap">
        <table className="chart-table" aria-label={`${chart.title} 数据表`}>
          <thead>
            <tr>
              <th scope="col">系列</th>
              <th scope="col">{chart.x_label || "类别 / 时间"}</th>
              <th scope="col" className="md-num">数值</th>
              <th scope="col">原文依据</th>
            </tr>
          </thead>
          <tbody>
            {seriesList.flatMap((series) =>
              series.points.map((point, idx) => {
                const projected = Boolean(series.is_projected || point.is_projected);
                const label = pointRangeLabel(point.provenance, sourceTitle);
                return (
                  <tr key={`${series.name}-${point.x}-${idx}`}>
                    <td>{series.name}</td>
                    <td>{point.x}</td>
                    <td className="md-num">
                      <span className="chart-cell-val">{point.display_value}</span>
                      {projected ? <span className="chart-proj-pill">推演</span> : null}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="link chart-src-btn"
                        title={label}
                        onClick={() => onSelectProvenance(point.provenance)}
                      >
                        <span>{sourceTitle(point.provenance.source_id)}</span>
                        <Icon name="chev" size={13} />
                      </button>
                    </td>
                  </tr>
                );
              }),
            )}
          </tbody>
        </table>
      </div>
    );
  }

  const xKeys: string[] = [];
  const xSeen = new Set<string>();
  const seriesMaps = seriesList.map((series) => {
    const map = new Map<string, ResearchV2.ChartPoint>();
    for (const point of series.points) {
      if (!xSeen.has(point.x)) {
        xSeen.add(point.x);
        xKeys.push(point.x);
      }
      if (!map.has(point.x)) map.set(point.x, point);
    }
    return { series, map };
  });

  return (
    <div className="chart-table-wrap">
      <table className="chart-table" aria-label={`${chart.title} 数据表`}>
        <thead>
          <tr>
            <th scope="col">{chart.x_label || "类别 / 时间"}</th>
            {seriesList.map((series) => (
              <th key={series.name} scope="col" className="md-num">
                <SeriesHeaderWithSpark series={series} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {xKeys.map((xVal) => (
            <tr key={xVal}>
              <td>{xVal}</td>
              {seriesMaps.map(({ series, map }) => {
                const point = map.get(xVal);
                if (!point) {
                  return <td key={series.name} className="md-num meta">—</td>;
                }
                const projected = Boolean(series.is_projected || point.is_projected);
                const label = pointRangeLabel(point.provenance, sourceTitle);
                return (
                  <td key={series.name} className="md-num">
                    <button
                      type="button"
                      className="chart-cell-btn"
                      title={`点击查看原文依据：${label}`}
                      onClick={() => onSelectProvenance(point.provenance)}
                    >
                      <span>{point.display_value}</span>
                      {projected ? <span className="chart-proj-pill">推演</span> : null}
                    </button>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
