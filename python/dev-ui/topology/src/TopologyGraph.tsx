// Renders the agent topology as an interactive @xyflow/react graph.
//
// Owner contract (see docs/superpowers/specs/2026-05-22-topology-ui.md):
//   - Auto-layout left-to-right via dagre (rankdir=LR, nodesep=60, ranksep=120).
//   - Each node uses NODE_STYLE[type] for fill + stroke.
//   - Selection highlights the node (accent box-shadow) and its incident edges.
//   - Edge labels show `kind`.
//   - Click → onNodeClick(nodeId).
//   - Drop Discover palette chips onto leaf Agents → onWireDrop(nodeId, payload).
//   - MiniMap + Controls only when showMap (large graphs).

import { useCallback, useEffect, useMemo, useState, type DragEvent } from "react";
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
import dagre from "dagre";

import { NODE_STYLE, type NodeType, type TopologyResponse } from "./types";
import {
  WIRE_MIME,
  isLeafAgentType,
  type WirePayload,
} from "./wire";

import "@xyflow/react/dist/style.css";

export interface TopologyGraphProps {
  data: TopologyResponse;
  selected: string | null;
  /** Nodes lit by the latest Chat turn (from `/_apx/traces/last-route`). */
  routeNodeIds?: ReadonlySet<string>;
  /** Edges lit by the latest Chat turn. */
  routeEdgeIds?: ReadonlySet<string>;
  droppableIds?: ReadonlySet<string>;
  /** Show MiniMap + zoom Controls (default: off for small graphs). */
  showMap?: boolean;
  onNodeClick: (nodeId: string) => void;
  onWireDrop?: (nodeId: string, payload: WirePayload) => void;
}

const DEFAULT_NODE_STYLE = { fill: "#1e293b", stroke: "#64748b" };
const styleFor = (type: string) =>
  NODE_STYLE[type as NodeType] ?? DEFAULT_NODE_STYLE;

