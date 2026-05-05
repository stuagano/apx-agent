# Agent Hub

A two-panel web app for discovering and chatting with AI agents deployed on Databricks Apps.

## What it does

The Agent Hub maintains a registry of agents. Each agent card shows its status (live, stub, unreachable), description, and tool list. Selecting a live agent opens a chat panel that proxies messages to the agent's `/responses` endpoint with the user's forwarded auth token.

Agents can be registered three ways:
- **Seeded** — pre-populated at startup (see `src/agent_hub/backend/router.py`)
- **Auto-register** — set `AGENT_HUB_AGENT_URLS` to a comma-separated list of URLs; the hub crawls each one at startup
- **On-demand** — POST to `/api/agents/register` with a `{"url": "..."}` body

## Required env vars

| Variable | Description |
|---|---|
| `EXAMPLE_AGENT_URL` | URL of a deployed agent to seed as a live example (optional — hub starts without it) |
| `AGENT_HUB_AGENT_URLS` | Comma-separated list of agent URLs to auto-register on startup |

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

## Registering your own agents

To add your own agents, edit `src/agent_hub/backend/router.py`. Replace the placeholder seeds with real `AgentCard` entries pointing to your deployed Databricks App URLs, or add them to `AGENT_HUB_AGENT_URLS` for auto-discovery.
