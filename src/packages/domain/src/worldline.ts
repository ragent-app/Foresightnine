import type { Worldline, WorldlineNode } from "@foresightnine/contracts";

export function assertAcyclicWorldline(worldline: Pick<Worldline, "nodes" | "edges">): void {
  const ids = new Set(worldline.nodes.map((node: WorldlineNode) => node.node_id));
  const children = new Map<string, string[]>();
  for (const edge of worldline.edges) {
    if (!ids.has(edge.from_node_id) || !ids.has(edge.to_node_id)) {
      throw new Error("Worldline edge refers to an unknown node");
    }
    children.set(edge.from_node_id, [...(children.get(edge.from_node_id) ?? []), edge.to_node_id]);
  }
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const visit = (id: string): void => {
    if (visiting.has(id)) throw new Error("Worldline must be acyclic");
    if (visited.has(id)) return;
    visiting.add(id);
    for (const child of children.get(id) ?? []) visit(child);
    visiting.delete(id);
    visited.add(id);
  };
  for (const id of ids) visit(id);
}

export function assertWorldlineDates(
  worldline: Pick<Worldline, "baseline_date" | "horizon_end" | "event_reveal_schedule">
): void {
  if (worldline.baseline_date > worldline.horizon_end) {
    throw new Error("Worldline baseline must not be after its horizon");
  }
  for (const reveal of worldline.event_reveal_schedule) {
    if (reveal.reveal_date < reveal.event_date) {
      throw new Error(`Reveal ${reveal.event_id} cannot precede its event date`);
    }
  }
}
