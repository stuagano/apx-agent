# Agent Hub

A two-panel web app for discovering and chatting with AI agents deployed on Databricks Apps.

## What it does

The Agent Hub maintains a registry of agents. Each agent card shows its status (live, stub, unreachable), description, and tool list. Selecting a live agent opens a chat panel that proxies messages to the agent's `/responses` endpoint with the user's forwarded auth token.

Agents can be registered four ways:
- **Workspace discovery** — on startup the hub lists Databricks Apps, probes each `/.well-known/agent.json`, and registers live apx agents before serving traffic. The UI also re-discovers on load (Refresh is optional).
- **Auto-register overlay** — set `AGENT_HUB_AGENT_URLS` to a comma-separated list of extra URLs crawled at startup (and on Discover)
- **On-demand** — POST to `/api/agents/register` with a `{"url": "..."}` body, or click **Discover workspace agents** in the UI (`POST /api/agents/discover-workspace`)
- **Seeded** — pre-populated stubs at startup (see `src/agent_hub/backend/router.py`)

For discovery and registration to work, the target agent must serve a `/.well-known/agent.json` discovery document (the A2A spec).

## Prerequisites

- Node.js 20+ and npm
- Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- [Databricks CLI](https://docs.databricks.com/dev-tools/cli/databricks-cli.html) configured with a profile

This project uses `apx-agent` as a path dependency — clone the full `apx-agent` repository rather than just this directory.

## Run locally

```bash
# Install Python deps
uv sync

# Install Node deps and start dev server
npm install
npm run dev       # starts Vite on http://localhost:5173

# In a separate terminal, start the backend
uv run uvicorn agent_hub.backend.app:app --reload
```

## Required env vars

| Variable | Description |
|---|---|
| `EXAMPLE_AGENT_URL` | URL of a deployed agent to seed as a live example (optional — hub starts without it) |
| `AGENT_HUB_AGENT_URLS` | Optional comma-separated overlay of agent URLs (in addition to automatic Apps discovery) |

## Deploy to Databricks Apps

```bash
# 1. Build the frontend
npm install && npm run build

# 2. Build the Python wheel
uv build --wheel

# 3. Upload wheel and requirements to your workspace
WHL=$(ls dist/*.whl | tail -1)
WHL_NAME=$(basename "$WHL")
databricks workspace import /path/in/workspace/"$WHL_NAME" \
  --file "$WHL" --format AUTO --overwrite --profile <profile>
echo "$WHL_NAME" > requirements.txt
databricks workspace import /path/in/workspace/requirements.txt \
  --file requirements.txt --format AUTO --overwrite --profile <profile>

# 4. Deploy the app
databricks apps deploy agent-hub \
  --source-code-path /path/in/workspace \
  --profile <profile>
```

Replace `/path/in/workspace` with a Databricks workspace path (e.g. `/Workspace/Users/you@company.com/agent-hub`).

## Registering your own agents

The hub **auto-discovers** running Databricks Apps that serve an apx A2A card on startup. Click **Discover workspace agents** in the UI to re-scan without restarting.

Optionally set `AGENT_HUB_AGENT_URLS` for agents that aren't Apps (or aren't listable yet).

To hard-code stubs or customize display names, edit `src/agent_hub/backend/router.py`.
