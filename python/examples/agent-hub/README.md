# Agent Hub

Central registry and chat interface for all your apx-agent deployments — **one FastAPI backend, one React app, under 300 lines of Python**.

Browse deployed agents, chat with them directly via streaming SSE, and link out to their full `/_apx/agent` dev UIs for traces, eval, and setup.

---

## What you'll learn

- How to build a **thin registry backend** that crawls A2A cards from deployed agents on startup
- How to **proxy SSE streams** from multiple downstream agents through a single hub
- How to seed an agent registry with **hardcoded cards, auto-crawl, or stubs** depending on how much you know at deploy time
- How to build a **React frontend** bundled into a FastAPI app for Databricks Apps deployment

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

## Prerequisites

| Requirement | Version / Notes |
|-------------|----------------|
| Python | 3.11+ |
| Node.js | 18+ (for the React frontend) |
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| apx-agent | General Databricks Apps development framework — this repo (`python/`) |
| Databricks CLI | `pip install databricks-cli` or `brew install databricks/tap/databricks` |
| Databricks workspace | Apps enabled; at least one deployed apx-agent app to register |

---

## Part 1: Workspace setup (one-time)

### Step 1: Deploy at least one other apx-agent example

The hub needs something to register. Deploy any other example in this repo first:

```bash
cd ../data-inspector      # or data-triage-agent, shortage-intelligence-agent, etc.
uv run apx-agent agents deploy
```

Note the deployed app URL — you'll need it to seed the hub in Part 2.

### Step 2: Install Node.js 18+

The React frontend requires Node.js. If you don't have it:

```bash
brew install node          # macOS with Homebrew
```

Or download from [nodejs.org](https://nodejs.org). Verify:

```bash
node --version   # should be v18+
npm --version
```

### Step 3: Note the URLs of agents to register

Collect the Databricks Apps URLs of the agents you want to show in the hub. They look like:

```
https://data-inspector-<workspace-hash>.databricksapps.com
https://shortage-intelligence-agent-<workspace-hash>.databricksapps.com
```

You'll use these in Part 2, Step 3.

---

## Part 2: Local development

### Step 1: Install Python deps

```bash
cd agent-hub
uv sync
```

### Step 2: Install Node deps and build the frontend

```bash
npm install
npm run build
```

The build output lands in `__dist__/` and is served by FastAPI as static files. You must run this before starting the server — the frontend is not rebuilt automatically on file changes.

> **Tip:** If you're actively developing the frontend, run `npm run dev` (Vite dev server) in a separate terminal alongside `uvicorn`. The Vite dev server proxies API calls to the FastAPI backend.

### Step 3: Seed your agents

Edit `api.py`. There are three seeding options:

**Option 1 — Hardcode a known agent** (fastest, no crawl needed):

```python
_AGENTS["my-agent"] = AgentCard(
    id="my-agent",
    name="my_agent",
    display_name="My Agent",
    description="What it does",
    status="live",
    url="https://my-agent-<workspace>.databricksapps.com",
    tags=["tag1"],
    supports_invoke=True,   # True if the agent exposes POST /responses
    tools=[
        AgentTool(name="tool_name", description="What the tool does"),
    ],
)
```

**Option 2 — Auto-crawl on startup** (discovers tools from A2A card at `/.well-known/agent.json`):

```python
_AUTO_REGISTER_URLS = [
    "https://my-agent-<workspace>.databricksapps.com",
    "https://another-agent-<workspace>.databricksapps.com",
]
```

**Option 3 — Seed a stub** for a planned agent not yet deployed:

```python
_AGENTS["planned-agent"] = AgentCard(
    id="planned-agent",
    name="planned_agent",
    display_name="Planned Agent",
    description="Coming soon",
    status="stub",
    url="",
    tags=["sql"],
    tools=[],
)
```

Set `supports_invoke=True` for agents that expose `POST /responses` for chat. Set `supports_invoke=False` for agents that need their own `/_apx/agent` full UI — the hub links out to them automatically.

### Step 4: Configure your Databricks CLI profile

```bash
databricks configure --profile my-workspace
# enter workspace URL and personal access token when prompted

databricks current-user me --profile my-workspace
# should return your user info
```

### Step 5: Run locally

```bash
uv run uvicorn app:app --port 8002 --reload
```

Open `http://localhost:8002`. The agent list shows the agents you seeded in Step 3. If you used `_AUTO_REGISTER_URLS`, the hub crawls them on startup — agents that are unreachable show as `unreachable` status rather than crashing.

---

## Part 3: Deploy to Databricks Apps

> **Important:** The React frontend must be built before deploying. If you skip `npm run build`, the deployed app will have no UI.

### Step 1: Build the frontend

```bash
npm run build
```

This regenerates `__dist__/` with the latest frontend code. Always run this before deploying if you've changed any UI files.

### Step 2: Deploy

```bash
uv run apx-agent agents deploy
```

### Step 3: Verify

```bash
databricks apps get agent-hub --profile my-workspace
# look for "state": "RUNNING"
```

Check the URL and status:

```bash
databricks apps get agent-hub --profile my-workspace -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('URL:   ', d.get('url', 'not yet available'))
print('State: ', d.get('app_status', {}).get('state', 'unknown'))
"
```

### Redeploy after changes

```bash
npm run build          # required if any UI files changed
uv run apx-agent agents deploy
```

---

## Configuration

No required env vars — the Databricks workspace client is injected by Apps for user auth forwarding. The hub uses `X-Forwarded-Access-Token` to forward the user's credentials when proxying chat requests to registered agents.

---

## Project structure

```
agent-hub/
├── app.py                           # FastAPI app + startup A2A crawl (uvicorn target: app:app)
├── api.py                           # Agent registry + /api/* routes — seed agents here
├── models.py                        # AgentCard, AgentTool, InvokeRequest
├── app.yml                          # Databricks Apps runtime config (no env vars needed)
├── databricks.yml                   # Asset Bundle config
├── package.json                     # Frontend deps (React, TanStack Router/Query, Tailwind)
├── vite.config.ts                   # Vite build config (outputs to __dist__/)
├── __dist__/                        # Vite build output (generated by `npm run build`)
└── client/                          # React frontend source
    ├── routes/
    │   ├── index.tsx                # Home — split pane agent list + streaming chat
    │   └── agents/                  # Agent detail page with Try It panel
    └── components/apx/              # Streaming SSE chat components
```

---

## Troubleshooting

**Agent list is empty after startup**
No agents are seeded. Add entries to `_AGENTS` or URLs to `_AUTO_REGISTER_URLS` in `api.py` and restart.

**Auto-crawled agent shows `unreachable`**
The target app is down or the URL is wrong. The hub logs a warning but doesn't crash. Fix the URL or deploy the target app, then call `POST /api/agents/{id}/refresh` to re-crawl.

**Chat returns nothing / SSE stream hangs**
The target agent must expose `POST /responses` with SSE. Verify `supports_invoke=True` on the card and that the agent is running. Test directly:

```bash
curl -N -X POST https://<agent-url>/responses \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"input": "hello", "stream": true}'
```

**Frontend not found (404 on `/`)**
You haven't built the frontend. Run `npm run build` and restart the server.

**Changes to `api.py` not reflected after deploy**
Seeding happens at import time. After changing `api.py`, you must redeploy (and rebuild the frontend if UI changed). There's no hot-reload in production.
