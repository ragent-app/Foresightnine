import type { ResearchV2 } from "@foresightnine/contracts";
import { useId, useMemo, useState, type KeyboardEvent, type RefObject } from "react";
import {
  CAUSAL_CATEGORY_LABEL,
  CAUSAL_DIRECTION_LABEL,
  CAUSAL_STATUS_LABEL,
} from "../../../lib/copy";
import { Icon } from "../../../ui/icons";
import { computeCausalGraphLayout, type LayoutEdge, type LayoutNode } from "./causalGraphLayout";

type Selection =
  | { kind: "node"; id: string }
  | { kind: "edge"; index: number }
  | null;

interface CausalGraphViewProps {
  graph: ResearchV2.CausalGraphSpec;
  title: string;
  onSelectClaim?: ((claimId: string) => void) | undefined;
  claimText?: ((claimId: string) => string | undefined) | undefined;
  svgRef?: RefObject<SVGSVGElement>;
  modal?: boolean;
}

function handleEnterOrSpace(event: KeyboardEvent, action: () => void): void {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    action();
  }
}

export function CausalGraphView({
  graph,
  title,
  onSelectClaim,
  claimText,
  svgRef,
  modal = false,
}: CausalGraphViewProps) {
  const uid = useId().replace(/:/g, "");
  const layout = useMemo(() => computeCausalGraphLayout(graph), [graph]);
  const [selected, setSelected] = useState<Selection>(null);

  const selectedNode: LayoutNode | null =
    selected?.kind === "node" ? layout.nodeById.get(selected.id) ?? null : null;
  const selectedEdge: LayoutEdge | null =
    selected?.kind === "edge" ? layout.edges.find((e) => e.index === selected.index) ?? null : null;

  const relatedNodeClaimIds = useMemo(() => {
    if (!selectedNode) return [];
    const seen = new Set<string>();
    const ids: string[] = [];
    for (const item of layout.edges) {
      if (item.edge.from_id !== selectedNode.node.id && item.edge.to_id !== selectedNode.node.id) continue;
      for (const cid of item.edge.claim_ids ?? []) {
        if (seen.has(cid)) continue;
        seen.add(cid);
        ids.push(cid);
      }
    }
    return ids;
  }, [layout.edges, selectedNode]);

  const markerId = (dir: string, isSelected: boolean) =>
    isSelected ? `${uid}-arrow-selected` : `${uid}-arrow-${dir}`;

  const activateNode = (nodeId: string) => {
    setSelected({ kind: "node", id: nodeId });
  };

  const activateEdge = (item: LayoutEdge) => {
    setSelected({ kind: "edge", index: item.index });
    const claims = item.edge.claim_ids ?? [];
    if (claims.length === 1 && !item.edge.unverified_assumption) {
      onSelectClaim?.(claims[0]!);
    }
  };

  const renderClaimJumpButtons = (claimIds: readonly string[]) => {
    if (claimIds.length === 0) return null;
    return (
      <div className="cg-detail-claims">
        <span className="meta">关联结论：</span>
        {claimIds.map((cid, idx) => {
          const text = claimText?.(cid);
          return (
            <button
              key={cid}
              type="button"
              className="cg-claim-btn"
              title={text || `跳转至结论 ${cid}`}
              onClick={() => onSelectClaim?.(cid)}
            >
              <span>{text ? `结论：${text.slice(0, 26)}${text.length > 26 ? "…" : ""}` : `定位结论 #${idx + 1} (${cid})`}</span>
              <Icon name="chev" size={12} />
            </button>
          );
        })}
      </div>
    );
  };

  return (
    <div className={`cg-wrap${modal ? " is-modal" : ""}`} data-chart-engine="causal-svg">
      <div className="cg-legend" aria-label="传导图图例">
        <span className="cg-legend-item">
          <span className="cg-swatch is-confirmed" aria-hidden="true" />
          已核实节点
        </span>
        <span className="cg-legend-item">
          <span className="cg-swatch is-assumption" aria-hidden="true" />
          假设节点
        </span>
        <span className="cg-legend-item">
          <span className="cg-swatch is-pos" aria-hidden="true" />
          正向
        </span>
        <span className="cg-legend-item">
          <span className="cg-swatch is-neg" aria-hidden="true" />
          负向
        </span>
        <span className="cg-legend-item">
          <span className="cg-swatch is-cond" aria-hidden="true" />
          条件 / 不确定
        </span>
      </div>

      <div className="cg-scroll">
        <svg
          ref={svgRef}
          className="cg-svg"
          width={layout.width}
          height={layout.height}
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          role="img"
          aria-label={`${title} 逻辑传导图`}
        >
          <title>{title}</title>
          <defs>
            {(["positive", "negative", "conditional", "uncertain", "selected"] as const).map((kind) => (
              <marker
                key={kind}
                id={`${uid}-arrow-${kind}`}
                viewBox="0 0 10 10"
                refX="8.5"
                refY="5"
                markerWidth="6.5"
                markerHeight="6.5"
                orient="auto-start-reverse"
              >
                <path d="M 0 1.5 L 9 5 L 0 8.5 z" className={`cg-arrow-head is-${kind}`} />
              </marker>
            ))}
          </defs>

          <g className="cg-edges">
            {layout.edges.map((item) => {
              const dir = item.edge.direction ?? "uncertain";
              const isSelected = selected?.kind === "edge" && selected.index === item.index;
              const isUnverified = Boolean(item.edge.unverified_assumption || (item.edge.claim_ids?.length ?? 0) === 0);
              const claimCount = item.edge.claim_ids?.length ?? 0;
              const edgeLabel = `${item.from.node.label} ${CAUSAL_DIRECTION_LABEL[dir] ?? dir} ${item.to.node.label}：${item.edge.relation}${isUnverified ? "（未核实假设）" : ""}`;
              const edgeClasses = [
                "cg-edge",
                `dir-${dir}`,
                isUnverified ? "is-unverified" : "is-verified",
                isSelected ? "is-selected" : "",
              ]
                .filter(Boolean)
                .join(" ");

              return (
                <g
                  key={`${item.edge.from_id}-${item.edge.to_id}-${item.index}`}
                  className={edgeClasses}
                  role="button"
                  tabIndex={0}
                  aria-label={edgeLabel}
                  aria-pressed={isSelected}
                  onClick={() => activateEdge(item)}
                  onKeyDown={(event) => handleEnterOrSpace(event, () => activateEdge(item))}
                >
                  <title>{edgeLabel}</title>
                  <path className="cg-edge-hit" d={item.path} fill="none" />
                  <path
                    className="cg-edge-line"
                    d={item.path}
                    fill="none"
                    markerEnd={`url(#${markerId(dir, isSelected)})`}
                  />
                  <g className="cg-edge-pill-group">
                    <rect
                      className="cg-edge-pill-bg"
                      x={item.labelX - 54}
                      y={item.labelY - 10}
                      width={108}
                      height={20}
                      rx={6}
                    />
                    <text
                      className="cg-edge-pill-text"
                      x={item.labelX}
                      y={item.labelY + 3.5}
                      textAnchor="middle"
                    >
                      {item.shortLabel}
                    </text>
                    {!isUnverified && claimCount > 0 ? (
                      <text
                        className="cg-edge-claim-count"
                        x={item.labelX}
                        y={item.labelY + 20}
                        textAnchor="middle"
                      >
                        {`引证 ${claimCount}`}
                      </text>
                    ) : isUnverified ? (
                      <text
                        className="cg-edge-unverified-tag"
                        x={item.labelX}
                        y={item.labelY + 20}
                        textAnchor="middle"
                      >
                        假设
                      </text>
                    ) : null}
                  </g>
                </g>
              );
            })}
          </g>

          <g className="cg-nodes">
            {layout.nodes.map((item) => {
              const status = item.node.status ?? "assumption";
              const catLabel = CAUSAL_CATEGORY_LABEL[item.node.category] ?? item.node.category;
              const statusLabel = CAUSAL_STATUS_LABEL[status] ?? status;
              const isSelected = selected?.kind === "node" && selected.id === item.node.id;
              const nodeClasses = [
                "cg-node",
                `cat-${item.node.category}`,
                `status-${status}`,
                isSelected ? "is-selected" : "",
              ]
                .filter(Boolean)
                .join(" ");
              const ariaLabel = `${catLabel} · ${item.node.label}（${statusLabel}）${item.node.detail ? `：${item.node.detail}` : ""}`;

              return (
                <g
                  key={item.node.id}
                  className={nodeClasses}
                  role="button"
                  tabIndex={0}
                  aria-label={ariaLabel}
                  aria-pressed={isSelected}
                  onClick={() => activateNode(item.node.id)}
                  onKeyDown={(event) => handleEnterOrSpace(event, () => activateNode(item.node.id))}
                >
                  <title>{ariaLabel}</title>
                  <rect
                    className="cg-node-box"
                    x={item.x}
                    y={item.y}
                    width={item.width}
                    height={item.height}
                    rx={10}
                  />
                  <text className="cg-node-cat" x={item.x + 10} y={item.y + 16}>
                    {catLabel}
                  </text>
                  {status !== "confirmed" ? (
                    <text className="cg-node-status" x={item.x + item.width - 10} y={item.y + 16} textAnchor="end">
                      {statusLabel}
                    </text>
                  ) : null}
                  {item.lines.map((line, lineIdx) => (
                    <text
                      key={lineIdx}
                      className="cg-node-label"
                      x={item.x + 10}
                      y={item.y + 34 + lineIdx * 16}
                    >
                      {line}
                    </text>
                  ))}
                </g>
              );
            })}
          </g>
        </svg>
      </div>

      <div className="cg-detail" aria-live="polite">
        {selectedNode ? (
          <div className="cg-detail-body">
            <div className="cg-detail-head">
              <span className="cg-detail-badge">
                {CAUSAL_CATEGORY_LABEL[selectedNode.node.category] ?? selectedNode.node.category}
              </span>
              <strong className="cg-detail-title">{selectedNode.node.label}</strong>
              <span className={`chart-badge ${selectedNode.node.status === "confirmed" ? "is-updated" : "is-proj"}`}>
                {CAUSAL_STATUS_LABEL[selectedNode.node.status ?? "assumption"] ?? selectedNode.node.status}
              </span>
            </div>
            {selectedNode.node.detail ? <p className="cg-detail-note">{selectedNode.node.detail}</p> : null}
            {renderClaimJumpButtons(relatedNodeClaimIds)}
          </div>
        ) : selectedEdge ? (
          <div className="cg-detail-body">
            <div className="cg-detail-head">
              <span className="cg-detail-badge">
                {CAUSAL_DIRECTION_LABEL[selectedEdge.edge.direction ?? "uncertain"] ?? selectedEdge.edge.direction}
              </span>
              <strong className="cg-detail-title">
                {`${selectedEdge.from.node.label} → ${selectedEdge.to.node.label}`}
              </strong>
              {selectedEdge.edge.unverified_assumption || (selectedEdge.edge.claim_ids?.length ?? 0) === 0 ? (
                <span className="chart-badge is-proj">未核实假设</span>
              ) : (
                <span className="chart-badge is-updated">已关联引证</span>
              )}
            </div>
            <p className="cg-detail-note">{selectedEdge.edge.relation}</p>
            {renderClaimJumpButtons(selectedEdge.edge.claim_ids ?? [])}
          </div>
        ) : (
          <span className="meta">点击或按回车选中节点或连线，可查看条件说明并跳转至对应结论</span>
        )}
      </div>
    </div>
  );
}
