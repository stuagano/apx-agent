"""Dev UI hub routes, split out of build_dev_ui_router (#1052)."""

from __future__ import annotations

from fastapi import APIRouter

from .context import DevRouteContext

from apx_agent._dev import (
    Any,
    HTMLResponse,
    HTTPException,
    Request,
    _crawl_agent,
    logger,
    os,
)


def register(router: APIRouter, ctx: DevRouteContext) -> None:
    """Attach this family's routes to the guarded dev router."""


    @router.get("/_apx/hub", include_in_schema=False)
    async def hub_ui() -> Any:
        """Hub: list caller's registered agents with register/refresh/invoke/delete."""
        from apx_agent._ui_hub import render_hub_ui

        return HTMLResponse(render_hub_ui())

    # -----------------------------------------------------------------------
    # Hub routes: register/list/get/deregister per-caller agent registrations
    # -----------------------------------------------------------------------

    @router.post("/_apx/hub/agents", response_model=Any)
    async def hub_register_agent(request: Request) -> Any:
        """Register an agent by URL (caller-scoped).

        Validates the URL against the SSRF/Apps-host allowlist, crawls
        /.well-known/agent.json to populate the card, and stores it per caller.
        Fails closed (401) without OBO on a deployed App.
        """
        from apx_agent._hub_models import RegisterRequest, AgentCard, AgentTool
        from apx_agent._hub_store import HUB_STORE
        from apx_agent._obo import extract_obo_headers

        # Resolve caller principal from request headers
        obo = extract_obo_headers(custom_inputs={}, headers=request.headers)
        principal = obo.get("user_id") or obo.get("user_email")
        if not principal:
            raise HTTPException(
                status_code=401,
                detail="No user identity in request headers",
            )

        # Parse and validate the request
        # RegisterRequest validates the URL hostname against the trusted-host allowlist
        try:
            body = await request.json()
            reg = RegisterRequest(**body)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid request: {e}") from e

        # Crawl the agent's A2A card
        a2a = await _crawl_agent(reg.url)

        if not a2a:
            raise HTTPException(
                status_code=502,
                detail=f"Could not fetch /.well-known/agent.json from {reg.url}",
            )

        # Build the agent card from the crawl. The card's own tools win;
        # skills are the fallback for cards that only advertise those.
        name = a2a.get("name", "unknown")
        declared = a2a.get("tools") or []
        if declared:
            tools = [
                AgentTool(
                    name=t.get("name", ""),
                    description=str(t.get("description", ""))[:200],
                )
                for t in declared
                if isinstance(t, dict) and t.get("name")
            ]
        else:
            tools = [
                AgentTool(name=s["name"], description=s.get("description", "")[:200])
                for s in a2a.get("skills", [])
            ]
        card = AgentCard(
            id=name.replace("_", "-"),
            name=name,
            display_name=name.replace("_", " ").title(),
            description=a2a.get("description", ""),
            status="live",
            url=reg.url.rstrip("/"),
            tools=tools,
            instructions=str(a2a.get("instructions", ""))[:2000],
            trace_header=str(a2a.get("trace_header", ""))[:200],
            tags=reg.tags,
            mcp_endpoint=a2a.get("mcpEndpoint"),
        )

        # Store in the per-caller registry
        HUB_STORE.put(principal, card)

        return card.model_dump()

    @router.get("/_apx/hub/agents", response_model=Any)
    async def hub_list_agents(request: Request) -> Any:
        """List the caller's registered agents (caller-scoped)."""
        from apx_agent._hub_store import HUB_STORE
        from apx_agent._obo import extract_obo_headers

        # Resolve caller principal from request headers
        obo = extract_obo_headers(custom_inputs={}, headers=request.headers)
        principal = obo.get("user_id") or obo.get("user_email")
        if not principal:
            raise HTTPException(
                status_code=401,
                detail="No user identity in request headers",
            )

        # Get the caller's agents
        agents = HUB_STORE.list(principal)
        return [a.model_dump() for a in agents]

    @router.get("/_apx/hub/agents/{agent_id}", response_model=Any)
    async def hub_get_agent(request: Request, agent_id: str) -> Any:
        """Get a single agent by ID (caller-scoped, 404 if not the caller's)."""
        from apx_agent._hub_store import HUB_STORE
        from apx_agent._obo import extract_obo_headers

        # Resolve caller principal
        obo = extract_obo_headers(custom_inputs={}, headers=request.headers)
        principal = obo.get("user_id") or obo.get("user_email")
        if not principal:
            raise HTTPException(
                status_code=401,
                detail="No user identity in request headers",
            )

        # Get the caller's agent
        agent = HUB_STORE.get(principal, agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        return agent.model_dump()

    @router.delete("/_apx/hub/agents/{agent_id}", response_model=Any)
    async def hub_deregister_agent(request: Request, agent_id: str) -> Any:
        """Deregister an agent (caller-scoped, 404 if not the caller's)."""
        from apx_agent._hub_store import HUB_STORE
        from apx_agent._obo import extract_obo_headers

        # Resolve caller principal
        obo = extract_obo_headers(custom_inputs={}, headers=request.headers)
        principal = obo.get("user_id") or obo.get("user_email")
        if not principal:
            raise HTTPException(
                status_code=401,
                detail="No user identity in request headers",
            )

        # Delete from the caller's store
        if not HUB_STORE.delete(principal, agent_id):
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        return {"deleted": agent_id}

    @router.post("/_apx/hub/agents/{agent_id}/refresh", response_model=Any)
    async def hub_refresh_agent(request: Request, agent_id: str) -> Any:
        """Refresh an agent's card by re-crawling its /.well-known/agent.json.

        Updates the card's status (live/unreachable) and last_seen timestamp.
        Caller-scoped — 404 if the agent is not in the caller's store.
        """
        from datetime import datetime

        from apx_agent._hub_models import AgentTool
        from apx_agent._hub_store import HUB_STORE
        from apx_agent._obo import extract_obo_headers

        # Resolve caller principal
        obo = extract_obo_headers(custom_inputs={}, headers=request.headers)
        principal = obo.get("user_id") or obo.get("user_email")
        if not principal:
            raise HTTPException(
                status_code=401,
                detail="No user identity in request headers",
            )

        # Get the caller's card from the store
        card = HUB_STORE.get(principal, agent_id)
        if not card:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

        # Re-crawl the agent's URL
        a2a = await _crawl_agent(card.url)

        if not a2a:
            # Mark as unreachable, clear last_seen
            card.status = "unreachable"
            card.last_seen = None
        else:
            # Keep the live card's prompt, tools, and trace header.
            card.instructions = str(a2a.get("instructions", ""))[:2000]
            card.trace_header = str(a2a.get("trace_header", ""))[:200]
            declared = a2a.get("tools") or []
            if declared:
                card.tools = [
                    AgentTool(
                        name=t.get("name", ""),
                        description=str(t.get("description", ""))[:200],
                    )
                    for t in declared
                    if isinstance(t, dict) and t.get("name")
                ]
            elif a2a.get("skills"):
                card.tools = [
                    AgentTool(name=s["name"], description=s.get("description", "")[:200])
                    for s in a2a.get("skills", [])
                ]
            # Update card with fresh data
            from apx_agent._hub_models import AgentTool

            card.status = "live"
            card.last_seen = datetime.utcnow()
            card.name = a2a.get("name", card.name)
            card.description = a2a.get("description", card.description)
            card.tools = [
                AgentTool(name=s["name"], description=s.get("description", "")[:200])
                for s in a2a.get("skills", [])
            ]
            if "mcpEndpoint" in a2a:
                card.mcp_endpoint = a2a.get("mcpEndpoint")

        # Store the updated card
        HUB_STORE.put(principal, card)

        return card.model_dump()

    @router.post("/_apx/hub/agents/{agent_id}/invoke", response_model=Any)
    async def hub_invoke_agent(
        request: Request,
        agent_id: str,
    ) -> Any:
        """Invoke a registered agent under the caller's OBO token.

        Forwards the request to the agent's URL using the caller's identity.
        Caller-scoped — 404 if the agent is not in the caller's store.
        Fails closed (401) on a deployed App with no OBO token.
        """
        from httpx import AsyncClient

        from apx_agent._defaults import _ws_prefer_obo
        from apx_agent._hub_models import InvokeRequest
        from apx_agent._hub_store import HUB_STORE
        from apx_agent._obo import extract_obo_headers, remote_hop_headers

        # Resolve identity via _ws_prefer_obo (fails closed → 401 on deployed App without OBO)
        ws = _ws_prefer_obo(request)

        # Resolve caller principal to look up the card in HUB_STORE
        obo = extract_obo_headers(custom_inputs={}, headers=request.headers)
        principal = obo.get("user_id") or obo.get("user_email")
        if not principal:
            raise HTTPException(
                status_code=401,
                detail="No user identity in request headers",
            )

        # Get the caller's card from the store (404 if absent)
        card = HUB_STORE.get(principal, agent_id)
        if not card:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

        # Parse the invoke request
        body = await request.json()
        invoke_req = InvokeRequest(**body)

        # Forward the invocation to the agent's URL using the caller's OBO token.
        # Reuse the forwarding pattern from _agent_tool.py: extract OBO headers
        # and forward them to the downstream agent.
        token = (request.headers.get("X-Forwarded-Access-Token") or "").strip()
        forwarded = remote_hop_headers(token, request.headers.get("X-Forwarded-Host"))

        # Same transport as a declared sub-agent: reads the peer's card and posts
        # the durable contract to a DurableAgentServer, else /invocations (#980).
        from apx_agent._remote import RemoteDatabricksAgent

        peer = RemoteDatabricksAgent(card.url, headers=forwarded)
        try:
            await peer.init()
            return await peer._post_via_http([{"role": "user", "content": invoke_req.input}], forwarded)
        except Exception as exc:
            logger.error("Failed to invoke agent %s at %s: %s", agent_id, card.url, exc)
            raise HTTPException(
                status_code=502,
                detail=f"Failed to invoke agent: {exc}",
            ) from exc

    @router.get("/_apx/hub/fleet", response_model=Any)
    async def hub_fleet_view(
        request: Request,
    ) -> Any:
        """List all agents from the UC registry, read as the App principal.

        Requires a signed-in caller; fails closed (401) on a deployed App
        without OBO. The query itself runs as the App SP: deploy grants the
        registry to ``account users`` and every App SP, so this shows nothing
        a caller couldn't read, and the caller's token needs no ``sql`` scope.
        """
        from apx_agent._defaults import _ws_prefer_obo
        from apx_agent._publish import list_registry_agents

        _ws_prefer_obo(request)  # signed-in gate: 401 on a deployed App without OBO
        ws = request.app.state.workspace_client

        # Off the loop: a cold warehouse can take minutes, and blocking here
        # stalls every other request the App is serving.
        import asyncio as _asyncio
        from fastapi.responses import JSONResponse

        from apx_agent._registry import REGISTRY_TABLE_ENV, resolve_registry_tables

        configured = os.environ.get(REGISTRY_TABLE_ENV)

        def _read() -> list[dict[str, Any]]:
            table = configured or resolve_registry_tables(ws).agents
            return list_registry_agents(registry_table=table, ws=ws)

        try:
            return await _asyncio.to_thread(_read)
        except (RuntimeError, ValueError) as e:
            logger.exception("listing the agent registry failed")
            return JSONResponse({"error": str(e)}, status_code=503)
