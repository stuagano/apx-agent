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
// Node fills use the Du Bois surface tones (--apx-panel #1F272D for agents,
// --apx-bg #11171C for tools/resources); the stroke hues stay distinct because
// they encode node TYPE (agent=blue, data=green, router=pink, genie=amber, …).
export const NODE_STYLE: Record<NodeType, { fill: string; stroke: string }> = {
  Agent: { fill: "#1F272D", stroke: "#4299E0" },
  LlmAgent: { fill: "#1F272D", stroke: "#4299E0" },
  DataAgent: { fill: "#1F272D", stroke: "#3BA65E" },
  SequentialAgent: { fill: "#1F272D", stroke: "#A78BFA" },
  ParallelAgent: { fill: "#1F272D", stroke: "#A78BFA" },
  LoopAgent: { fill: "#1F272D", stroke: "#A78BFA" },
  RouterAgent: { fill: "#1F272D", stroke: "#EC7BA8" },
  KeywordRouter: { fill: "#1F272D", stroke: "#EC7BA8" },
  HandoffAgent: { fill: "#1F272D", stroke: "#EC7BA8" },
  Tool: { fill: "#11171C", stroke: "#92A4B3" },
  UCFunction: { fill: "#11171C", stroke: "#3BA65E" },
  GenieSpace: { fill: "#11171C", stroke: "#FACB66" },
  VectorIndex: { fill: "#11171C", stroke: "#22D3EE" },
  ServingEndpoint: { fill: "#11171C", stroke: "#FB923C" },
  SubAgent: { fill: "#11171C", stroke: "#A78BFA" },
  WarehouseSQL: { fill: "#11171C", stroke: "#A3E635" },
};
