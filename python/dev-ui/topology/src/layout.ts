// Shared dagre layout for the Topology and Discover canvases.
import dagre from "dagre";

export const NODE_WIDTH = 200;
export const NODE_HEIGHT = 56;

/** Top-left node positions, left-to-right ranks. */
export function dagrePositions(
  ids: string[],
  edges: ReadonlyArray<{ source: string; target: string }>,
): Map<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 60, ranksep: 120 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const id of ids) g.setNode(id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  for (const e of edges) g.setEdge(e.source, e.target);
  dagre.layout(g);
  const out = new Map<string, { x: number; y: number }>();
  for (const id of ids) {
    const p = g.node(id);
    out.set(id, { x: (p?.x ?? 0) - NODE_WIDTH / 2, y: (p?.y ?? 0) - NODE_HEIGHT / 2 });
  }
  return out;
}
