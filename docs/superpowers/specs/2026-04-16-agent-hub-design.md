# Agent Hub — Design Spec

A Databricks App built with apx-agent (Python) that serves as a unified catalog and chat interface for all agents in a workspace.

## Responsibilities

1. **Registry** — accepts self-registration from apx-agent apps (existing `POST /api/agents/register` pattern), discovers workspace serving endpoints and Genie spaces via Databricks SDK.
2. **Catalog UI** — searchable card grid at `/` showing all agents across three source types.
3. **Chat UI** — click any agent to open a chat session, switch agents mid-conversation.

## Project structure

```
hub/
├── src/hub/
│   ├── __init__.py
│   ├── app.py              # apx-agent create_app + hub-specific routes
│   ├── registry.py         # in-memory agent store + periodic refresh
│   ├── discovery.py        # workspace endpoint + Genie space scanner
│   ├── chat.py             # proxy chat requests to selected agent
│   └── models.py           # HubAgent unified model
├── frontend/
│   ├── index.html          # SPA shell
│   ├── app.js              # catalog + chat logic
│   └── style.css
├── pyproject.toml
├── app.yml                 # Databricks App manifest
└── databricks.yml          # DABs bundle config
```

## Unified agent model

Every agent, regardless of source, is normalized into one shape:

```python
class HubAgent(BaseModel):
    id: str                                         # deterministic key (source + name hash)
    name: str
    description: str
    source: Literal["apx", "serving_endpoint", "genie_space"]
    url: str | None = None                          # base URL for chat routing
    skills: list[HubSkill] = []                     # capabilities
    status: Literal["online", "offline", "unknown"] = "unknown"
    metadata: dict[str, Any] = {}                   # source-specific extras (endpoint type, space_id, etc.)

class HubSkill(BaseModel):
    name: str
    description: str
```

## Discovery

### apx-agent apps (Python and TypeScript)

Existing pattern, no changes needed on the agent side:

1. Agent starts up with `registry = "$AGENT_HUB_URL"` in config.
2. Agent calls `POST {hub_url}/api/agents/register` with `{"url": "<agent_public_url>"}`.
3. Hub fetches `{agent_url}/.well-known/agent.json` to populate the `HubAgent` entry.
4. Hub stores the agent in-memory with `source = "apx"`.

Periodic health check (every 60s): `GET {agent_url}/health`. Mark `offline` after 3 consecutive failures. Remove after 10 minutes offline.

### Workspace serving endpoints

On startup and every 5 minutes:

```python
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
endpoints = w.serving_endpoints.list()
for ep in endpoints:
    if ep.task == "llm/v1/chat" or ep.tags.get("agent"):
        # Convert to HubAgent with source="serving_endpoint"
```

Chat URL: the hub proxies via Databricks SDK rather than direct HTTP, so `url` stores the endpoint name rather than a full URL.

### Genie spaces

On startup and every 5 minutes:

```python
spaces = w.api_client.do("GET", "/api/2.0/genie/spaces")
for space in spaces["spaces"]:
    # Convert to HubAgent with source="genie_space"
    # metadata includes space_id for chat routing
```

## API endpoints

The hub is itself an apx-agent app, so it gets all standard endpoints (`/health`, `/.well-known/agent.json`, `/mcp`, etc.) plus:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/agents/register` | Accept agent self-registration (existing contract) |
| `GET` | `/api/agents` | List all agents (all sources). Query params: `?source=apx`, `?q=search` |
| `GET` | `/api/agents/{id}` | Get single agent details |
| `POST` | `/api/chat` | Proxy a chat message to a target agent |
| `GET` | `/` | Serve the frontend SPA |

### Registration endpoint

```python
@router.post("/api/agents/register")
async def register_agent(body: RegisterRequest) -> RegisterResponse:
    """Accept self-registration from apx-agent apps."""
    # body.url is the agent's public URL
    # Fetch /.well-known/agent.json from that URL
    # Store as HubAgent with source="apx"
    # Return {"id": agent_id}
```

### Chat proxy endpoint

```python
class ChatRequest(BaseModel):
    agent_id: str
    message: str
    conversation_id: str | None = None  # client-generated, for continuity

class ChatResponse(BaseModel):
    agent_id: str
    message: str
    conversation_id: str
```

Chat routing by source:
- **apx** → `POST {agent_url}/responses` with OpenAI-compatible message format
- **serving_endpoint** → `w.serving_endpoints.query(name, messages)` via Databricks SDK
- **genie_space** → Genie conversation API (`POST /api/2.0/genie/spaces/{space_id}/conversations` then `POST .../messages`)

## Frontend

Single-page app served as static files from `frontend/`. No build step — vanilla HTML/JS/CSS.

### Catalog view (default)

- Card grid showing all agents
- Each card: name, description, source badge ("App", "Endpoint", "Genie"), status dot (green/red/gray)
- Search bar filters by name and description
- Filter buttons by source type
- Click a card → opens chat view for that agent

### Chat view

- Left sidebar: list of agents (same as catalog but compact), current agent highlighted
- Main area: message thread with the selected agent
- Input bar at bottom
- Click a different agent in the sidebar → switches to that agent. Previous messages stay visible (grayed out) but new messages go to the new agent.
- Conversation state is client-side (messages array in JS). No server-side session storage.

### Agent switching behavior

When switching agents:
- The message history stays visible with a divider: "Switched to {agent_name}"
- New messages go to the new agent
- Each agent gets its own `conversation_id` so the backend can distinguish them
- The sidebar shows which agent is currently active

## Registry internals

```python
class AgentRegistry:
    """In-memory agent store with periodic refresh."""

    _agents: dict[str, HubAgent]          # id → agent
    _refresh_interval: int = 300          # 5 min for workspace discovery
    _health_interval: int = 60            # 1 min for apx-agent health checks

    async def register(self, url: str) -> HubAgent:
        """Register an apx-agent app by URL. Fetches its card."""

    async def refresh_workspace(self, ws: WorkspaceClient) -> None:
        """Scan serving endpoints + Genie spaces. Add/update/remove."""

    async def health_check(self) -> None:
        """Ping all apx-agent apps. Update status."""

    def list(self, source: str | None = None, query: str | None = None) -> list[HubAgent]:
        """List agents with optional filtering."""

    def get(self, agent_id: str) -> HubAgent | None:
        """Get a single agent by ID."""
```

Background tasks started on app startup:
- `refresh_workspace()` every 5 minutes
- `health_check()` every 60 seconds

## Auth

No custom auth. The hub runs as a Databricks App, so:
- All requests carry the user's OBO token
- Workspace API calls (serving endpoints, Genie spaces) use the OBO token
- Chat proxy passes OBO headers through to target agents
- Frontend is accessible to anyone with access to the Databricks App

## What this does NOT do

- No persistent storage — registry is in-memory, rebuilt on restart
- No agent orchestration — this is a catalog + chat, not a supervisor
- No conversation persistence — message history is client-side only
- No custom auth — relies entirely on Databricks App OBO
- No agent creation/management — read-only view of what exists

## Dependencies

```toml
[project]
dependencies = [
    "apx-agent",              # the toolkit itself (local path reference)
    "databricks-sdk>=0.74.0", # workspace API access
    "httpx>=0.27.0",          # async HTTP for agent proxying
]
```

The hub imports from `apx_agent` to use `create_app`, `Agent`, and `Dependencies`, making it both a consumer and a showcase of the toolkit.
