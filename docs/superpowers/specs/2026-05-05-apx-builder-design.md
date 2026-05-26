# apx-builder Design Spec

**Date:** 2026-05-05  
**Status:** Approved  
**Goal:** A Databricks App that lets a rep with no coding experience go from natural language description to a live deployed agent URL in under 15 minutes.

---

## Problem

The `databricks-multi-agent` vibe skill guides reps through agent building, but requires Claude Code, vibe, and technical familiarity. Reps who don't know the toolchain can't benefit. The AI Dev Kit Builder App is a web UI but is general-purpose — not optimized for the "build an apx-agent" flow.

## Solution

A purpose-built Databricks App — **apx-builder** — that each rep deploys once to their own workspace. It's a conversational UI that asks a few plain-English questions, then autonomously scaffolds and deploys an `apx-agent` app, returning a live URL.

The app is itself an `apx-agent` project — the flagship example of the framework.

---

## Architecture

### Stack

- **Frontend:** React chat UI (lifted from AI Dev Kit Builder App)
- **Backend:** FastAPI + `claude-agent-sdk`, same pattern as Builder App but stripped to essentials — no projects management, no multi-session persistence, no backup manager
- **Auth:** Databricks Apps OAuth via apx-agent OBO token propagation — runs as the rep's identity automatically, no PAT or env var configuration required
- **No cluster required:** all tools use REST APIs (Workspace Files API, Apps API, SQL warehouse)

### Repo location

`examples/apx-builder/` inside the `apx-agent` repo — maintained alongside the framework.

### Directory structure

```
examples/apx-builder/
  app.py              # apx-agent app — agent definition + create_app() = FastAPI server
  pyproject.toml
  app.yaml
  client/             # React frontend (chat UI)
  tools/
    discover_tables.py
    scaffold_project.py
    deploy_agent.py
    poll_deployment.py
  system_prompt.py
```

---

## Conversation Flow

### Discovery (3–6 messages, one question at a time)

1. **Opening:** *"What should your agent do? Describe it in plain English."*
2. **Data sources:** *"Which tables or data sources should it have access to?"* — agent calls `discover_tables()` in background and suggests real options from the rep's UC catalog
3. **Naming:** *"What should we call this agent?"* — agent suggests a slug derived from the use case
4. **Workspace (conditional):** *"Which Databricks profile should we deploy to?"* — skipped if only one profile is configured

### Build phase (fully autonomous)

After discovery, agent announces: *"Got everything I need — building your agent now."*

Tools run in sequence:

| Tool | What it does |
|------|-------------|
| `discover_tables(search)` | Queries UC to find real table names; used during discovery to suggest options |
| `scaffold_project(use_case, tables, genie_spaces, app_name)` | Writes `app.py`, `pyproject.toml`, `app.yaml` to Databricks Workspace at `/Users/{email}/apx-builder/{app_name}/` |
| `deploy_agent(app_name, workspace_path)` | Calls Databricks Apps deploy API |
| `poll_deployment(app_name)` | Two-stage readiness check (see below); returns live URL only after both pass |

### Finish

*"Your agent is live at `https://mcp-{app_name}.databricksapps.com`. It can answer questions about {tables}. Try asking it: [agent generates a concrete example question based on the discovered tables and use case]."*

---

## Scaffolded Output

Every built agent is a minimal apx-agent project with three files:

**`app.py`**
```python
from apx_agent import Agent, create_app, sql_tool, genie_tool, lineage_tool

agent = Agent(
    tools=[
        sql_tool("catalog.schema.table_a"),
        sql_tool("catalog.schema.table_b"),
        # genie_tool("space_id") if Genie space was identified
        # lineage_tool() if lineage was requested
    ],
    instructions="You are a data assistant for {use_case}. Answer questions using the available tables.",
)
app = create_app(agent)
```

**Tool selection logic:**
- `sql_tool()` — default for any UC table query
- `genie_tool("space_id")` — if rep mentions Genie; builder calls `list_genie_spaces()` to discover available spaces and presents them for selection (rep picks by name, not ID)
- `lineage_tool()` — included if rep asks about data discovery or column-level lineage
- Multiple tools can be combined in the same agent

**`pyproject.toml`** — `apx-agent` from the GitHub repo, `uv`-managed

**`app.yaml`** — standard uvicorn command for Databricks Apps

App name is prefixed `mcp-` for automatic discovery in Genie Code and AI Playground. Project files land at `/Users/{email}/apx-builder/{app_name}/` — visible in the workspace browser, editable by the rep if they want to go deeper.

### MVP scope

Single `Agent` only — no `RouterAgent`, `SequentialAgent`, `ParallelAgent`, etc. in v1. These patterns require the rep to understand agent topology; the goal for v1 is zero-friction single-agent deployment.

---

## poll_deployment: Two-Stage Readiness Check

This is a hard requirement. The Databricks Apps API reports `RUNNING` before the uvicorn process inside the container has bound to the port. Handing out the URL after Stage 1 alone causes 404s.

**Stage 1 — API readiness** (up to 120s, 5s intervals):
Poll `GET /apps/{app_name}` until:
- `app_status.state = RUNNING`
- `active_deployment.status.state = SUCCEEDED`

**Stage 2 — HTTP readiness** (up to 60s, 5s intervals):
Poll `GET {app_url}/health` until it returns HTTP 200. apx-agent exposes `/health` on every app it creates.

**Error handling:**
- Stage 1 timeout → report deployment failure with log excerpt
- Stage 2 timeout (Stage 1 passed) → return URL with warning: *"The app deployed but isn't responding yet — try in 30 seconds"*

The URL is returned to the rep **only after both stages pass**.

---

## Rep Setup (One-Time)

```bash
git clone https://github.com/stuagano/apx-agent
cd apx-agent/examples/apx-builder
databricks apps deploy apx-builder --source-code-path . --profile <profile>
```

No env vars, no `.env` files. Auth is fully automatic via Databricks Apps OAuth.

**Warehouse:** The builder detects available SQL warehouses on first load and selects the best available one automatically (using the same `get_best_warehouse` logic as the AI Dev Kit). Rep does not configure this manually.

---

## Success Criteria

- Rep with no coding experience opens the app URL, has a 5–10 minute conversation, receives a live Databricks App URL
- The deployed agent correctly answers questions about the tables it was configured with
- The rep can say "my agent is live" within 15 minutes of first opening the builder
- No 404s on handoff — the URL only appears after the health endpoint confirms the app is serving

---

## Out of Scope (v1)

- Multi-agent patterns (RouterAgent, SequentialAgent, ParallelAgent, HandoffAgent, LoopAgent)
- Iterating on existing agents (no session persistence across builder conversations)
- Custom tool code (only built-in apx-agent tools: sql_tool, genie_tool, lineage_tool)
- GitHub repo output
- Multi-workspace deployment (deploys to the rep's current workspace only)
