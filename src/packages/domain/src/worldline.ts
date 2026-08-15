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
  worldline: Pick<Worldline, "baseline_date" | "horizon_end" | "nodes" | "edges" | "event_reveal_schedule">
): void {
  if (worldline.baseline_date > worldline.horizon_end) {
    throw new Error("Worldline baseline must not be after its horizon");
  }
  const nodes = new Map(worldline.nodes.map((node) => [node.node_id, node]));
  const incoming = new Map<string, Set<string>>(
    worldline.nodes.map((node) => [node.node_id, new Set<string>()])
  );
  for (const edge of worldline.edges) {
    const parent = nodes.get(edge.from_node_id);
    const child = nodes.get(edge.to_node_id);
    if (!parent || !child) throw new Error("Worldline edge refers to an unknown node");
    if (parent.event_date > child.event_date) {
      throw new Error(`Node time must be monotonic across ${edge.from_node_id} -> ${edge.to_node_id}`);
    }
    incoming.get(edge.to_node_id)?.add(edge.from_node_id);
  }
  for (const node of worldline.nodes) {
    if (node.event_date < worldline.baseline_date || node.event_date > worldline.horizon_end) throw new Error(`Node ${node.node_id} is outside the worldline horizon`);
    if (node.data_as_of > worldline.baseline_date || node.data_as_of > node.event_date) throw new Error(`Node ${node.node_id} references future data`);
    const parents = new Set(node.causal_parents);
    if ([...parents].some((parent) => !nodes.has(parent))) throw new Error(`Node ${node.node_id} has an unknown causal parent`);
    const edgeParents = incoming.get(node.node_id) ?? new Set<string>();
    if (parents.size !== edgeParents.size || [...parents].some((parent) => !edgeParents.has(parent))) {
      throw new Error(`Node ${node.node_id} causal parents must match incoming edges`);
    }
  }
  if (worldline.event_reveal_schedule.length === 0) throw new Error("Worldline event reveal schedule must not be empty");
  const scheduled = new Set<string>();
  for (const reveal of worldline.event_reveal_schedule) {
    if (scheduled.has(reveal.event_id)) throw new Error(`Reveal ${reveal.event_id} is duplicated`);
    scheduled.add(reveal.event_id);
    const node = nodes.get(reveal.event_id);
    if (!node) throw new Error(`Reveal ${reveal.event_id} refers to an unknown node`);
    if (reveal.event_date !== node.event_date) throw new Error(`Reveal ${reveal.event_id} must use its node event date`);
    if (reveal.rule !== "leak" && reveal.reveal_date < reveal.event_date) {
      throw new Error(`Reveal ${reveal.event_id} cannot precede its event date unless it is a leak`);
    }
    if (reveal.event_date < worldline.baseline_date || reveal.event_date > worldline.horizon_end || reveal.reveal_date < worldline.baseline_date || reveal.reveal_date > worldline.horizon_end) throw new Error(`Reveal ${reveal.event_id} is outside the worldline horizon`);
  }
  if (scheduled.size !== nodes.size || [...nodes.keys()].some((nodeId) => !scheduled.has(nodeId))) {
    throw new Error("Worldline event reveal schedule must contain every node exactly once");
  }
}
