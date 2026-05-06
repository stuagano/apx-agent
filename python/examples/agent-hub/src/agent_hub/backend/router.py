"""Agent registry — dynamic registration + live A2A discovery.

Agents register by POSTing their URL. The registry crawls
/.well-known/agent.json to get name, description, tools, and MCP endpoint.

Seed agents in the block below, or add them at runtime via POST /api/agents/register.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from databricks.sdk.service.iam import User as UserOut
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from apx_agent import Dependencies
from .models import AgentCard, AgentTool, InvokeRequest, RegisterRequest, VersionOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# In-memory registry
# ---------------------------------------------------------------------------

_AGENTS: dict[str, AgentCard] = {}

# ---------------------------------------------------------------------------
# Seed your agents here. Two options:
#
# Option 1 — hardcode a known agent (fastest, no crawl needed):
#
#   _AGENTS["my-agent"] = AgentCard(
#       id="my-agent",
#       name="my_agent",
#       display_name="My Agent",
#       description="What it does",
#       status="live",
#       url="https://my-agent-<workspace>.databricksapps.com",
#       tags=["tag1"],
#       supports_invoke=True,   # True if the agent exposes POST /responses
#       tools=[
#           AgentTool(name="tool_name", description="What the tool does"),
#       ],
#   )
#
# Option 2 — auto-crawl on startup (discovers tools from A2A card):
#
#   _AUTO_REGISTER_URLS = [
#       "https://my-agent-<workspace>.databricksapps.com",
#   ]
#
# Option 3 — seed a stub for a planned agent:
#
#   _AGENTS["planned-agent"] = AgentCard(
#       id="planned-agent",
#       name="planned_agent",
#       display_name="Planned Agent",
#       description="Coming soon",
#       status="stub",
#       url="",
#       tags=["sql"],
#       tools=[],
#   )
# ---------------------------------------------------------------------------

_AUTO_REGISTER_URLS: list[str] = []


# ---------------------------------------------------------------------------
# A2A crawl helpers
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
        supports_invoke=True,
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
    agents = list(_AGENTS.values())
    if status:
        agents = [a for a in agents if a.status == status]
    return sorted(agents, key=lambda a: (a.status != "live", a.display_name))


@router.get("/agents/{agent_id}", response_model=AgentCard, operation_id="getAgent")
async def get_agent(agent_id: str):
    agent = _AGENTS.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return agent


@router.delete("/agents/{agent_id}", operation_id="deregisterAgent")
async def deregister_agent(agent_id: str):
    if agent_id not in _AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    del _AGENTS[agent_id]
    return {"deleted": agent_id}


@router.post("/agents/{agent_id}/refresh", response_model=AgentCard, operation_id="refreshAgent")
async def refresh_agent(agent_id: str):
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


@router.post("/agents/{agent_id}/invoke", operation_id="invokeAgent")
async def invoke_agent(agent_id: str, req: InvokeRequest, request: Request):
    """Proxy a /responses call to the agent as an SSE stream, forwarding the user's auth token."""
    agent = _AGENTS.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    if not agent.url:
        raise HTTPException(status_code=400, detail="Agent has no URL")

    token = request.headers.get("X-Forwarded-Access-Token")
    auth_headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def _stream_proxy():
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    f"{agent.url.rstrip('/')}/responses",
                    json={"input": req.input, "stream": True},
                    headers=auth_headers,
                ) as r:
                    r.raise_for_status()
                    async for chunk in r.aiter_bytes():
                        yield chunk
        except httpx.HTTPStatusError as e:
            yield f"event: error\ndata: {e.response.text}\n\n".encode()
        except Exception as e:
            yield f"event: error\ndata: {e}\n\n".encode()

    return StreamingResponse(_stream_proxy(), media_type="text/event-stream")
