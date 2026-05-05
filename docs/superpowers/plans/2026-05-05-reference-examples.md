# Reference Examples Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the working Uplight agent-hub and three Python agents into the public `apx-agent` repo as clean, generic reference examples with all Uplight branding removed.

**Architecture:** Replace `hub/` entirely with a renamed copy of uplight-agent-hub (dropping `workstream` and hardcoded Uplight URLs). Add three Python examples to `python/examples/` — data-triage-agent, data-inspector, and contract-parsing-agent — with surface-level sanitization only (env-var the hardcoded URLs, genericize Uplight strings in prompts/comments, add READMEs).

**Tech Stack:** FastAPI + apx-agent SDK (Python), React + TanStack Router + TanStack Query + Tailwind + Vite (hub frontend), uv + hatchling (build), Databricks Apps deploy target.

---

## File Map

**Hub (replace `hub/` entirely):**
- Copy from: `/Users/stuart.gano/Documents/Customers/uplight/agents/uplight-agent-hub/`
- Target: `/Users/stuart.gano/Documents/apx-agent/hub/`
- Create: `hub/src/agent_hub/backend/models.py` — drop `workstream` field
- Modify: `hub/src/agent_hub/backend/router.py` — drop `workstream`, replace 6 Uplight agents with 3 generic seeds
- Modify: `hub/pyproject.toml` — rename, path dep for apx-agent, update metadata
- Modify: `hub/app.yml` — update uvicorn command
- Modify: `hub/vite.config.ts` — update `__APP_NAME__`, paths
- Modify: `hub/src/agent_hub/ui/lib/api.ts` — remove `workstream` from interfaces
- Modify: `hub/src/agent_hub/ui/routes/index.tsx` — change "Uplight Agent Hub" string
- Modify: `hub/package.json` — update name
- Create: `hub/README.md`

**Python examples (add to `python/examples/`):**
- Copy + sanitize: `data-triage-agent/` from `/Users/stuart.gano/Documents/Customers/uplight/agents/data-triage-agent/`
- Copy + sanitize: `data-inspector/` from `/Users/stuart.gano/Documents/Customers/uplight/agents/data-inspector/`
- Copy + sanitize: `contract-parsing-agent/` from `/Users/stuart.gano/Documents/Customers/uplight/uplight-contract-parsing-genai/contract-parsing-agent/`

---

### Task 0: Create branch

**Files:** none

- [ ] **Step 1: Create and check out branch**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git checkout main
git pull
git checkout -b reference-examples
```

Expected: branch `reference-examples` checked out.

- [ ] **Step 2: Commit**

```bash
git commit --allow-empty -m "chore: start reference-examples branch"
```

---

### Task 1: Copy hub and bulk-rename

**Files:**
- Remove: all existing content in `hub/`
- Create: full directory tree under `hub/` from uplight-agent-hub source

- [ ] **Step 1: Remove old hub content**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git rm -rf hub/
```

Expected: all hub/ files staged for deletion.

- [ ] **Step 2: Copy from uplight-agent-hub (excluding build artifacts and deps)**

```bash
rsync -a \
  --exclude='.venv' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.build' \
  --exclude='dist' \
  --exclude='uv.lock' \
  --exclude='*.whl' \
  --exclude='__dist__' \
  /Users/stuart.gano/Documents/Customers/uplight/agents/uplight-agent-hub/ \
  /Users/stuart.gano/Documents/apx-agent/hub/
```

Expected: hub/ contains pyproject.toml, app.yml, vite.config.ts, package.json, src/uplight_agent_hub/, etc.

- [ ] **Step 3: Rename the package directory**

```bash
mv /Users/stuart.gano/Documents/apx-agent/hub/src/uplight_agent_hub \
   /Users/stuart.gano/Documents/apx-agent/hub/src/agent_hub
```

- [ ] **Step 4: Bulk string replace in all text files**

```bash
find /Users/stuart.gano/Documents/apx-agent/hub/ -type f \
  \( -name "*.py" -o -name "*.ts" -o -name "*.tsx" -o -name "*.json" \
     -o -name "*.toml" -o -name "*.yml" -o -name "*.yaml" -o -name "*.html" \
     -o -name "*.css" \) \
| xargs sed -i '' \
    -e 's/uplight_agent_hub/agent_hub/g' \
    -e 's/uplight-agent-hub/agent-hub/g' \
    -e 's/Uplight Agent Hub/Agent Hub/g'
```

Expected: no remaining `uplight_agent_hub`, `uplight-agent-hub`, or `Uplight Agent Hub` strings in text files (verify below).

- [ ] **Step 5: Verify no Uplight strings remain in text files**

```bash
grep -r "uplight_agent_hub\|uplight-agent-hub\|Uplight Agent Hub" \
  /Users/stuart.gano/Documents/apx-agent/hub/ \
  --include="*.py" --include="*.ts" --include="*.tsx" \
  --include="*.toml" --include="*.yml" --include="*.json"
```

Expected: no output.

- [ ] **Step 6: Stage new hub files**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git add hub/
```

- [ ] **Step 7: Commit**

```bash
git commit -m "feat(hub): copy uplight-agent-hub and bulk-rename to agent_hub"
```

---

### Task 2: Hub models.py — drop workstream

**Files:**
- Modify: `hub/src/agent_hub/backend/models.py`

- [ ] **Step 1: Write the updated models.py**

Replace the full file at `hub/src/agent_hub/backend/models.py` with:

```python
from __future__ import annotations