const EMBED =
  typeof window !== "undefined" &&
  new URLSearchParams(window.location.search).get("embed") === "1";

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
  routeNodeIds: ReadonlySet<string> | undefined,
  routeEdgeIds: ReadonlySet<string> | undefined,
  droppableIds: ReadonlySet<string> | undefined,
  dropHoverId: string | null,
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
    const style = styleFor(n.type);
    const isSelected = selected === n.id;
    const onRoute = !!routeNodeIds?.has(n.id);
    const isDroppable = !!droppableIds?.has(n.id) && isLeafAgentType(n.type);
    const isDropHover = dropHoverId === n.id;
    return {
      id: n.id,
      position: {
        x: (pos?.x ?? 0) - NODE_WIDTH / 2,
        y: (pos?.y ?? 0) - NODE_HEIGHT / 2,
      },
      data: {
        label: n.label,
        topoType: n.type,
        droppable: isDroppable,
      },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      className: [
        isDroppable ? "apx-droppable" : "",
        isDropHover ? "apx-drop-hover" : "",
        onRoute ? "apx-route-active" : "",
      ]
        .filter(Boolean)
        .join(" "),
      style: {
        background: style.fill,
        border: `1.5px solid ${
          isDropHover ? "#4ade80" : onRoute ? "#fbbf24" : style.stroke
        }`,
        borderRadius: 6,
        color: "#e2e8f0",
        fontSize: 12,
        fontWeight: 500,
        padding: "8px 12px",
        width: NODE_WIDTH,
        boxShadow: isSelected
          ? "0 0 0 2px var(--accent, #38bdf8)"
          : onRoute
            ? "0 0 0 2px #fbbf24, 0 0 12px rgba(251, 191, 36, 0.35)"
            : isDropHover
              ? "0 0 0 2px #4ade80"
              : isDroppable
                ? "0 0 0 1px rgba(74, 222, 128, 0.35)"
                : undefined,
        outline: isDroppable ? "1px dashed rgba(74, 222, 128, 0.4)" : undefined,
        outlineOffset: 2,
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
      labelStyle: { fill: onRoute ? "#fde68a" : "#cbd5e1", fontSize: 10 },
      labelBgStyle: { fill: "#0a0a0a", fillOpacity: 0.85 },
      labelBgPadding: [4, 2] as [number, number],
      labelBgBorderRadius: 3,
      animated: onRoute,
      style: {
        stroke: onRoute ? "#fbbf24" : incident ? "var(--accent, #38bdf8)" : "#475569",
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

function hitNodeId(
  flowPos: { x: number; y: number },
  nodes: Node[],
): string | null {
  for (let i = nodes.length - 1; i >= 0; i--) {
    const n = nodes[i]!;
    const w = typeof n.style?.width === "number" ? n.style.width : NODE_WIDTH;
    const h = NODE_HEIGHT;
    if (
      flowPos.x >= n.position.x &&
      flowPos.x <= n.position.x + w &&
      flowPos.y >= n.position.y &&
      flowPos.y <= n.position.y + h
    ) {
      return n.id;
    }
  }
  return null;
}

function TopologyGraphInner(props: TopologyGraphProps) {
  const {
    data,
    selected,
    routeNodeIds,
    routeEdgeIds,
    droppableIds,
    showMap = false,
    onNodeClick,
    onWireDrop,
  } = props;
  const [dropHoverId, setDropHoverId] = useState<string | null>(null);
  const { screenToFlowPosition } = useReactFlow();

  const { nodes, edges } = useMemo(
    () =>
      layout(data, selected, routeNodeIds, routeEdgeIds, droppableIds, dropHoverId),
    [data, selected, routeNodeIds, routeEdgeIds, droppableIds, dropHoverId],
  );

  const handleNodeClick: NodeMouseHandler = (_event, node) => {
    onNodeClick(node.id);
  };

  const parsePayload = (e: DragEvent): WirePayload | null => {
    const raw =
      e.dataTransfer.getData(WIRE_MIME) || e.dataTransfer.getData("text/plain");
    if (!raw) return null;
    try {
      return JSON.parse(raw) as WirePayload;
    } catch {
      return null;
    }
  };

  const onDragOver = useCallback(
    (e: DragEvent) => {
      if (!onWireDrop) return;
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = "copy";
      const flowPos = screenToFlowPosition({ x: e.clientX, y: e.clientY });
      const id = hitNodeId(flowPos, nodes);
      if (id && droppableIds?.has(id)) {
        setDropHoverId(id);
      } else {
        setDropHoverId(null);
      }
    },
    [onWireDrop, screenToFlowPosition, nodes, droppableIds],
  );

  const onDrop = useCallback(
    (e: DragEvent) => {
      if (!onWireDrop) return;
      e.preventDefault();
      e.stopPropagation();
      const flowPos = screenToFlowPosition({ x: e.clientX, y: e.clientY });
      const id = hitNodeId(flowPos, nodes);
      setDropHoverId(null);
      const payload = parsePayload(e);
      if (!payload) return;
      // Always invoke so App can toast "not eligible" when id isn't droppable
      onWireDrop(id || "", payload);
    },
    [onWireDrop, screenToFlowPosition, nodes],
  );

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
      onDragOver={onDragOver}
      onDrop={onDrop}
      onDragLeave={() => setDropHoverId(null)}
    >
      <FitOnResize />
      <Background color="#1e293b" gap={20} />
      {!EMBED && showMap && <Controls />}
      {!EMBED && showMap && (
        <MiniMap
          pannable
          zoomable
          maskColor="rgba(10, 10, 10, 0.7)"
          style={{ background: "#0f172a" }}
          nodeColor={(node) => {
            const topoNode = data.nodes.find((n) => n.id === node.id);
            if (!topoNode) return "#475569";
            return styleFor(topoNode.type).stroke;
          }}
          nodeStrokeColor={(node) => {
            const topoNode = data.nodes.find((n) => n.id === node.id);
            if (!topoNode) return "#475569";
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
