import type { ResearchV2 } from "@foresightnine/contracts";

export const CAUSAL_CATEGORY_ORDER: readonly ResearchV2.CausalGraphNode["category"][] = [
  "macro",
  "industry",
  "company",
  "asset",
  "scenario",
];

export const CAUSAL_NODE_WIDTH = 168;
export const CAUSAL_NODE_HEIGHT = 70;
const COL_GAP = 72;
const ROW_GAP = 24;
const PAD_X = 20;
const PAD_Y = 20;

export interface LayoutNode {
  node: ResearchV2.CausalGraphNode;
  col: number;
  row: number;
  x: number;
  y: number;
  width: number;
  height: number;
  cx: number;
  cy: number;
  lines: readonly string[];
}

export interface LayoutEdge {
  index: number;
  edge: ResearchV2.CausalGraphEdge;
  from: LayoutNode;
  to: LayoutNode;
  path: string;
  labelX: number;
  labelY: number;
  shortLabel: string;
}

export interface CausalGraphLayout {
  width: number;
  height: number;
  nodes: readonly LayoutNode[];
  edges: readonly LayoutEdge[];
  nodeById: ReadonlyMap<string, LayoutNode>;
}

function charDisplayWidth(ch: string): number {
  return /[\u2E80-\u9FFF\uF900-\uFAFF\uFE30-\uFE4F\uFF00-\uFFEF]/.test(ch) ? 2 : 1;
}

/** Deterministic two-line label wrapper preserving readable CJK and Latin widths inside fixed SVG node boxes. */
export function wrapCausalLabel(raw: string, maxUnitsPerLine = 20, maxLines = 2): string[] {
  const clean = raw.trim().replace(/\s+/g, " ");
  if (!clean) return [""];
  const lines: string[] = [];
  let current = "";
  let units = 0;

  const chars = [...clean];
  for (let i = 0; i < chars.length; i += 1) {
    const ch = chars[i]!;
    const w = charDisplayWidth(ch);
    if (units + w > maxUnitsPerLine) {
      if (lines.length + 1 >= maxLines) {
        // Reserve room for ellipsis on final line.
        while (current && [...current].reduce((s, c) => s + charDisplayWidth(c), 0) > maxUnitsPerLine - 2) {
          current = [...current].slice(0, -1).join("");
        }
        lines.push(`${current.trimEnd()}…`);
        return lines;
      }
      lines.push(current.trimEnd());
      current = ch === " " ? "" : ch;
      units = ch === " " ? 0 : w;
    } else {
      current += ch;
      units += w;
    }
  }
  if (current) lines.push(current.trimEnd());
  return lines;
}

function truncateLabel(raw: string, maxUnits = 16): string {
  const wrapped = wrapCausalLabel(raw, maxUnits, 1);
  return wrapped[0] ?? raw;
}

function cubicPointAtHalf(
  p0: number,
  c1: number,
  c2: number,
  p1: number,
): number {
  return 0.125 * p0 + 0.375 * c1 + 0.375 * c2 + 0.125 * p1;
}

/**
 * Deterministic, jitter-free layered layout ordered by category columns
 * (`macro -> industry -> company -> asset -> scenario`) and intra-category topology.
 */