from datetime import datetime
from importlib.metadata import version

from pydantic import BaseModel


class AgentTool(BaseModel):
    name: str
    description: str


class AgentCard(BaseModel):
    id: str
    name: str
    display_name: str
    description: str
    status: str
    url: str
    tools: list[AgentTool]
    tags: list[str] = []
    mcp_endpoint: str | None = None
    last_seen: datetime | None = None
    supports_invoke: bool = False


class RegisterRequest(BaseModel):
    url: str
    tags: list[str] = []


class InvokeRequest(BaseModel):
    input: str


class VersionOut(BaseModel):
    version: str

    @classmethod
    def from_metadata(cls) -> "VersionOut":
        try:
            v = version("agent-hub")
        except Exception:
            v = "dev"
        return cls(version=v)
```

- [ ] **Step 2: Verify no `workstream` field anywhere in models.py**

```bash
grep "workstream" /Users/stuart.gano/Documents/apx-agent/hub/src/agent_hub/backend/models.py
```

Expected: no output.

- [ ] **Step 3: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git add hub/src/agent_hub/backend/models.py
git commit -m "feat(hub): drop workstream from AgentCard and RegisterRequest"
```

---

### Task 3: Hub router.py — drop workstream and replace seeded agents

**Files:**
- Modify: `hub/src/agent_hub/backend/router.py`

- [ ] **Step 1: Write the updated router.py**

Replace the full file at `hub/src/agent_hub/backend/router.py` with:

