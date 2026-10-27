// Discover canvas: typed workspace assets on xyflow, dagre LR layout.
// Impact mode lights the downstream set amber and dims everything else.
import { useMemo } from "react";
import {
  Background, Controls, MiniMap, ReactFlow, ReactFlowProvider,
  Position, type Edge, type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { NODE_WIDTH, dagrePositions } from "../layout";
import { EDGE_LEGEND, NODE_COLORS, type GraphEdge, type GraphNode } from "./types";

const EDGE_STYLE = new Map(EDGE_LEGEND.map((e) => [e.type, e]));

export interface DiscoverGraphProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  selected: string | null;
  impacted: ReadonlySet<string> | null;
  collapsedCounts: ReadonlyMap<string, number>;
  onSelect: (id: string) => void;
  onToggleSchema: (id: string) => void;
}

function Inner(props: DiscoverGraphProps) {
  const { nodes, edges, selected, impacted, collapsedCounts, onSelect, onToggleSchema } = props;
  const pos = useMemo(
    () => dagrePositions(nodes.map((n) => n.id), edges.map((e) => ({ source: e.src, target: e.dst }))),
    [nodes, edges],
  );
  const typeById = useMemo(() => new Map(nodes.map((n) => [n.id, n.type])), [nodes]);
  const rf = useMemo(() => {
    const lit = (id: string) => impacted?.has(id) || id === selected;
    const rfNodes: Node[] = nodes.map((n) => {
      const color = NODE_COLORS[n.type] ?? "#92A4B3";
      const hidden = collapsedCounts.get(n.id);
      return {
        id: n.id,
        position: pos.get(n.id)!,
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        data: {
          label: (
            <div className="apx-dg-node">
              <span className="apx-dg-type" style={{ color }}>{n.type}</span>
              <span className="apx-dg-name" title={n.fqn}>{n.name}</span>
              {hidden !== undefined && <span className="apx-dg-collapsed">+{hidden}</span>}
            </div>
          ),
        },
        style: {
          width: NODE_WIDTH,
          background: "#11171C",
          color: "#E8ECF0",
          border: `1.5px solid ${impacted?.has(n.id) ? "#FACB66" : color}`,
          borderRadius: 6,
          padding: "6px 10px",
          fontSize: 12,
          opacity: impacted && !lit(n.id) ? 0.2 : 1,
          boxShadow: n.id === selected ? "0 0 0 2px var(--accent, #2272B4)" : undefined,
        },
      };
    });
    const rfEdges: Edge[] = edges.map((e) => {
      const s = EDGE_STYLE.get(e.type);
      return {
        id: `${e.src}--${e.type}--${e.dst}`,
        source: e.src,
        target: e.dst,
        label: e.type === "contains" ? undefined : e.type,
        labelStyle: { fill: "#92A4B3", fontSize: 10 },
        labelBgStyle: { fill: "#11171C", fillOpacity: 0.85 },
        style: {
          stroke: s?.color ?? "#445461",
          strokeDasharray: s?.dashed ? "5 4" : undefined,
          opacity: impacted && !(lit(e.src) && lit(e.dst)) ? 0.15 : 1,
        },
      };
    });
    return { rfNodes, rfEdges };
  }, [nodes, edges, pos, selected, impacted, collapsedCounts]);

  return (
    <ReactFlow
      nodes={rf.rfNodes}
      edges={rf.rfEdges}
      onNodeClick={(_e, n) => onSelect(n.id)}
      onNodeDoubleClick={(_e, n) => {
        if (n.id.startsWith("schema:")) onToggleSchema(n.id);
      }}
      nodesConnectable={false}
      fitView
      minZoom={0.05}
      maxZoom={2}
      proOptions={{ hideAttribution: true }}
    >
      <Background color="#37444F" gap={20} />
      <Controls />
      <MiniMap pannable zoomable maskColor="rgba(17, 23, 28, 0.7)" style={{ background: "#11171C" }}
        nodeColor={(n) => NODE_COLORS[typeById.get(n.id) ?? ""] ?? "#92A4B3"} />
    </ReactFlow>
  );
}

export function DiscoverGraph(props: DiscoverGraphProps) {
  return (
    <ReactFlowProvider>
      <Inner {...props} />
    </ReactFlowProvider>
  );
}
