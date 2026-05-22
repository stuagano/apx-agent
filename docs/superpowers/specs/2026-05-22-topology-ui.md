# Topology UI — design spec

Status: draft (Phase 0)
Owner: stuart-gano
Date: 2026-05-22

## Goal

Add an interactive React-flow visualization of the agent topology at `/_apx/topology`. Click any node to inspect its details in a side panel. Matches dao-ai-builder's "graph visualization showing agent/tool/resource connections" feature.

## Non-goals (v1)

- Edit topology from the graph (view-only)
- Trace overlays (which paths were called, frequencies)
- Live updates as `agent.py` changes (refresh-on-reload only)

## Architecture

```
python/dev-ui/topology/         # React + Vite source
  package.json
  vite.config.ts                 # outputs to ../../src/apx_agent/_static/topology/
  tsconfig.json
  index.html
  src/
    main.tsx                     # entry point
    App.tsx                      # owns selection state, wires graph + inspector
    TopologyGraph.tsx            # @xyflow/react graph
    NodeInspector.tsx            # right-side details panel
    types.ts                     # TypeScript types matching the JSON schemas
    sample-topology.json         # fixture for component development
    index.css                    # dark theme matching /_apx/*

python/src/apx_agent/
  _topology.py                   # builds the JSON from an Agent tree
  _dev.py                        # adds 3 new routes (see below)
  _static/topology/              # built bundle (shipped in wheel)
```

## Routes

| Route | Returns |
|---|---|
| `GET /_apx/topology` | `index.html` from the built bundle |
| `GET /_apx/topology.json` | full topology as `{ nodes, edges }` (see schema below) |
| `GET /_apx/topology/inspect/{node_id}` | details for one node |
| `GET /_apx/topology/assets/...` | static assets from the bundle |

## JSON schemas

### `/_apx/topology.json`

```typescript
type TopologyResponse = {
  rootId: string;          // ID of the top-level Agent
  agentName: string;       // ctx.config.name
  nodes: Node[];
  edges: Edge[];
};

type Node = {
  id: string;              // stable ID — see ID convention below
  type: NodeType;
  label: string;           // display name
  description?: string;    // tooltip / inspector summary
};

type NodeType =
  | "Agent"                // root + sub-agents that are LlmAgent or workflow agents
  | "LlmAgent"             // explicit LlmAgent (distinct from generic Agent)
  | "SequentialAgent"
  | "ParallelAgent"
  | "LoopAgent"
  | "RouterAgent"
  | "HandoffAgent"
  | "Tool"                 // generic typed-tool function
  | "UCFunction"           // uc_function_tool
  | "GenieSpace"           // genie_tool
  | "VectorIndex"          // vector_search_tool
  | "ServingEndpoint"      // foundation_model_tool / LLM endpoint
  | "SubAgent"             // remote agent reached via URL or endpoint
  | "WarehouseSQL";        // sql_tool

type Edge = {
  id: string;              // unique edge ID, e.g. "agent.root->tool.lookup"
  source: string;          // Node.id
  target: string;          // Node.id
  kind: EdgeKind;
};

type EdgeKind =
  | "uses-tool"            // Agent → Tool
  | "delegates-to"         // Agent → SubAgent (agent_tool / sub_agents=[url])
  | "next-step"            // SequentialAgent step ordering
  | "branch"               // RouterAgent branch / HandoffAgent target
  | "iterates"             // LoopAgent → inner agent
  | "calls-model";         // Agent → ServingEndpoint (its LLM)
```

#### ID convention

Stable enough to reconnect a fresh topology JSON to a previously selected node:

- Agents: `agent:<dotted-path>` — root is `agent:root`, sub-agents are `agent:root.specialists.billing` etc.
- Tools: `tool:<dotted-path>:<tool_name>` — `tool:agent:root:lookup_account`
- Platform resources: `<type>:<identifier>` — `uc:main.tools.classify_intent`, `genie:abc123`, `vs:main.search.docs`, `endpoint:databricks-claude-sonnet-4-6`

### `/_apx/topology/inspect/{node_id}`