```python
"""Agent registry — dynamic registration + live A2A discovery.

Agents register by POSTing their URL. The registry crawls
/.well-known/agent.json to get name, description, tools, and MCP endpoint.
Static seed agents are kept for agents that haven't been deployed yet.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx
from databricks.sdk.service.iam import User as UserOut
from fastapi import APIRouter, HTTPException, Request

from apx_agent import Dependencies
from .models import AgentCard, AgentTool, InvokeRequest, RegisterRequest, VersionOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# In-memory registry (persists for the lifetime of the app process)
# ---------------------------------------------------------------------------

_AGENTS: dict[str, AgentCard] = {}


def _seed_stub(
    agent_id: str,
    display_name: str,
    description: str,
    tags: list[str] | None = None,
    tools: list[AgentTool] | None = None,
) -> None:
    """Add a placeholder for an agent that isn't deployed yet."""
    if agent_id not in _AGENTS:
        _AGENTS[agent_id] = AgentCard(
            id=agent_id,
            name=agent_id.replace("-", "_"),
            display_name=display_name,
            description=description,
            status="stub",
            url="",
            tools=tools or [],
            tags=tags or [],
        )


# ---------------------------------------------------------------------------
# Generic seed agents — replace with your real agents
#
# 1. live-example  — wire a real deployed agent via EXAMPLE_AGENT_URL
# 2. planned-agent — stub for a planned-but-not-deployed agent
# 3. offline-agent — shows what an unreachable agent looks like in the UI
# ---------------------------------------------------------------------------

_EXAMPLE_AGENT_URL = os.environ.get("EXAMPLE_AGENT_URL", "")

_AGENTS["live-example"] = AgentCard(
    id="live-example",
    name="live_example",
    display_name="Live Example Agent",
    description=(
        "A deployed agent. Set EXAMPLE_AGENT_URL to point to your "
        "Databricks App and this card will be populated automatically on startup."
    ),
    status="live" if _EXAMPLE_AGENT_URL else "unreachable",
    url=_EXAMPLE_AGENT_URL,
    tags=["example"],
    tools=[],
    supports_invoke=bool(_EXAMPLE_AGENT_URL),
)

_seed_stub(
    "planned-agent",
    "Planned Agent",
    "A placeholder for an agent that is planned but not yet deployed. "
    "Stubs appear in the list but cannot be selected for chat.",
    tags=["planned"],
    tools=[
        AgentTool(name="example_tool", description="An example tool this agent will expose"),
    ],
)

_AGENTS["offline-agent"] = AgentCard(
    id="offline-agent",
    name="offline_agent",
    display_name="Offline Agent",
    description=(
        "An agent whose URL is configured but cannot be reached. "
        "The hub marks it unreachable after a failed crawl."
    ),
    status="unreachable",
    url="https://example.invalid",
    tags=["example"],
    tools=[],
)

# URLs to auto-crawl on startup. Set AGENT_HUB_AGENT_URLS to a comma-separated
# list of deployed agent base URLs. Each URL must serve /.well-known/agent.json.
_AUTO_REGISTER_URLS = [
    u.strip()
    for u in os.environ.get("AGENT_HUB_AGENT_URLS", "").split(",")
    if u.strip()
]


# ---------------------------------------------------------------------------
# A2A crawl helper
# ---------------------------------------------------------------------------

async def _crawl_agent(url: str) -> dict | None:
    """Fetch /.well-known/agent.json from a deployed agent. Returns None on failure."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{url.rstrip('/')}/.well-known/agent.json")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.warning("Failed to crawl %s: %s", url, e)
        return None


def _card_from_a2a(
    a2a: dict,
    url: str,
    tags: list[str] | None = None,
) -> AgentCard:
    """Build an AgentCard from an A2A discovery document."""
    name = a2a.get("name", "unknown")
    tools = [
        AgentTool(name=s["name"], description=s.get("description", "")[:200])
        for s in a2a.get("skills", [])
    ]
    return AgentCard(
        id=name.replace("_", "-"),
        name=name,
        display_name=name.replace("_", " ").title(),
        description=a2a.get("description", ""),
        status="live",
        url=url.rstrip("/"),
        tools=tools,
        tags=tags or [],
        mcp_endpoint=a2a.get("mcpEndpoint"),
        last_seen=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/version", response_model=VersionOut, operation_id="version")
async def version():
    return VersionOut.from_metadata()


@router.get("/current-user", response_model=UserOut, operation_id="currentUser")
def me(user_ws: Dependencies.UserClient):
    return user_ws.current_user.me()


@router.post("/agents/register", response_model=AgentCard, operation_id="registerAgent")
async def register_agent(req: RegisterRequest):
    """Register an agent by URL. Crawls /.well-known/agent.json to populate the card."""
    a2a = await _crawl_agent(req.url)
    if not a2a:
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch /.well-known/agent.json from {req.url}",
        )
    card = _card_from_a2a(a2a, req.url, tags=req.tags)
    _AGENTS[card.id] = card
    logger.info("Registered agent '%s' from %s (%d tools)", card.id, req.url, len(card.tools))
    return card


@router.get("/agents", response_model=list[AgentCard], operation_id="listAgents")
async def list_agents(status: str | None = None):
    """Return all agents. Optionally filter by status (live, stub, unreachable)."""
    agents = list(_AGENTS.values())
    if status:
        agents = [a for a in agents if a.status == status]
    return sorted(agents, key=lambda a: (a.status != "live", a.display_name))


@router.get("/agents/{agent_id}", response_model=AgentCard, operation_id="getAgent")
async def get_agent(agent_id: str):
    """Return a single agent by ID."""
    agent = _AGENTS.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return agent


@router.delete("/agents/{agent_id}", operation_id="deregisterAgent")
async def deregister_agent(agent_id: str):
    """Remove an agent from the registry."""
    if agent_id not in _AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    del _AGENTS[agent_id]
    return {"deleted": agent_id}


@router.post("/agents/{agent_id}/refresh", response_model=AgentCard, operation_id="refreshAgent")
async def refresh_agent(agent_id: str):
    """Re-crawl a registered agent's A2A card to update tools and status."""
    agent = _AGENTS.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    if not agent.url:
        raise HTTPException(status_code=400, detail="Agent has no URL (stub)")

    a2a = await _crawl_agent(agent.url)
    if not a2a:
        agent.status = "unreachable"
        agent.last_seen = None
        return agent

    updated = _card_from_a2a(a2a, agent.url, tags=agent.tags)
    _AGENTS[agent_id] = updated
    return updated


@router.post("/agents/refresh-all", response_model=list[AgentCard], operation_id="refreshAllAgents")
async def refresh_all_agents():
    """Re-crawl all registered agents with URLs."""
    results = []
    for agent_id, agent in list(_AGENTS.items()):
        if not agent.url:
            results.append(agent)
            continue
        a2a = await _crawl_agent(agent.url)
        if a2a:
            updated = _card_from_a2a(a2a, agent.url, tags=agent.tags)
            _AGENTS[agent_id] = updated
            results.append(updated)
        else:
            agent.status = "unreachable"
            agent.last_seen = None
            results.append(agent)
    return results


@router.get("/agents/{agent_id}/card", operation_id="getAgentA2ACard")
async def get_agent_a2a_card(agent_id: str):
    """Fetch the live A2A discovery card from a deployed agent."""
    agent = _AGENTS.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    if not agent.url:
        return {"error": "Agent URL not configured", "agent_id": agent_id}
    a2a = await _crawl_agent(agent.url)
    if not a2a:
        return {"error": "Agent unreachable", "agent_id": agent_id, "url": agent.url}
    return a2a


@router.post("/agents/{agent_id}/invoke", operation_id="invokeAgent")
async def invoke_agent(agent_id: str, req: InvokeRequest, request: Request):
    """Proxy a /responses call to the agent, forwarding the user's auth token."""
    agent = _AGENTS.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    if not agent.url:
        raise HTTPException(status_code=400, detail="Agent has no URL")

    token = request.headers.get("X-Forwarded-Access-Token")
    auth_headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{agent.url.rstrip('/')}/responses",
                json={"input": req.input},
                headers=auth_headers,
            )
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
```

- [ ] **Step 2: Verify no `workstream` anywhere in router.py**

```bash
grep "workstream" /Users/stuart.gano/Documents/apx-agent/hub/src/agent_hub/backend/router.py
```

Expected: no output.

- [ ] **Step 3: Verify no Uplight URLs**

```bash
grep "databricksapps.com\|uplight" /Users/stuart.gano/Documents/apx-agent/hub/src/agent_hub/backend/router.py
```

Expected: no output.

- [ ] **Step 4: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git add hub/src/agent_hub/backend/router.py
git commit -m "feat(hub): drop workstream, replace seeded agents with 3 generic examples"
```

---

### Task 4: Hub pyproject.toml + app.yml + vite.config.ts

**Files:**
- Modify: `hub/pyproject.toml`
- Modify: `hub/app.yml`
- Modify: `hub/vite.config.ts`

- [ ] **Step 1: Write the updated pyproject.toml**

Replace the full file at `hub/pyproject.toml`:

```toml
[project]
name = "agent-hub"
dynamic = ["version"]
description = "Portal for discovering and chatting with AI agents deployed on Databricks"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "apx-agent",
    "fastapi>=0.119.0",
    "pydantic-settings>=2.11.0",
    "uvicorn>=0.37.0",
    "databricks-sdk>=0.74.0",
    "httpx>=0.27.0",
]

