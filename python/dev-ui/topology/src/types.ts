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
  /** Application-owned semantic fields added by apx_agent.annotate_topology. */
  metadata?: Record<string, unknown>;
}

export interface TopoEdge {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
  /** Application-owned semantic fields added by apx_agent.annotate_topology. */
  metadata?: Record<string, unknown>;
}

export interface TopologyExecution {
  trace_id?: string;
  active_node_ids?: string[];
  completed_edge_ids?: string[];
  failed_node_ids?: string[];
  [key: string]: unknown;
}

export interface ArtifactSummary {
  source_agent?: string;
  contract?: string;
  [key: string]: unknown;
}

export interface WorkflowHandoff {
  source: string;
  target: string;
  input_contract: string;
  output_contract: string;
  explanation: string;
}

export interface ExampleWorkflow {
  id: string;
  title: string;
  question: string;
  purpose: string;
  route: string[];
  handoffs: WorkflowHandoff[];
  outcome: string;
  follow_ups: string[];
}

export interface TopologyResponse {
  rootId: string;
  agentName: string;
  nodes: TopoNode[];
  edges: TopoEdge[];
  /** Absent from topology fixtures emitted before example workflows existed. */
  workflows?: ExampleWorkflow[];
  execution?: TopologyExecution;
  artifact_summaries?: ArtifactSummary[];
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

export interface InspectActions {
  canEditInstructions?: boolean;
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
// Node fills use the Du Bois surface tones (--apx-panel #ffffff for agents,
// --apx-bg #f1f3ff for tools/resources); the stroke hues stay distinct because
// they encode node TYPE (agent=blue, data=green, router=pink, genie=amber, …).
export const NODE_STYLE: Record<NodeType, { fill: string; stroke: string }> = {
  Agent: { fill: "#ffffff", stroke: "#1a5fc4" },
  LlmAgent: { fill: "#ffffff", stroke: "#1a5fc4" },
  DataAgent: { fill: "#ffffff", stroke: "#137333" },
  SequentialAgent: { fill: "#ffffff", stroke: "#7040a0" },
  ParallelAgent: { fill: "#ffffff", stroke: "#7040a0" },
  LoopAgent: { fill: "#ffffff", stroke: "#7040a0" },
  RouterAgent: { fill: "#ffffff", stroke: "#be185d" },
  KeywordRouter: { fill: "#ffffff", stroke: "#be185d" },
  HandoffAgent: { fill: "#ffffff", stroke: "#be185d" },
  Tool: { fill: "#f1f3ff", stroke: "#5f6469" },
  UCFunction: { fill: "#f1f3ff", stroke: "#137333" },
  GenieSpace: { fill: "#f1f3ff", stroke: "#8b5000" },
  VectorIndex: { fill: "#f1f3ff", stroke: "#0b7285" },
  ServingEndpoint: { fill: "#f1f3ff", stroke: "#c2410c" },
  SubAgent: { fill: "#f1f3ff", stroke: "#7040a0" },
  WarehouseSQL: { fill: "#f1f3ff", stroke: "#4d7c0f" },
};
