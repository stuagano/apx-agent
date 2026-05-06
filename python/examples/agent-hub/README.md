# Agent Hub

Central registry and chat interface for all your apx-agent deployments — **one FastAPI backend, one React app, under 300 lines of Python**.

Browse deployed agents, chat with them directly via streaming SSE, and link out to their full `/_apx/agent` dev UIs for traces, eval, and setup.

---

## What makes this simple

A thin registry backend with five API routes:

```python
POST /api/agents/register     # Crawl /.well-known/agent.json and register
GET  /api/agents              # List all agents (live, stub, unreachable)
GET  /api/agents/{id}         # Single agent detail
POST /api/agents/{id}/invoke  # Proxy /responses with SSE streaming
POST /api/agents/{id}/refresh # Re-crawl A2A card
```

The invoke proxy streams SSE directly from the agent:

```python
async def _stream_proxy():
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", f"{agent.url}/responses",
                                 json={"input": req.input, "stream": True},
                                 headers=auth_headers) as r:
            async for chunk in r.aiter_bytes():
                yield chunk

return StreamingResponse(_stream_proxy(), media_type="text/event-stream")
```

The React frontend reads `output_text.delta` SSE events and streams text into the message bubble — no page refresh, no polling.

---

## Seeding agents

Add agents in `router.py` — hardcode known ones or auto-crawl on startup:

```python
# Hardcode a known agent (no crawl required):
_AGENTS["my-agent"] = AgentCard(
    id="my-agent",
    name="my_agent",
    display_name="My Agent",
    description="What it does",
    status="live",
    url="https://my-agent-<workspace>.databricksapps.com",
    tags=["tag1"],
    supports_invoke=True,
    tools=[AgentTool(name="tool_name", description="What the tool does")],
)

# Auto-crawl on startup (discovers tools from /.well-known/agent.json):
_AUTO_REGISTER_URLS = [
    "https://my-agent-<workspace>.databricksapps.com",
]
```

Set `supports_invoke=True` for agents that use the `/responses` proxy. Set `supports_invoke=False` for agents that need their own `/_apx/agent` full UI — the hub links out to them automatically.

---

## Run locally

```bash
git clone https://github.com/stuagano/apx-agent
cd python/examples/agent-hub

# Install Python deps
uv sync

# Build the React frontend
npm install
npm run build

# Start the hub
uv run uvicorn agent_hub.backend.app:app --port 8002
```

Open http://localhost:8002. The agent list will be empty until you seed `router.py` with your deployed agents.

---

## Deploy to Databricks Apps

### Prerequisites

- **Databricks CLI** — [install](https://docs.databricks.com/dev-tools/cli/databricks-cli.html)
- **uv** — `pip install uv`
- A Databricks workspace with [Apps enabled](https://docs.databricks.com/en/dev-tools/databricks-apps/index.html)
- At least one deployed apx-agent app to register

### 1. Authenticate

```bash
databricks auth login --host https://<your-workspace>.azuredatabricks.net
databricks current-user me
```

### 2. Seed your agents

Edit `src/agent_hub/backend/router.py` and add your deployed agents to `_AGENTS` or `_AUTO_REGISTER_URLS`.

### 3. Build

```bash
npm run build
uv build --wheel -o .build/
ls .build/*.whl | xargs basename > .build/requirements.txt
```

### 4. Deploy

```bash
databricks bundle deploy
```

Check status:

```bash
databricks apps get agent-hub -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('URL:   ', d.get('url', 'not yet available'))
print('State: ', d.get('app_status', {}).get('state', 'unknown'))
"
```

### Redeploy after changes

```bash
npm run build
uv build --wheel -o .build/
ls .build/*.whl | xargs basename > .build/requirements.txt
databricks bundle deploy
```

---

## Configuration

No required env vars — the workspace client is injected by Databricks Apps for user auth forwarding.

---

## Project Structure

```
agent-hub/
├── app.yml                          # Databricks Apps runtime config
├── databricks.yml                   # Asset Bundle — build, deploy, app resource
├── package.json                     # Frontend deps (React, TanStack Router/Query, Tailwind)
├── vite.config.ts                   # Vite build config
└── src/agent_hub/
    ├── backend/
    │   ├── app.py                   # FastAPI app + startup A2A crawl
    │   ├── router.py                # Agent registry + /api/* routes — seed agents here
    │   └── models.py                # AgentCard, AgentTool, InvokeRequest
    └── ui/
        ├── routes/
        │   ├── index.tsx            # Home — split pane agent list + streaming chat
        │   └── agents/$agentId.tsx  # Agent detail page with Try It panel
        └── components/apx/
            └── ChatPanel.tsx        # Streaming SSE chat component
```