[dependency-groups]
dev = [
    "ty>=0.0.12",
]

[tool.uv.sources]
apx-agent = { path = "../python/src", editable = true }

[tool.apx.metadata]
app-name = "agent-hub"
app-slug = "agent_hub"
app-entrypoint = "agent_hub.backend.app:app"
api-prefix = "/api"
metadata-path = "src/agent_hub/_metadata.py"

[tool.apx.ui]
ui-root = "src/agent_hub/ui"
dist-dir = "src/agent_hub/__dist__"

[tool.hatch.metadata]
allow-direct-references = true

[build-system]
requires = ["hatchling", "uv-dynamic-versioning>=0.7.0"]
build-backend = "hatchling.build"

[tool.hatch.version]
source = "uv-dynamic-versioning"

[tool.uv-dynamic-versioning]
vcs = "git"
fallback-version = "0.0.0"

[tool.hatch.build.hooks.version]
path = "src/agent_hub/_version.py"
template = '''
version = "{version}"
'''

[tool.hatch.build]
artifacts = ["src/agent_hub/__dist__", "src/agent_hub/_metadata.py"]
```

- [ ] **Step 2: Write the updated app.yml**

Replace the full file at `hub/app.yml`:

```yaml
command: ["uvicorn", "agent_hub.backend.app:app", "--workers", "2"]
```

- [ ] **Step 3: Write the updated vite.config.ts**

Replace the full file at `hub/vite.config.ts`:

```typescript
import path from "path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { TanStackRouterVite } from "@tanstack/router-plugin/vite";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [
    TanStackRouterVite({ routesDirectory: "./routes", generatedRouteTree: "./types/routeTree.gen.ts" }),
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src/agent_hub/ui") },
  },
  root: "./src/agent_hub/ui",
  build: {
    outDir: path.resolve(__dirname, "./src/agent_hub/__dist__"),
    emptyOutDir: true,
  },
  define: {
    __APP_NAME__: JSON.stringify("Agent Hub"),
  },
  server: { port: 1420, strictPort: true },
});
```

- [ ] **Step 4: Verify no uplight refs in these three files**

```bash
grep -i "uplight" \
  /Users/stuart.gano/Documents/apx-agent/hub/pyproject.toml \
  /Users/stuart.gano/Documents/apx-agent/hub/app.yml \
  /Users/stuart.gano/Documents/apx-agent/hub/vite.config.ts
```

Expected: no output.

- [ ] **Step 5: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git add hub/pyproject.toml hub/app.yml hub/vite.config.ts
git commit -m "feat(hub): update pyproject + app.yml + vite.config for agent_hub"
```

---

### Task 5: Hub frontend — api.ts, index.tsx, package.json

**Files:**
- Modify: `hub/src/agent_hub/ui/lib/api.ts`
- Modify: `hub/src/agent_hub/ui/routes/index.tsx`
- Modify: `hub/package.json`

- [ ] **Step 1: Remove `workstream` from `AgentCard` in api.ts**

In `hub/src/agent_hub/ui/lib/api.ts`, find the `AgentCard` interface (around line 15) and remove the `workstream?: string;` line:

```typescript
// BEFORE (remove this line):
    workstream?: string;

// The AgentCard interface should look like:
export interface AgentCard {
    description: string;
    display_name: string;
    id: string;
    last_seen?: string | null;
    mcp_endpoint?: string | null;
    name: string;
    status: string;
    supports_invoke?: boolean;
    tags?: string[];
    tools: AgentTool[];
    url: string;
}
```

- [ ] **Step 2: Remove `workstream` from `RegisterRequest` in api.ts**

In the same file, find the `RegisterRequest` interface and remove `workstream?: string;`:

```typescript
// BEFORE (remove this line):
    workstream?: string;

// The RegisterRequest interface should look like:
export interface RegisterRequest {
    tags?: string[];
    url: string;
}
```

- [ ] **Step 3: Verify workstream removed from api.ts**

```bash
grep "workstream" /Users/stuart.gano/Documents/apx-agent/hub/src/agent_hub/ui/lib/api.ts
```

Expected: no output.

- [ ] **Step 4: Change "Uplight Agent Hub" string in index.tsx**

In `hub/src/agent_hub/ui/routes/index.tsx`, find the hardcoded panel title and update it:

```typescript
// BEFORE (around line 91):
            <h1 className="font-semibold text-sm">Uplight Agent Hub</h1>

// AFTER:
            <h1 className="font-semibold text-sm">Agent Hub</h1>
```

- [ ] **Step 5: Update package.json name**

In `hub/package.json`, change the name field:

```json
// BEFORE:
    "name": "uplight_agent_hub_ui",

// AFTER:
    "name": "agent_hub_ui",
```

- [ ] **Step 6: Verify no uplight strings in these three files**

```bash
grep -i "uplight" \
  /Users/stuart.gano/Documents/apx-agent/hub/src/agent_hub/ui/lib/api.ts \
  /Users/stuart.gano/Documents/apx-agent/hub/src/agent_hub/ui/routes/index.tsx \
  /Users/stuart.gano/Documents/apx-agent/hub/package.json
```

Expected: no output.

