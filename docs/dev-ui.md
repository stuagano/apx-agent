# Dev UI

Apps-hosted agents include built-in development tooling under the `/_apx/*` path. Each surface is self-contained and styled to a shared dark theme; a fixed header (rendered by `_ui_nav.py`) lets you jump between them.

Model Serving deployments use AI Playground as the equivalent surface.

## Surfaces

### `/_apx/agent` — Chat
Interactive chat interface for testing the running agent. Streams responses, surfaces tool calls inline, and shows a setup banner if `[tool.apx.agent]` isn't configured. Use this to exercise the agent end-to-end while iterating.

### `/_apx/tools` — Tool inspector
Lists every tool exposed by the agent (typed Python tools, `uc_function_tool`, `genie_tool`, `vector_search_tool`, etc.) with their JSON Schemas and a live "invoke" form for each. Useful for verifying tool wiring without going through the LLM.

### `/_apx/topology` — Interactive topology graph

![topology view of customer_triage — HandoffAgent + 4 specialists + 8 tools + serving endpoint](images/topology-customer-triage.png)

A React-flow visualization of the agent topology: agent nodes, tools, sub-agents, and platform resources (UC functions, Genie spaces, vector indexes, serving endpoints), connected by typed edges (`uses-tool`, `delegates-to`, `calls-model`, `next-step`, `branch`, `iterates`). Click any node to inspect its details in a side panel — instructions, model, JSON Schema, resource identifier, or sub-agent URL. View-only in v1; no in-graph editing or live trace overlay yet.

Color coding follows NodeType: pink stroke for routing agents (`HandoffAgent`, `RouterAgent`), blue for `LlmAgent`, slate for tools, green/yellow/cyan/orange for UC functions / Genie spaces / vector indexes / serving endpoints respectively. Selection highlights the node and its incident edges.

Data is served from `GET /_apx/topology.json` (full graph) and `GET /_apx/topology/inspect/{node_id}` (per-node details). See the [topology spec](superpowers/specs/2026-05-22-topology-ui.md) for the full schema.

### `/_apx/edit` — Edit agent source
Loads the agent's `agent_router.py` (or equivalent entry module) into a browser editor with a preview-diff endpoint. Save writes the file and the dev server hot-reloads.

### `/_apx/probe` — Outbound connectivity tester
Pass `?url=<url>` to verify outbound network reachability from the App. Pairs with `/_apx/probe/checks` for a curated list of common Databricks endpoints (control plane, model serving, UC, Genie). Useful when an App can't reach a managed MCP / vector index / endpoint.

### `/_apx/setup` — First-run wizard
Picks a catalog/schema, a SQL warehouse, and seeds suggested tools and agent instructions. Writes to `pyproject.toml` and the project's `.env` so the next reload comes up configured. Surfaces a nudge from `/_apx/agent` when `DEMO_CATALOG` / `WAREHOUSE_ID` aren't set.

### `/_apx/eval` — Evalset + judge
Stores evaluation rows (`/_apx/eval/data`), runs them through the agent, and scores responses with an LLM-as-judge (`/_apx/eval/judge`). Lightweight regression harness for iterating on prompts and tool wiring.

## Build the topology UI

The topology surface is a Vite + React SPA in `python/dev-ui/topology/`. Maintainers rebuild it before publishing the wheel:

```bash
cd python/dev-ui/topology
npm install                 # one-time
npm run build               # tsc --noEmit + vite build
```

The build outputs to `python/src/apx_agent/_static/topology/` (`index.html`, `assets/*.js`, `assets/*.css`). The wheel ships that directory via `hatch`'s `force-include`:

```toml
[tool.hatch.build.targets.wheel.force-include]
"src/apx_agent/_static/topology" = "apx_agent/_static/topology"
```

End users get a working UI from `pip install apx-agent` without needing Node installed.

## Build a deployable wheel

`make wheel` packages the framework, copies the wheel into `hello-world/`, and updates the `uv.lock` hash for deploys:

    make wheel
