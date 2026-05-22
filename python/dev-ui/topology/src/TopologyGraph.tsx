// STUB — to be filled in by Agent 2 (Phase 1).
//
// Owner contract (see docs/superpowers/specs/2026-05-22-topology-ui.md):
//
//   - Render a `@xyflow/react` graph from the supplied topology data.
//   - Auto-layout left-to-right using `dagre` (rankdir=LR, nodesep=60, ranksep=120).
//   - Color/stroke each node per NODE_STYLE from ./types.
//   - When `selected` is non-null, highlight that node and its incident edges.
//   - On node click, invoke `onNodeClick(nodeId)`.
//   - Include MiniMap + Controls + Background components from @xyflow/react.

import type { TopologyResponse } from "./types";

export interface TopologyGraphProps {
  data: TopologyResponse;
  selected: string | null;
  onNodeClick: (nodeId: string) => void;
}

export function TopologyGraph(_props: TopologyGraphProps) {
  return (
    <div style={{ padding: 20, color: "var(--text-muted)" }}>
      TopologyGraph stub — filled in by Phase 1 Agent 2.
    </div>
  );
}