- [ ] **Step 7: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git add hub/src/agent_hub/ui/lib/api.ts \
        hub/src/agent_hub/ui/routes/index.tsx \
        hub/package.json
git commit -m "feat(hub): remove workstream from frontend interfaces, update branding"
```

---

### Task 6: Hub build verify

**Files:**
- Create: `hub/src/agent_hub/_metadata.py` (build artifact, needed for uvicorn startup)

- [ ] **Step 1: Write `_metadata.py` so uvicorn can start**

The file at `hub/src/agent_hub/_metadata.py` is generated during `uv build` but must exist for local dev. If the rsync from Task 1 copied it with old content, overwrite it:

```python
from pathlib import Path

app_name = "agent-hub"
app_entrypoint = "agent_hub.backend.app:app"
app_slug = "agent_hub"
api_prefix = "/api"
dist_dir = Path(__file__).parent / "__dist__"
```

- [ ] **Step 2: Install npm dependencies**

```bash
cd /Users/stuart.gano/Documents/apx-agent/hub
npm install
```

Expected: `node_modules/` populated, no errors.

- [ ] **Step 3: Run TypeScript build**

```bash
cd /Users/stuart.gano/Documents/apx-agent/hub
npm run build
```

Expected: `src/agent_hub/__dist__/` created with `index.html` and assets. No TypeScript errors.

- [ ] **Step 4: Verify build output exists**

```bash
ls /Users/stuart.gano/Documents/apx-agent/hub/src/agent_hub/__dist__/
```

Expected: `index.html` and `assets/` directory present.

- [ ] **Step 5: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git add hub/src/agent_hub/_metadata.py
git commit -m "feat(hub): add _metadata.py and verify frontend build"
```

---

### Task 7: Hub README

**Files:**
- Create: `hub/README.md`

- [ ] **Step 1: Write the README**

Create `hub/README.md`:

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git add hub/README.md
git commit -m "docs(hub): add README"
```

---

### Task 8: data-triage-agent Python example

**Files:**
- Create: `python/examples/data-triage-agent/` (full copy from source)
- Modify: `python/examples/data-triage-agent/pyproject.toml`
- Modify: `python/examples/data-triage-agent/app.yml`
- Modify: `python/examples/data-triage-agent/databricks.yml`
- Modify: `python/examples/data-triage-agent/src/data_triage_agent/backend/agent_router.py`
- Modify: `python/examples/data-triage-agent/src/data_triage_agent/backend/pipeline.py`
- Create: `python/examples/data-triage-agent/README.md`

- [ ] **Step 1: Copy source to examples directory**

```bash
rsync -a \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.build' \
  --exclude='dist' \
  --exclude='uv.lock' \
  --exclude='*.whl' \
  /Users/stuart.gano/Documents/Customers/uplight/agents/data-triage-agent/ \
  /Users/stuart.gano/Documents/apx-agent/python/examples/data-triage-agent/
```

- [ ] **Step 2: Update pyproject.toml — path dep + static version**

Replace the full file at `python/examples/data-triage-agent/pyproject.toml`:

```toml
[project]
name = "data-triage-agent"
version = "0.1.0"
description = "Investigate why data is missing from Databricks tables — traces lineage, checks job failures, and inspects source code"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "apx-agent",
    "fastapi>=0.119.0",
    "pydantic-settings>=2.11.0",
    "uvicorn>=0.37.0",
    "databricks-sdk>=0.74.0",
    "httpx>=0.27.0",
]

[dependency-groups]
dev = [
    "ty>=0.0.12",
    "pytest>=8.0.0",
    "pytest-asyncio>=0.24.0",
    "respx>=0.21.0",
]

[tool.uv.sources]
apx-agent = { path = "../../src", editable = true }

[project.scripts]
investigate = "data_triage_agent.jobs.investigate:main"

[tool.apx.metadata]
app-name = "data-triage-agent"
app-slug = "data_triage_agent"
app-entrypoint = "data_triage_agent.backend.app:app"
api-prefix = "/api"
metadata-path = "src/data_triage_agent/_metadata.py"

[tool.apx.agent]
name = "data_triage_agent"
description = "Investigate why data is missing from Databricks tables or APIs — traces lineage, checks job failures, and inspects source code"
model = "databricks-claude-sonnet-4-6"
url = ""
registry = ""

[tool.hatch.metadata]
allow-direct-references = true

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/data_triage_agent"]
```

- [ ] **Step 3: Update app.yml — clear hardcoded DATA_INSPECTOR_URL value**

Replace the full file at `python/examples/data-triage-agent/app.yml`:

```yaml
command: ["uvicorn", "data_triage_agent.backend.app:app", "--workers", "2"]
env:
  - name: DATA_INSPECTOR_URL
    value: ""
```

- [ ] **Step 4: Update databricks.yml — clear hardcoded Uplight URL in dev target**

In `python/examples/data-triage-agent/databricks.yml`, find the `targets.dev.variables` section and clear the hardcoded URL value. Replace:

```yaml
targets:
  dev:
    mode: development
    default: true
    variables:
      data_inspector_url: https://mcp-data-inspector-7474652869938903.aws.databricksapps.com
```

With:

```yaml
targets:
  dev:
    mode: development
    default: true
    variables:
      data_inspector_url: ""
