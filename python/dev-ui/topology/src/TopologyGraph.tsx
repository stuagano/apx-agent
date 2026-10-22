// Renders the agent topology as an interactive @xyflow/react graph.
//
// Owner contract (see docs/superpowers/specs/2026-05-22-topology-ui.md):
//   - Auto-layout left-to-right via dagre (rankdir=LR, nodesep=60, ranksep=120).
//   - Each node uses NODE_STYLE[type] for fill + stroke.
//   - Selection highlights the node (accent box-shadow) and its incident edges.
//   - Edge labels show `kind`.
//   - Click → onNodeClick(nodeId).
//   - MiniMap + Controls only when showMap (large graphs).

import { useEffect, useMemo } from "react";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
  type NodeMouseHandler,
  Position,
} from "@xyflow/react";

import { NODE_STYLE, type NodeType, type TopologyResponse } from "./types";
import { NODE_WIDTH, dagrePositions } from "./layout";

import "@xyflow/react/dist/style.css";

export interface TopologyGraphProps {
  data: TopologyResponse;
  selected: string | null;
  /** Nodes lit by the latest Chat turn (from `/_apx/traces/last-route`). */
  routeNodeIds?: ReadonlySet<string>;
  /** Edges lit by the latest Chat turn. */
  routeEdgeIds?: ReadonlySet<string>;
  /** Show MiniMap + zoom Controls (default: off for small graphs). */
  showMap?: boolean;
  onNodeClick: (nodeId: string) => void;
}

const DEFAULT_NODE_STYLE = { fill: "#37444F", stroke: "#92A4B3" };
const styleFor = (type: string) =>
  NODE_STYLE[type as NodeType] ?? DEFAULT_NODE_STYLE;

const EMBED =
  typeof window !== "undefined" &&
  new URLSearchParams(window.location.search).get("embed") === "1";

interface LayoutResult {
  nodes: Node[];
  edges: Edge[];
}

function contextBadges(metadata?: Record<string, unknown>): string[] {
  if (!metadata) return [];
  const badges: string[] = [];
  if (typeof metadata.purpose === "string" && metadata.purpose.trim()) badges.push("OKF");
  if (typeof metadata.domain === "string" && metadata.domain.trim()) badges.push("Domain");
  if (Array.isArray(metadata.question_answer_pairs) && metadata.question_answer_pairs.length) {
    badges.push("Q/A");
  }
  return badges;
}

function layout(
  data: TopologyResponse,
  selected: string | null,
  routeNodeIds: ReadonlySet<string> | undefined,
  routeEdgeIds: ReadonlySet<string> | undefined,
): LayoutResult {
  const pos = dagrePositions(data.nodes.map((n) => n.id), data.edges);

  const rfNodes: Node[] = data.nodes.map((n) => {
    const style = styleFor(n.type);
    const badges = contextBadges(n.metadata);
    const isSelected = selected === n.id;
    const onRoute = !!routeNodeIds?.has(n.id);
    return {
      id: n.id,
      position: pos.get(n.id)!,
      data: {
        label: badges.length ? `${n.label}  ${badges.join(" · ")}` : n.label,
        topoType: n.type,
      },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      className: onRoute ? "apx-route-active" : "",
      style: {
        background: style.fill,
        border: `1.5px solid ${onRoute ? "#FACB66" : style.stroke}`,
        borderRadius: 6,
        color: "#E8ECF0",
        fontSize: 12,
        fontWeight: 500,
        padding: "8px 12px",
        width: NODE_WIDTH,
        boxShadow: isSelected
          ? "0 0 0 2px var(--accent, #2272B4)"
          : onRoute
            ? "0 0 0 2px #FACB66, 0 0 12px rgba(250, 203, 102, 0.35)"
            : undefined,
      },
    };
  });

  const rfEdges: Edge[] = data.edges.map((e) => {
    const incident = selected !== null && (e.source === selected || e.target === selected);
    const onRoute = !!routeEdgeIds?.has(e.id);
    return {
      id: e.id,
      source: e.source,
      target: e.target,
      label: e.kind,
      labelStyle: { fill: onRoute ? "#FACB66" : "#92A4B3", fontSize: 10 },
      labelBgStyle: { fill: "#11171C", fillOpacity: 0.85 },
      labelBgPadding: [4, 2] as [number, number],
      labelBgBorderRadius: 3,
      animated: onRoute,
      style: {
        stroke: onRoute ? "#FACB66" : incident ? "var(--accent, #2272B4)" : "#445461",
        strokeWidth: onRoute ? 2.5 : incident ? 2 : 1,
      },
    };
  });

  return { nodes: rfNodes, edges: rfEdges };
}

function FitOnResize() {
  const { fitView } = useReactFlow();
  useEffect(() => {
    const handler = () => fitView({ duration: 200, padding: 0.15 });
    window.addEventListener("resize", handler);
    return () => window.removeEventListener("resize", handler);
  }, [fitView]);
  return null;
}

function TopologyGraphInner(props: TopologyGraphProps) {
  const {
    data,
    selected,
    routeNodeIds,
    routeEdgeIds,
    showMap = false,
    onNodeClick,
  } = props;

  const { nodes, edges } = useMemo(
    () => layout(data, selected, routeNodeIds, routeEdgeIds),
    [data, selected, routeNodeIds, routeEdgeIds],
  );

  const handleNodeClick: NodeMouseHandler = (_event, node) => {
    onNodeClick(node.id);
  };

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodeClick={handleNodeClick}
      nodesConnectable={false}
      fitView
      proOptions={{ hideAttribution: true }}
      minZoom={0.2}
      maxZoom={2}
    >
      <FitOnResize />
      <Background color="#37444F" gap={20} />
      {!EMBED && showMap && <Controls />}
      {!EMBED && showMap && (
        <MiniMap
          pannable
          zoomable
          maskColor="rgba(17, 23, 28, 0.7)"
          style={{ background: "#11171C" }}
          nodeColor={(node) => {
            const topoNode = data.nodes.find((n) => n.id === node.id);
            if (!topoNode) return "#445461";
            return styleFor(topoNode.type).stroke;
          }}
          nodeStrokeColor={(node) => {
            const topoNode = data.nodes.find((n) => n.id === node.id);
            if (!topoNode) return "#445461";
            return styleFor(topoNode.type).stroke;
          }}
        />
      )}
    </ReactFlow>
  );
}

export function TopologyGraph(props: TopologyGraphProps) {
  return (
    <ReactFlowProvider>
      <TopologyGraphInner {...props} />
    </ReactFlowProvider>
  );
}