```typescript
type InspectResponse = {
  id: string;
  type: NodeType;
  label: string;
  description?: string;
  // type-specific details — at least one of these will be present
  agent?: AgentDetails;
  tool?: ToolDetails;
  resource?: ResourceDetails;
  subAgent?: SubAgentDetails;
};

type AgentDetails = {
  className: string;       // "Agent" | "LlmAgent" | "SequentialAgent" | ...
  instructions?: string;   // truncated to 4 KB if longer
  model?: string;
  toolCount: number;
  subAgentCount: number;
  maxIterations?: number;
};

type ToolDetails = {
  name: string;
  description?: string;
  inputSchema?: object;    // JSON Schema from the tool's input model
  isSync: boolean;
  hasObOTokenDep: boolean; // True if function takes Dependencies.UserClient / Dependencies.Workspace
};

type ResourceDetails = {
  resourceKind: "uc_function" | "genie_space" | "vector_index" | "serving_endpoint" | "sql_warehouse";
  identifier: string;      // "main.tools.classify_intent" / space_id / etc.
  url?: string;            // Managed MCP URL when applicable
};

type SubAgentDetails = {
  url?: string;            // Remote URL or "endpoints/<name>"
  cardSource?: "well-known" | "name-resolve" | "config";
  resolvedName?: string;
};
```

## Component contracts

```typescript
// App.tsx owns selection state:
type AppState = {
  selected: string | null;     // current Node.id
  data: TopologyResponse | null;
  loading: boolean;
  error: string | null;
};

// TopologyGraph.tsx
type TopologyGraphProps = {
  data: TopologyResponse;
  selected: string | null;     // highlights the node + its incoming/outgoing edges
  onNodeClick: (nodeId: string) => void;
};

// NodeInspector.tsx
type NodeInspectorProps = {
  nodeId: string;              // fetches /_apx/topology/inspect/{nodeId}
  onClose: () => void;
};
```

## Visual contract

Node colors (set via `style` on react-flow nodes):

| NodeType | Fill | Stroke | Icon hint |
|---|---|---|---|
| `Agent`, `LlmAgent` | `#1e293b` | `#60a5fa` | brain |
| `SequentialAgent`, `ParallelAgent`, `LoopAgent` | `#1e293b` | `#a78bfa` | flow |
| `RouterAgent`, `HandoffAgent` | `#1e293b` | `#f472b6` | fork |
| `Tool` | `#0f172a` | `#94a3b8` | wrench |
| `UCFunction` | `#0f172a` | `#34d399` | database |
| `GenieSpace` | `#0f172a` | `#fbbf24` | sparkles |
| `VectorIndex` | `#0f172a` | `#22d3ee` | search |
| `ServingEndpoint` | `#0f172a` | `#fb923c` | cpu |
| `SubAgent` | `#0f172a` | `#c084fc` | link |
| `WarehouseSQL` | `#0f172a` | `#a3e635` | database |

Background: `#0a0a0a` (matches `/_apx/agent` dark theme).

Layout: dagre, left-to-right (`rankdir=LR`), node spacing 60px, rank separation 120px.

## Build + ship

```bash
cd python/dev-ui/topology
npm install
npm run build          # outputs to python/src/apx_agent/_static/topology/
```

The wheel ships the built bundle via:

```toml
[tool.hatch.build.targets.wheel.force-include]
"src/apx_agent/_static/topology" = "apx_agent/_static/topology"
```

The maintainer runs `npm run build` before publishing. Users get a working UI from `pip install` without needing Node installed.

## Phase 1 fanout

Each subagent owns disjoint files (see plan in the conversation log).

| Agent | Owns | Develops against |
|---|---|---|
| 1. Backend | `python/src/apx_agent/_topology.py` (new), additions to `_dev.py`, `python/tests/test_topology.py` (new) | A real `Agent` tree built in the test |
| 2. TopologyGraph | `python/dev-ui/topology/src/TopologyGraph.tsx` | `sample-topology.json` fixture |
| 3. NodeInspector | `python/dev-ui/topology/src/NodeInspector.tsx` | Hardcoded inspect responses + `sample-topology.json` |
| 4. App shell | `python/dev-ui/topology/src/App.tsx`, `src/index.css` | Stubs from 2 + 3 |
| 5. Build wiring + docs | `python/pyproject.toml`, `docs/dev-ui.md`, `python/dev-ui/topology/README.md`, one header-link addition to `_ui_chat.py` | — |
