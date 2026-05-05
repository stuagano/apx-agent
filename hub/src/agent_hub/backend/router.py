"""Agent registry — dynamic registration + live A2A discovery.

Agents register by POSTing their URL. The registry crawls
/.well-known/agent.json to get name, description, tools, and MCP endpoint.
Static seed agents are kept for agents that haven't been deployed yet.
"""

from __future__ import annotations

import logging
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
    workstream: str = "",
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
            workstream=workstream,
            tags=tags or [],
        )


# Seed stubs for planned agents
_AGENTS["contract-parsing-agent"] = AgentCard(
    id="contract-parsing-agent",
    name="contract_parsing_agent",
    display_name="Contract Parsing",
    description="Extract structured data from utility contracts — pricing terms, service periods, SLAs, and regulatory clauses.",
    status="live",
    url="https://contract-parsing-agent-7474652869938903.aws.databricksapps.com",
    workstream="uplight-contract-parsing-genai",
    tags=["genai", "contracts", "vector-search"],
    supports_invoke=True,
    tools=[
        AgentTool(name="list_contracts", description="List contracts in the system"),
        AgentTool(name="get_contract_summary", description="Structured summary of a contract"),
        AgentTool(name="extract_pricing_terms", description="Pricing tiers, demand charges, escalation clauses"),
        AgentTool(name="search_contracts", description="Semantic search across contracts"),
    ],
)
_seed_stub(
    "entity-resolution-agent",
    "Entity Resolution",
    "Resolve and deduplicate customer/account entities across utility data systems.",
    workstream="uplight-fuzzy-match-entity-resolution",
    tags=["deduplication", "fuzzy-match", "master-data"],
    tools=[
        AgentTool(name="find_matching_accounts", description="Fuzzy-match customer accounts"),
        AgentTool(name="get_canonical_identity", description="Golden record for a resolved entity"),
        AgentTool(name="list_duplicate_clusters", description="Top duplicate account clusters"),
    ],
)

# Seed live agents (deployed apps with known tools)
_AGENTS["data-triage-agent"] = AgentCard(
    id="data-triage-agent",
    name="data_triage_agent",
    display_name="Data Triage",
    description="Investigate why data is missing from Databricks tables or APIs — traces lineage, checks job failures, and inspects source code.",
    status="live",
    url="https://mcp-data-triage-7474652869938903.aws.databricksapps.com",
    workstream="data-triage",
    tags=["lineage", "jobs", "sql", "genie", "python"],
    supports_invoke=True,
    tools=[
        AgentTool(name="run_sql_query", description="Execute a read-only SQL query"),
        AgentTool(name="get_table_info", description="Schema, row count, and freshness for a UC table"),
        AgentTool(name="get_table_lineage", description="Upstream sources via Unity Catalog lineage"),
        AgentTool(name="find_jobs_for_table", description="Jobs that write to a given table"),
        AgentTool(name="get_job_run_history", description="Recent run history for a job"),
        AgentTool(name="get_job_run_logs", description="Error output from a failed run"),
        AgentTool(name="get_job_source_paths", description="Notebook/file paths for a job"),
        AgentTool(name="list_genie_spaces", description="List available Genie Spaces"),
        AgentTool(name="query_genie_space", description="Ask a question to a Genie Space"),
        AgentTool(name="read_github_file", description="Read a source file from GitHub"),
        AgentTool(name="search_github_code", description="Search for code patterns in GitHub"),
    ],
)

_AGENTS["data-triage-agent-ts"] = AgentCard(
    id="data-triage-agent-ts",
    name="data_triage_agent_ts",
    display_name="Data Triage (TypeScript)",
    description="Investigate why data is missing from Databricks tables or APIs — TypeScript port with esbuild bundle deployment.",
    status="live",
    url="https://data-triage-agent-ts-7474652869938903.aws.databricksapps.com",
    workstream="data-triage",
    tags=["lineage", "jobs", "sql", "genie", "typescript"],
    supports_invoke=True,
    tools=[
        AgentTool(name="run_sql_query", description="Execute a read-only SQL query"),
        AgentTool(name="get_table_info", description="Schema, row count, and freshness for a UC table"),
        AgentTool(name="get_table_lineage", description="Upstream sources via Unity Catalog lineage"),
        AgentTool(name="find_jobs_for_table", description="Jobs that write to a given table"),
        AgentTool(name="get_job_run_history", description="Recent run history for a job"),
        AgentTool(name="get_job_run_logs", description="Error output from a failed run"),
        AgentTool(name="get_job_source_paths", description="Notebook/file paths for a job"),
        AgentTool(name="list_genie_spaces", description="List available Genie Spaces"),
        AgentTool(name="query_genie_space", description="Ask a question to a Genie Space"),
        AgentTool(name="read_github_file", description="Read a source file from GitHub"),
        AgentTool(name="search_github_code", description="Search for code patterns in GitHub"),
    ],
)

_AGENTS["data-inspector"] = AgentCard(
    id="data-inspector",
    name="data_inspector",
    display_name="Data Inspector",
    description="SQL queries, table schemas, Delta forensics (bisect, diff, audit) for Databricks tables.",
    status="live",
    url="https://data-inspector-7474652869938903.aws.databricksapps.com",
    workstream="data-triage",
    tags=["sql", "delta", "forensics"],
    supports_invoke=True,
    tools=[
        AgentTool(name="run_sql_query", description="Execute a read-only SQL query"),
        AgentTool(name="get_table_info", description="Schema, row count, freshness"),
    ],
)

_AGENTS["explain-my-bill"] = AgentCard(
    id="explain-my-bill",
    name="explain_my_bill",
    display_name="Explain My Bill",
    description="Help utility customers understand their energy bill — line items, charges, and usage patterns.",
    status="live",
    url="https://mcp-explain-my-bill-7474652869938903.aws.databricksapps.com",
    workstream="uplight-customer-facing-chatbot",
    tags=["customer-facing", "billing", "genai"],
    supports_invoke=True,
    tools=[],
)

# URLs for auto-crawl refresh (inter-app auth required)
_AUTO_REGISTER_URLS = [
    "https://data-triage-agent-ts-7474652869938903.aws.databricksapps.com",
    "https://mcp-data-triage-7474652869938903.aws.databricksapps.com",
    "https://data-inspector-7474652869938903.aws.databricksapps.com",
    "https://mcp-explain-my-bill-7474652869938903.aws.databricksapps.com",
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
    workstream: str = "",
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
        workstream=workstream,
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
    card = _card_from_a2a(a2a, req.url, workstream=req.workstream, tags=req.tags)
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

    updated = _card_from_a2a(a2a, agent.url, workstream=agent.workstream, tags=agent.tags)
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
            updated = _card_from_a2a(a2a, agent.url, workstream=agent.workstream, tags=agent.tags)
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