```

- [ ] **Step 5: Fix Uplight example in agent_router.py docstring**

In `python/examples/data-triage-agent/src/data_triage_agent/backend/agent_router.py`, find line ~172 (the `read_github_file` docstring):

```python
    # BEFORE:
    """Read a source file from a GitHub repository.
    Use to inspect transformation or filter logic in pipeline or API code.
    repo format: 'org/repo-name', e.g. 'uplight/demand-response-api'"""

    # AFTER:
    """Read a source file from a GitHub repository.
    Use to inspect transformation or filter logic in pipeline or API code.
    repo format: 'org/repo-name', e.g. 'my-org/my-repo'"""
```

- [ ] **Step 6: Fix Uplight catalog example in pipeline.py**

In `python/examples/data-triage-agent/src/data_triage_agent/backend/pipeline.py`, find the general agent instructions (around line 292) and replace the hardcoded UC catalog example:

```python
    # BEFORE:
    "  'List tables in serverless_stable_qh44kx_catalog.explain_my_bill'\n"

    # AFTER:
    "  'List tables in <catalog>.<schema>'\n"
```

- [ ] **Step 7: Verify no Uplight-specific strings remain**

```bash
grep -r "uplight\|databricksapps.com\|7474652869938903\|serverless_stable_qh44kx" \
  /Users/stuart.gano/Documents/apx-agent/python/examples/data-triage-agent/ \
  --include="*.py" --include="*.yml" --include="*.yaml" --include="*.toml"
```

Expected: no output.

- [ ] **Step 8: Write README.md**

Create `python/examples/data-triage-agent/README.md`:

```markdown
# Data Triage Agent

Investigates why data is missing from Databricks tables or downstream APIs.

## What it does

Runs a six-step investigation pipeline: confirms what data is missing, traces Unity Catalog lineage, checks job run history and error logs, queries Genie Spaces for domain context, inspects source code for filter logic, and synthesizes a root cause report. Delegates SQL queries and Delta forensics to a companion `data-inspector` sub-agent.

For non-investigation queries (table discovery, general SQL), routes to a general agent that also delegates to the data-inspector.

## Required env vars

| Variable | Description |
|---|---|
| `DATA_INSPECTOR_URL` | Base URL of the deployed `data-inspector` companion agent |
| `AGENT_HUB_URL` | (optional) URL of the Agent Hub to register with on startup |

## Deploy to Databricks Apps

```bash
# 1. Build the wheel
uv build --wheel

# 2. Upload and deploy (see apx-agent docs for full deploy workflow)
apx deploy --profile <your-profile>
```

## Tools

| Tool | Description |
|---|---|
| `run_sql_query` | Execute a read-only SQL query |
| `get_table_info` | Schema, row count, and freshness for a UC table |
| `get_table_lineage` | Upstream sources via Unity Catalog lineage |
| `find_jobs_for_table` | Jobs that write to a given table |
| `get_job_run_history` | Recent run history for a job |
| `get_job_run_logs` | Error output from a failed run |
| `get_job_source_paths` | Notebook/file paths for a job |
| `list_genie_spaces` | List available Genie Spaces |
| `query_genie_space` | Ask a question to a Genie Space |
| `read_github_file` | Read a source file from GitHub (stub — configure your GitHub token) |
| `search_github_code` | Search for code patterns in GitHub (stub) |
```

- [ ] **Step 9: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git add python/examples/data-triage-agent/
git commit -m "feat(examples): add data-triage-agent Python example"
```

---

### Task 9: data-inspector Python example

**Files:**
- Create: `python/examples/data-inspector/` (full copy from source)
- Modify: `python/examples/data-inspector/pyproject.toml`
- Create: `python/examples/data-inspector/README.md`

- [ ] **Step 1: Copy source to examples directory**

```bash
rsync -a \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.build' \
  --exclude='dist' \
  --exclude='uv.lock' \
  --exclude='*.whl' \
  /Users/stuart.gano/Documents/Customers/uplight/agents/data-inspector/ \
  /Users/stuart.gano/Documents/apx-agent/python/examples/data-inspector/
```

- [ ] **Step 2: Update pyproject.toml — path dep + static version**

Replace the full file at `python/examples/data-inspector/pyproject.toml`:

```toml
[project]
name = "data-inspector"
version = "0.1.0"
description = "Delta table forensics — binary search versions, diff changes, audit history, and SQL queries"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "apx-agent",
    "fastapi>=0.119.0",
    "pydantic-settings>=2.11.0",
    "uvicorn>=0.37.0",
    "databricks-sdk>=0.74.0",
    "httpx>=0.27.0",
]

[dependency-groups]
dev = [
    "ty>=0.0.12",
]

[tool.uv.sources]
apx-agent = { path = "../../src", editable = true }

[tool.apx.metadata]
app-name = "data-inspector"
app-slug = "data_inspector"
app-entrypoint = "data_inspector.backend.app:app"
api-prefix = "/api"
metadata-path = "src/data_inspector/_metadata.py"

[tool.apx.agent]
name = "data_inspector"
description = "Inspect and forensically analyze Delta tables — check data presence, binary search version history, diff versions, and audit who changed what"
model = "databricks-claude-sonnet-4-6"
url = ""
registry = ""

[tool.hatch.metadata]
allow-direct-references = true

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/data_inspector"]
```

- [ ] **Step 3: Verify no Uplight-specific strings**

```bash
grep -r "uplight\|databricksapps.com\|7474652869938903" \
  /Users/stuart.gano/Documents/apx-agent/python/examples/data-inspector/ \
  --include="*.py" --include="*.yml" --include="*.yaml" --include="*.toml"
```