export function computeCausalGraphLayout(spec: ResearchV2.CausalGraphSpec): CausalGraphLayout {
  const nodes = spec.nodes ?? [];
  const edges = spec.edges ?? [];
  if (nodes.length === 0) {
    return { width: 320, height: 140, nodes: [], edges: [], nodeById: new Map() };
  }

  const indexById = new Map<string, number>();
  const nodeMeta = nodes.map((node, index) => {
    indexById.set(node.id, index);
    return {
      node,
      index,
      catRank: Math.max(0, CAUSAL_CATEGORY_ORDER.indexOf(node.category)),
      intraDepth: 0,
    };
  });

  // Compute deterministic intra-category sub-column depth (strictly forward by index to stay cycle-safe).
  for (let pass = 0; pass < nodes.length; pass += 1) {
    let changed = false;
    for (const edge of edges) {
      const uIdx = indexById.get(edge.from_id);
      const vIdx = indexById.get(edge.to_id);
      if (uIdx === undefined || vIdx === undefined || uIdx === vIdx) continue;
      const u = nodeMeta[uIdx]!;
      const v = nodeMeta[vIdx]!;
      if (u.catRank === v.catRank && uIdx < vIdx && v.intraDepth <= u.intraDepth) {
        v.intraDepth = Math.min(nodes.length - 1, u.intraDepth + 1);
        changed = true;
      }
    }
    if (!changed) break;
  }

  const colKeys: string[] = [];
  const colKeySet = new Set<string>();
  const sortedMeta = [...nodeMeta].sort(
    (a, b) => a.catRank - b.catRank || a.intraDepth - b.intraDepth || a.index - b.index,
  );
  for (const item of sortedMeta) {
    const key = `${item.catRank}:${item.intraDepth}`;
    if (!colKeySet.has(key)) {
      colKeySet.add(key);
      colKeys.push(key);
    }
  }
  const colIndexByKey = new Map(colKeys.map((key, idx) => [key, idx]));

  const columns: (typeof nodeMeta[number])[][] = Array.from({ length: colKeys.length }, () => []);
  const colOfNode = new Map<string, number>();
  for (const item of sortedMeta) {
    const col = colIndexByKey.get(`${item.catRank}:${item.intraDepth}`) ?? 0;
    columns[col]!.push(item);
    colOfNode.set(item.node.id, col);
  }

  // Track predecessor list per node for deterministic barycenter ordering left-to-right.
  const predsById = new Map<string, string[]>();
  for (const edge of edges) {
    const uCol = colOfNode.get(edge.from_id);
    const vCol = colOfNode.get(edge.to_id);
    if (uCol === undefined || vCol === undefined || uCol >= vCol) continue;
    const list = predsById.get(edge.to_id) ?? [];
    list.push(edge.from_id);
    predsById.set(edge.to_id, list);
  }

  const maxRows = Math.max(1, ...columns.map((c) => c.length));
  const totalInnerH = maxRows * CAUSAL_NODE_HEIGHT + Math.max(0, maxRows - 1) * ROW_GAP;
  const totalHeight = PAD_Y * 2 + totalInnerH;
  const totalWidth =
    PAD_X * 2 + columns.length * CAUSAL_NODE_WIDTH + Math.max(0, columns.length - 1) * COL_GAP;

  const tempCenterY = new Map<string, number>();
  const layoutNodes: LayoutNode[] = [];
  const nodeById = new Map<string, LayoutNode>();

  for (let col = 0; col < columns.length; col += 1) {
    const bucket = columns[col]!;
    bucket.sort((a, b) => {
      const aPreds = (predsById.get(a.node.id) ?? []).map((id) => tempCenterY.get(id)).filter((v): v is number => v !== undefined);
      const bPreds = (predsById.get(b.node.id) ?? []).map((id) => tempCenterY.get(id)).filter((v): v is number => v !== undefined);
      const aAvg = aPreds.length > 0 ? aPreds.reduce((s, v) => s + v, 0) / aPreds.length : Number.POSITIVE_INFINITY;
      const bAvg = bPreds.length > 0 ? bPreds.reduce((s, v) => s + v, 0) / bPreds.length : Number.POSITIVE_INFINITY;
      if (aAvg !== bAvg) return aAvg - bAvg;
      return a.index - b.index;
    });

    const colInnerH = bucket.length * CAUSAL_NODE_HEIGHT + Math.max(0, bucket.length - 1) * ROW_GAP;
    const startY = PAD_Y + (totalInnerH - colInnerH) / 2;
    const x = PAD_X + col * (CAUSAL_NODE_WIDTH + COL_GAP);

    bucket.forEach((item, row) => {
      const y = startY + row * (CAUSAL_NODE_HEIGHT + ROW_GAP);
      const cx = x + CAUSAL_NODE_WIDTH / 2;
      const cy = y + CAUSAL_NODE_HEIGHT / 2;
      tempCenterY.set(item.node.id, cy);
      const layoutNode: LayoutNode = {
        node: item.node,
        col,
        row,
        x,
        y,
        width: CAUSAL_NODE_WIDTH,
        height: CAUSAL_NODE_HEIGHT,
        cx,
        cy,
        lines: wrapCausalLabel(item.node.label, 20, 2),
      };
      layoutNodes.push(layoutNode);
      nodeById.set(item.node.id, layoutNode);
    });
  }

  // Deterministic edge routing. Track midpoint counts to avoid overlapping edge badges.
  const layoutEdges: LayoutEdge[] = [];
  const sameColEdgeOrder = new Map<string, number>();

  edges.forEach((edge, index) => {
    const from = nodeById.get(edge.from_id);
    const to = nodeById.get(edge.to_id);
    if (!from || !to) return;

    let path = "";
    let labelX = 0;
    let labelY = 0;

    if (from.col < to.col) {
      const x1 = from.x + from.width;
      const y1 = from.cy;
      const x2 = to.x;
      const y2 = to.cy;
      const dx = Math.max(28, (x2 - x1) * 0.46);
      const c1x = x1 + dx;
      const c2x = x2 - dx;
      path = `M ${x1} ${y1} C ${c1x} ${y1}, ${c2x} ${y2}, ${x2} ${y2}`;
      labelX = cubicPointAtHalf(x1, c1x, c2x, x2);
      labelY = cubicPointAtHalf(y1, y1, y2, y2);
    } else if (from.col === to.col) {
      const count = sameColEdgeOrder.get(String(from.col)) ?? 0;
      sameColEdgeOrder.set(String(from.col), count + 1);
      const arcOut = 34 + count * 12;
      const x1 = from.x + from.width;
      const y1 = from.cy;
      const x2 = to.x + to.width;
      const y2 = to.cy;
      const cx = x1 + arcOut;
      path = `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}`;
      labelX = cubicPointAtHalf(x1, cx, cx, x2);
      labelY = cubicPointAtHalf(y1, y1, y2, y2);
    } else {
      // Backward feedback edge routed over the top border between columns.
      const x1 = from.cx;
      const y1 = from.y;
      const x2 = to.cx;
      const y2 = to.y;
      const topY = Math.min(y1, y2) - 34;
      path = `M ${x1} ${y1} C ${x1} ${topY}, ${x2} ${topY}, ${x2} ${y2}`;
      labelX = cubicPointAtHalf(x1, x1, x2, x2);
      labelY = cubicPointAtHalf(y1, topY, topY, y2);
    }

    layoutEdges.push({
      index,
      edge,
      from,
      to,
      path,
      labelX,
      labelY,
      shortLabel: truncateLabel(edge.relation, 14),
    });
  });

  return {
    width: totalWidth,
    height: totalHeight,
    nodes: layoutNodes,
    edges: layoutEdges,
    nodeById,
  };
}
