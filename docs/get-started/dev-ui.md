# Dev UI

Apps-hosted agents include built-in development tooling under the `/_apx/*` path
(mounted by `mount_mcp_endpoints` in both local `apx run` and deployed Apps).
Each surface is self-contained and styled to a shared dark theme; a fixed header
(rendered by `_ui_nav.py`) lets you jump between them.

On a **deployed App**, chat/Discover/Probe/Topology work behind workspace SSO.
Discover inventory GETs use the caller's OBO token and **fail closed** (401)
when it is missing — they do not list under the App service principal (#612).
**Write** routes (edit, create tools, replay) are allowed for any **signed-in
Apps user** (SSO / `X-Forwarded-Access-Token`). Discover wire/unwire additionally
requires `APX_DEV_UI_TOKEN` (#611). Optional `APX_DEV_UI_TOKEN` also covers
non-browser automation.

Model Serving deployments use AI Playground as the equivalent surface.

## Surfaces

### `/_apx/agent` — Chat shell
Tabbed shell (Chat · Edit · Eval · **Discover** · Probe) for testing the running
agent. Streams responses, surfaces tool calls inline, and hosts the other
`/_apx/*` pages in an iframe.

### `/_apx/discover` — Workspace peer + UC tool + API discovery
Auto-scans Databricks Apps for `/.well-known/agent.json` A2A cards, merges
UC-tagged apx models, lists Unity Catalog functions as tool candidates, and
surfaces Model Serving endpoints, Genie spaces, and Vector Search indexes
(with Managed MCP URLs where Databricks hosts them). Runs on page load;
Refresh re-scans. Backed by `GET /_apx/workspace-agents`,
`/_apx/workspace-functions`, and `/_apx/workspace-apis`.

**Wire-back (writes `agent.py` + hot-applies live):** pick a leaf Agent in the
**Wire into** dropdown, then:

- **Add as sub-agent** on Apps peers that have a URL — appends
  `$APX_PEER_<SLUG>_URL` to `sub_agents=`, writes the URL into `.env`, and
  materializes the remote tool on the **running** agent so Chat can use it
  immediately (no Apps redeploy)
- **Unwire** appears on peers already in `sub_agents=` for the selected leaf
- **Attach as tool** on UC functions, Genie spaces, and Vector Search indexes —
  splices `uc_function_tool` / `genie_tool` / `vector_search_tool` into
  `tools=` and registers them live
- Model Serving cards stay display-only (set `model=` / use Playground)

Mutating Discover / Topology wire calls on a **deployed App** require the
operator secret ``APX_DEV_UI_TOKEN`` (open Discover/Topology with
`?token=<secret>`). SSO alone is not enough — wire/unwire mutates the shared
live agent for every App user. Local ``apx-agent run`` stays open. Source is
still written so the next real deploy/restart stays consistent; prefer Chat
right after wire when the banner says live apply succeeded.

### `/_apx/topology` — Interactive topology graph

![topology view of customer_triage — HandoffAgent + 4 specialists + 8 tools + serving endpoint](images/topology-customer-triage.png)

A React-flow visualization of the agent topology: agent nodes, tools, sub-agents, and platform resources (UC functions, Genie spaces, vector indexes, serving endpoints), connected by typed edges (`uses-tool`, `delegates-to`, `calls-model`, `next-step`, `branch`, `iterates`). Click any node to inspect its details in a side panel — instructions, model, JSON Schema, resource identifier, or sub-agent URL.

**Drag-to-wire:** the left **Wire** palette lists workspace Apps peers, UC functions (when a catalog/schema is known), Genie spaces, and Vector Search indexes. Drag a chip onto a leaf Agent node (dashed green outline) to call the same Discover wire APIs — live hot-apply when possible, then the graph refreshes. Serving endpoints are not wireable as tools. Hidden in `?embed=1` minimap mode.

**Inspector Save / Unwire:** click a leaf Agent to edit `instructions=` and Save (writes `agent.py` + hot-applies live). Click a peer SubAgent (or a Discover-wired UC/Genie/VS tool) for **Unwire** — same Discover unwire APIs, graph refreshes.

**Chat dock:** a slim Chat panel on the right sends turns via `/responses` without leaving Topology. When the turn finishes, the amber last-turn route highlight refreshes automatically.

**Tracing + last turn:** click the header `traces → <experiment>` badge to set/change `MLFLOW_EXPERIMENT_ID` (SSO write). After a Chat turn (dock or `/_apx/agent`), the path that ran lights up in amber (`GET /_apx/traces/last-route`).

Color coding follows NodeType: pink stroke for routing agents (`HandoffAgent`, `RouterAgent`), blue for `LlmAgent`, slate for tools, green/yellow/cyan/orange for UC functions / Genie spaces / vector indexes / serving endpoints respectively. Selection highlights the node and its incident edges.

Data is served from `GET /_apx/topology.json` (full graph), `GET /_apx/topology/inspect/{node_id}` (per-node details), `GET /_apx/topology/tracing` (experiment destination), and `GET /_apx/traces/last-route` (last-turn highlight).

### `/_apx/edit` — Edit agent source
Loads the agent's `agent_router.py` (or equivalent entry module) into a browser editor with a preview-diff endpoint. Save writes the file to disk; the running agent is compiled at startup, so a save here — like any source change — takes effect on the next restart. Auto-reload on source changes happens only when you start the server with `apx-agent agents run --reload` (the `--reload` flag is off by default).

Tool authoring also lives here: the **New Tool** modal scaffolds a tool into the source — including a natural-language generator (`POST /_apx/tools/suggest`) that drafts the tool from a description. The standalone `/_apx/tools` inspector page has been retired in the Python dev UI (`/_apx/tools` now redirects to `/_apx/edit`); the standalone tools inspector survives only in the TypeScript dev UI.

### `/_apx/probe` — Outbound connectivity tester
Pass `?url=<url>` to verify outbound network reachability from the App. Pairs with `/_apx/probe/checks` for a curated list of common Databricks endpoints (control plane, model serving, UC, Genie). Useful when an App can't reach a managed MCP / vector index / endpoint.

### `/_apx/grounding` — OKF pack curation
Per-column description curation for the agent's `.apx/okf/` pack. Suggestions come from Unity Catalog COMMENTs (and optional AI Suggest). Edits write the local bundle only.

When the project has no pack but a DataAgent declares a `catalog.schema`, the empty state shows **Generate pack from `catalog.schema`** (`POST /_apx/grounding/generate`) — introspects via the UC Tables API, seeds descriptions from COMMENTs, writes `.apx/okf/` + `schema.json`, and wires `knowledge = "./.apx/okf"`. Restart the agent to load the new grounding.

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

The build outputs to `python/src/apx_agent/_static/topology/` (`index.html`, `assets/*.js`, `assets/*.css`). The wheel ships that directory automatically — `packages` already includes every file under `src/apx_agent/`, data files and all:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/apx_agent"]
```

Do **not** add a `force-include` for `_static/topology`: it maps the same files to the same wheel paths a second time, which newer `hatchling` rejects with a hard `ValueError` (failing the wheel build, and thus any `git+https` install).

End users get a working UI from `pip install apx-agent` without needing Node installed.

## Build a deployable wheel

`make wheel` packages the framework, copies the wheel into `hello-world/`, and updates the `uv.lock` hash for deploys:

    make wheel