Expected: no output.

- [ ] **Step 4: Write README.md**

Create `python/examples/data-inspector/README.md`:

```markdown
# Data Inspector

SQL queries, table schemas, and Delta forensics for Databricks tables.

## What it does

Provides tools to inspect, query, and forensically analyze Delta tables in Unity Catalog. Useful as a standalone agent or as a sub-agent invoked by a higher-level triage agent.

## Required env vars

| Variable | Description |
|---|---|
| `AGENT_HUB_URL` | (optional) URL of the Agent Hub to register with on startup |

## Deploy to Databricks Apps

```bash
uv build --wheel
apx deploy --profile <your-profile>
```

## Tools

| Tool | Description |
|---|---|
| `run_sql_query` | Execute a read-only SQL query |
| `get_table_info` | Schema, row count, and freshness |
| `list_catalogs` | List accessible Unity Catalog catalogs |
| `list_schemas` | List schemas in a catalog |
| `list_tables` | List tables in a schema |
| `search_tables` | Search tables by name pattern |
| `delta_bisect` | Binary search Delta history to find when data appeared/disappeared |
| `delta_bisect_column` | Binary search for when a specific value appeared in a column |
| `version_diff` | Compare two Delta versions to see what changed |
| `audit_lookup` | Who changed a table and when via Unity Catalog audit log |
```

- [ ] **Step 5: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git add python/examples/data-inspector/
git commit -m "feat(examples): add data-inspector Python example"
```

---

### Task 10: contract-parsing-agent Python example

**Files:**
- Create: `python/examples/contract-parsing-agent/` (full copy from source)
- Modify: `python/examples/contract-parsing-agent/pyproject.toml`
- Modify: `python/examples/contract-parsing-agent/agent.config.yaml`
- Modify: `python/examples/contract-parsing-agent/databricks.yml`
- Create: `python/examples/contract-parsing-agent/README.md`

- [ ] **Step 1: Copy source to examples directory**

```bash
rsync -a \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.build' \
  --exclude='dist' \
  --exclude='uv.lock' \
  --exclude='*.whl' \
  /Users/stuart.gano/Documents/Customers/uplight/uplight-contract-parsing-genai/contract-parsing-agent/ \
  /Users/stuart.gano/Documents/apx-agent/python/examples/contract-parsing-agent/
```

- [ ] **Step 2: Update pyproject.toml — path dep + static version**

Replace the full file at `python/examples/contract-parsing-agent/pyproject.toml`:

```toml
[project]
name = "contract-parsing-agent"
version = "0.1.0"
description = "Extract structured data from contracts using GenAI — identifies pricing terms, service periods, SLAs, and regulatory clauses"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "apx-agent",
    "fastapi>=0.119.0",
    "pydantic-settings>=2.11.0",
    "uvicorn>=0.37.0",
    "databricks-sdk>=0.74.0",
    "httpx>=0.27.0",
    "pymupdf>=1.24.0",
    "reportlab>=4.2.0",
    "pyyaml>=6.0.2",
    "mlflow>=2.18.0",
]

[dependency-groups]
dev = [
    "ty>=0.0.12",
    "pytest>=8.3.0",
    "pytest-asyncio>=0.24.0",
]

[tool.uv.sources]
apx-agent = { path = "../../src", editable = true }

[tool.pytest.ini_options]
markers = [
  "integration: requires live Databricks workspace (FM API)",
]
addopts = "-m 'not integration'"
filterwarnings = [
  "ignore:Field name \"schema\".*shadows:UserWarning",
]

[tool.apx.metadata]
app-name = "contract-parsing-agent"
app-slug = "contract_parsing_agent"
app-entrypoint = "contract_parsing_agent.backend.app:app"
api-prefix = "/api"
metadata-path = "src/contract_parsing_agent/_metadata.py"

[tool.apx.agent]
name = "contract_parsing_agent"
description = "Extract structured data from contracts — identifies pricing terms, service periods, SLAs, and regulatory clauses from uploaded PDF or text documents"
model = "databricks-claude-sonnet-4-6"
url = ""
registry = ""

[tool.hatch.metadata]
allow-direct-references = true

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/contract_parsing_agent"]
```

- [ ] **Step 3: Update agent.config.yaml — genericize system prompt and demo questions**

Replace the full file at `python/examples/contract-parsing-agent/agent.config.yaml`:

```yaml
# Reusability surface. A cloning team edits this file (and brings their own
# generator/data) — everything else in this repo is generic.
#
# Deployment-specific values (catalog, schema, volumes, sub_agents) are set
# via environment variables. See .env.example.

model: databricks-claude-sonnet-4-6

tables:
  primary: contracts
  ground_truth: contracts_ground_truth

system_prompt: |
  You are an expert contracts analyst. You help teams understand a portfolio
  of contracts (interconnection agreements, power purchase agreements,
  demand-response contracts, service agreements).

  When the user asks about specific contracts or filters across the
  portfolio, call the appropriate tool. When you don't know which tool fits,
  delegate to the data-inspector sub-agent's run_sql_query.

  Never fabricate contracts, counterparties, or values. If a query returns
  no rows, say so plainly.

  When a user message contains a volume_path, your first action must be to
  call extract_new_contract with that path. After successful extraction,
  always call query_portfolio to find existing contracts from the same
  counterparty and provide a term-by-term comparison.

