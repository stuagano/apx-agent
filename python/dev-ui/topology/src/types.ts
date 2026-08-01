// TypeScript types matching the JSON schemas in
// docs/superpowers/specs/2026-05-22-topology-ui.md.

export type NodeType =
  | "Agent"
  | "LlmAgent"
  | "DataAgent"
  | "SequentialAgent"
  | "ParallelAgent"
  | "LoopAgent"
  | "RouterAgent"
  | "KeywordRouter"
  | "HandoffAgent"
  | "Tool"
  | "UCFunction"
  | "GenieSpace"
  | "VectorIndex"
  | "ServingEndpoint"
  | "SubAgent"
  | "WarehouseSQL";

export type EdgeKind =
  | "uses-tool"
  | "delegates-to"
  | "next-step"
  | "branch"
  | "iterates"
  | "calls-model";

export interface TopoNode {
  id: string;
  type: NodeType;
  label: string;
  description?: string;
}

export interface TopoEdge {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
}

export interface TopologyResponse {
  rootId: string;
  agentName: string;
  nodes: TopoNode[];
  edges: TopoEdge[];
}

// /_apx/topology/inspect/{node_id}

export interface AgentDetails {
  className: string;
  instructions?: string;
  model?: string;
  toolCount: number;
  subAgentCount: number;
  maxIterations?: number;
}

export interface ToolDetails {
  name: string;
  description?: string;
  inputSchema?: Record<string, unknown>;
  isSync: boolean;
  hasObOTokenDep: boolean;
}

export interface ResourceDetails {
  resourceKind:
    | "uc_function"
    | "genie_space"
    | "vector_index"
    | "serving_endpoint"
    | "sql_warehouse";
  identifier: string;
  url?: string;
}

export interface SubAgentDetails {
  url?: string;
  cardSource?: "well-known" | "name-resolve" | "config";
  resolvedName?: string;
  /** Exact ``sub_agents=`` entry (e.g. ``$APX_PEER_…_URL``). */
  ref?: string;
}

export interface InspectUnwireAction {
  kind: "agent" | "tool";
  target: string;
  ref?: string;
  binding_name?: string;
}

export interface InspectActions {
  canEditInstructions?: boolean;
  canUnwire?: boolean;
  wireTarget?: string;
  unwire?: InspectUnwireAction;
}

export interface InspectResponse {
  id: string;
  type: NodeType;
  label: string;
  description?: string;
  agent?: AgentDetails;
  tool?: ToolDetails;
  resource?: ResourceDetails;
  subAgent?: SubAgentDetails;
  actions?: InspectActions;
}

// Color map for node types — matches the visual contract in the spec.
export const NODE_STYLE: Record<NodeType, { fill: string; stroke: string }> = {
  Agent: { fill: "#1e293b", stroke: "#60a5fa" },
  LlmAgent: { fill: "#1e293b", stroke: "#60a5fa" },
  DataAgent: { fill: "#1e293b", stroke: "#34d399" },
  SequentialAgent: { fill: "#1e293b", stroke: "#a78bfa" },
  ParallelAgent: { fill: "#1e293b", stroke: "#a78bfa" },
  LoopAgent: { fill: "#1e293b", stroke: "#a78bfa" },
  RouterAgent: { fill: "#1e293b", stroke: "#f472b6" },
  KeywordRouter: { fill: "#1e293b", stroke: "#f472b6" },
  HandoffAgent: { fill: "#1e293b", stroke: "#f472b6" },
  Tool: { fill: "#0f172a", stroke: "#94a3b8" },
  UCFunction: { fill: "#0f172a", stroke: "#34d399" },
  GenieSpace: { fill: "#0f172a", stroke: "#fbbf24" },
  VectorIndex: { fill: "#0f172a", stroke: "#22d3ee" },
  ServingEndpoint: { fill: "#0f172a", stroke: "#fb923c" },
  SubAgent: { fill: "#0f172a", stroke: "#c084fc" },
  WarehouseSQL: { fill: "#0f172a", stroke: "#a3e635" },
};
