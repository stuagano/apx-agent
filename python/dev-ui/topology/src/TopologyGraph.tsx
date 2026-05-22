// Renders the agent topology as an interactive @xyflow/react graph.
//
// Owner contract (see docs/superpowers/specs/2026-05-22-topology-ui.md):
//   - Auto-layout left-to-right via dagre (rankdir=LR, nodesep=60, ranksep=120).
//   - Each node uses NODE_STYLE[type] for fill + stroke.
//   - Selection highlights the node (accent box-shadow) and its incident edges.
//   - Edge labels show `kind`.
//   - Click → onNodeClick(nodeId).
//   - Includes MiniMap, Controls, Background.

import { useMemo } from "react";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  type Edge,
  type Node,
  type NodeMouseHandler,
  Position,
} from "@xyflow/react";
import dagre from "dagre";

import { NODE_STYLE, type NodeType, type TopologyResponse } from "./types";

import "@xyflow/react/dist/style.css";

export interface TopologyGraphProps {
  data: TopologyResponse;
  selected: string | null;
  onNodeClick: (nodeId: string) => void;
}

// Dagre layout constants — match the spec's Visual contract.
const NODE_WIDTH = 200;
const NODE_HEIGHT = 56;
const RANKDIR = "LR";
const NODESEP = 60;
const RANKSEP = 120;

interface LayoutResult {
  nodes: Node[];
  edges: Edge[];
}

function layout(
  data: TopologyResponse,
  selected: string | null,
): LayoutResult {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: RANKDIR, nodesep: NODESEP, ranksep: RANKSEP });
  g.setDefaultEdgeLabel(() => ({}));

  for (const n of data.nodes) {
    g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const e of data.edges) {
    g.setEdge(e.source, e.target);
  }

  dagre.layout(g);

  const rfNodes: Node[] = data.nodes.map((n) => {
    const pos = g.node(n.id);
    const style = NODE_STYLE[n.type as NodeType];
    const isSelected = selected === n.id;
    return {
      id: n.id,
      position: {
        // dagre gives center coords; react-flow wants top-left.
        x: (pos?.x ?? 0) - NODE_WIDTH / 2,
        y: (pos?.y ?? 0) - NODE_HEIGHT / 2,
      },
      data: { label: n.label },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      style: {
        background: style.fill,
        border: `1.5px solid ${style.stroke}`,
        borderRadius: 6,
        color: "#e2e8f0",
        fontSize: 12,
        fontWeight: 500,
        padding: "8px 12px",
        width: NODE_WIDTH,
        boxShadow: isSelected ? "0 0 0 2px var(--accent, #38bdf8)" : undefined,
      },
    };
  });

  const rfEdges: Edge[] = data.edges.map((e) => {
    const incident = selected !== null && (e.source === selected || e.target === selected);
    return {
      id: e.id,
      source: e.source,
      target: e.target,
      label: e.kind,
      labelStyle: { fill: "#cbd5e1", fontSize: 10 },
      labelBgStyle: { fill: "#0a0a0a", fillOpacity: 0.85 },
      labelBgPadding: [4, 2],
      labelBgBorderRadius: 3,
      style: {
        stroke: incident ? "var(--accent, #38bdf8)" : "#475569",
        strokeWidth: incident ? 2 : 1,
      },
    };
  });

  return { nodes: rfNodes, edges: rfEdges };
}

export function TopologyGraph(props: TopologyGraphProps) {
  const { data, selected, onNodeClick } = props;

  const { nodes, edges } = useMemo(
    () => layout(data, selected),
    [data, selected],
  );

  const handleNodeClick: NodeMouseHandler = (_event, node) => {
    onNodeClick(node.id);
  };

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodeClick={handleNodeClick}
      fitView
      proOptions={{ hideAttribution: true }}
      minZoom={0.2}
      maxZoom={2}
    >
      <Background color="#1e293b" gap={20} />
      <Controls />
      <MiniMap
        pannable
        zoomable
        maskColor="rgba(10, 10, 10, 0.7)"
        style={{ background: "#0f172a" }}
        nodeColor={(node) => {
          const topoNode = data.nodes.find((n) => n.id === node.id);
          if (!topoNode) return "#475569";
          return NODE_STYLE[topoNode.type as NodeType].stroke;
        }}
        nodeStrokeColor={(node) => {
          const topoNode = data.nodes.find((n) => n.id === node.id);
          if (!topoNode) return "#475569";
          return NODE_STYLE[topoNode.type as NodeType].stroke;
        }}
      />
    </ReactFlow>
  );
}