demo_questions:
  - "Which contracts have auto-renewal and expire in the next 90 days?"
  - "Summarize the pricing terms across contracts from Counterparty A."
  - "Show me the counterparties with the largest demand-response exposure."
  - "What's the average term length, grouped by counterparty?"

extraction_schema:
  type: object
  required:
    - counterparty
    - contract_type
    - effective_date
    - expiry_date
    - term_years
    - pricing_model
    - pricing_summary
    - auto_renewal
  properties:
    counterparty:
      type: string
      description: The counterparty entity name.
    contract_type:
      type: string
      enum: [interconnection, ppa, demand_response, tariff, service]
    effective_date:
      type: string
      format: date
    expiry_date:
      type: string
      format: date
    term_years:
      type: number
    pricing_model:
      type: string
      enum: [fixed, indexed, tiered, time_of_use]
    pricing_summary:
      type: string
      description: One-sentence plain-English summary of pricing terms.
    auto_renewal:
      type: boolean
    sla_uptime_pct:
      type: number
      description: Required uptime %, if specified.
    notes:
      type: string
```

- [ ] **Step 4: Update databricks.yml — clear hardcoded Uplight dev target values**

In `python/examples/contract-parsing-agent/databricks.yml`, find the `targets.dev.variables` section and replace hardcoded Uplight UC values:

```yaml
# BEFORE:
targets:
  dev:
    mode: development
    default: true
    variables:
      catalog: serverless_stable_qh44kx_catalog
      schema: chatbot_contracts
      volumes_raw: /Volumes/serverless_stable_qh44kx_catalog/chatbot_contracts/raw_contracts
      volumes_uploads: /Volumes/serverless_stable_qh44kx_catalog/chatbot_contracts/uploads

# AFTER:
targets:
  dev:
    mode: development
    default: true
    variables:
      catalog: ""
      schema: ""
      volumes_raw: ""
      volumes_uploads: ""
```

- [ ] **Step 5: Verify no Uplight-specific strings remain**

```bash
grep -r "uplight\|PG.E\|databricksapps.com\|7474652869938903\|serverless_stable_qh44kx" \
  /Users/stuart.gano/Documents/apx-agent/python/examples/contract-parsing-agent/ \
  --include="*.py" --include="*.yml" --include="*.yaml" --include="*.toml"
```

Expected: no output. (Note: `PG.E` regex matches "PG&E" or "PG E".)

- [ ] **Step 6: Write README.md**

Create `python/examples/contract-parsing-agent/README.md`:

```markdown
# Contract Parsing Agent

Extract structured data from contracts using GenAI — identifies pricing terms, service periods, SLAs, and regulatory clauses.

## What it does

Maintains a portfolio of parsed contracts in Unity Catalog. Supports uploading new contracts (PDF or text) via a `/upload` endpoint, which triggers GenAI extraction into a structured schema. Provides tools to query the portfolio, summarize individual contracts, and search semantically across the corpus using vector search.

## Required env vars

| Variable | Description |
|---|---|
| `CATALOG` | Unity Catalog catalog name |
| `SCHEMA` | Unity Catalog schema name |
| `VOLUMES_RAW` | Full UC volume path for raw contract files (e.g. `/Volumes/<catalog>/<schema>/raw`) |
| `VOLUMES_UPLOADS` | Full UC volume path for uploaded contracts |
| `AGENT_HUB_URL` | (optional) URL of the Agent Hub to register with on startup |

## Deploy to Databricks Apps

```bash
# 1. Provision the Unity Catalog tables and volumes
# Run the provisioning notebook in notebooks/

# 2. Build and deploy
uv build --wheel
apx deploy --profile <your-profile>
```

## Tools

| Tool | Description |
|---|---|
| `query_portfolio` | Filter and list contracts by counterparty, type, date range |
| `get_contract_summary` | Structured summary of a specific contract |
| `extract_pricing_terms` | Pricing tiers, demand charges, escalation clauses |
| `search_contracts` | Semantic search across contracts via vector search |
| `find_contracts_expiring` | Contracts expiring within N days |
| `extract_new_contract` | Extract and store a new contract from a volume path |
```

- [ ] **Step 7: Commit**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git add python/examples/contract-parsing-agent/
git commit -m "feat(examples): add contract-parsing-agent Python example"
```

---

## Self-Review

### Spec Coverage Check

| Spec requirement | Task |
|---|---|
| Replace hub/ entirely with uplight-agent-hub stripped of branding | Tasks 1–7 |
| Drop `workstream` from AgentCard and RegisterRequest | Tasks 2, 5 |
| Generic placeholder seeded agents (live + stub + unreachable) | Task 3 |
| `_AUTO_REGISTER_URLS` → env var AGENT_HUB_AGENT_URLS | Task 3 |
| Remove hardcoded *.databricksapps.com URLs | Tasks 3, 8, 9, 10 |
| Hub display title "Agent Hub" | Tasks 4, 5 |
| Hub README | Task 7 |
| data-triage-agent Python example | Task 8 |
| data-inspector Python example | Task 9 |
| contract-parsing-agent Python example | Task 10 |
| Genericize contract agent system prompt (remove "for Uplight", PG&E) | Task 10 |
| TypeScript example SKIPPED (pipeline agent, already covered) | — |

All spec requirements covered. No TypeScript example added (the existing `typescript/examples/pipeline-agent/` already covers the same pattern as `data-triage-agent-ts`).
