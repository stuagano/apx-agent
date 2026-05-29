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
from .models import (
    AgentCard,
    AgentTool,
    InvokeRequest,
    RegisterRequest,
    VersionOut,
    is_trusted_agent_url,
)

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
    status="live" if _EXAMPLE_AGENT_URL else "stub",
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
async def register_agent(req: RegisterRequest, user_ws: Dependencies.UserClient):
    """Register an agent by URL. Crawls /.well-known/agent.json to populate the card.

    Requires an authenticated caller (Dependencies.UserClient binds the request
    to the user's OBO identity). ``req.url`` is validated as an HTTP(S) URL and
    checked against the trusted-host allowlist in ``RegisterRequest``.
    """
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

    # Authoritative trust gate: never forward the caller's OBO token to a host
    # outside the allowlist. Enforced here (not only at registration) because
    # seed / EXAMPLE_AGENT_URL / AGENT_HUB_AGENT_URLS agents bypass register.
    if not is_trusted_agent_url(agent.url):
        raise HTTPException(
            status_code=403,
            detail="Agent URL host is not on the trusted allowlist; refusing to forward credentials",
        )

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
