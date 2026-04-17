"""Agent Hub — FastAPI app with registry, discovery, and chat proxy."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles

from .chat import proxy_chat
from .discovery import discover_apps, discover_genie_spaces, discover_serving_endpoints
from .models import ChatRequest, ChatResponse, HubAgent, HubSkill, RegisterRequest, RegisterResponse
from .registry import AgentRegistry

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


def _get_workspace_client():
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient()


def create_hub_app() -> FastAPI:
    """Create the Agent Hub FastAPI application."""
    registry = AgentRegistry()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.state.hub_registry = registry
        app.state.obo_token = None  # Populated from first authenticated request
        ws = _get_workspace_client()
        app.state.workspace_client = ws

        # Initial workspace discovery (serving endpoints + Genie spaces)
        _refresh_registry(registry, ws)

        # Start background refresh task
        task = asyncio.create_task(_background_refresh(registry, app))
        yield
        task.cancel()

    app = FastAPI(title="Agent Hub", lifespan=lifespan)
    # Expose registry on state immediately so tests and direct access work
    # even outside of lifespan context (lifespan will also set it, but this
    # ensures it's available before the lifespan runs).
    app.state.hub_registry = registry

    # Capture OBO token from any authenticated request for use in app discovery.
    # Databricks Apps inject X-Forwarded-Access-Token on every request —
    # this user token (not the SP token) is required for cross-app HTTP calls.
    @app.middleware("http")
    async def capture_obo_token(request: Request, call_next: Any) -> Any:
        token = request.headers.get("X-Forwarded-Access-Token", "")
        if token:
            request.app.state.obo_token = token
        return await call_next(request)

    # --- API routes ---

    @app.post("/api/agents/register")
    async def register_agent(body: RegisterRequest, request: Request) -> RegisterResponse:
        url = body.url.rstrip("/")
        card_url = f"{url}/.well-known/agent.json"

        fwd_headers = {}
        if auth := request.headers.get("Authorization"):
            fwd_headers["Authorization"] = auth
        if token := request.headers.get("X-Forwarded-Access-Token"):
            fwd_headers["X-Forwarded-Access-Token"] = token

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(card_url, headers=fwd_headers)
                resp.raise_for_status()
                _json_result = resp.json()
                card = await _json_result if asyncio.iscoroutine(_json_result) else _json_result
        except Exception as e:
            logger.warning("Failed to fetch agent card from %s: %s", card_url, e)
            card = {"name": url.split("/")[-1], "description": url}

        skills = [
            HubSkill(name=s.get("name", ""), description=s.get("description", ""))
            for s in card.get("skills", [])
        ]
        agent = HubAgent(
            name=card.get("name", url),
            description=card.get("description", ""),
            source="apx",
            url=url,
            skills=skills,
            status="online",
        )
        registry.add(agent)
        logger.info("Registered agent: %s (%s)", agent.name, agent.id)
        return RegisterResponse(id=agent.id)

    @app.get("/api/agents")
    async def list_agents(
        source: str | None = Query(None),
        q: str | None = Query(None),
    ) -> list[dict[str, Any]]:
        agents = registry.list(source=source, query=q)
        return [a.model_dump() for a in agents]

    @app.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: str) -> dict[str, Any]:
        agent = registry.get(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        return agent.model_dump()

    @app.post("/api/discover")
    async def trigger_discovery(request: Request) -> dict[str, int]:
        obo_token = request.headers.get("X-Forwarded-Access-Token", "")
        if obo_token:
            request.app.state.obo_token = obo_token
        ws = request.app.state.workspace_client
        found = await discover_apps(ws, obo_token=obo_token or None)
        for agent in found:
            registry.add(agent)
        return {"discovered": len(found)}

    @app.delete("/api/agents/{agent_id}", status_code=204)
    async def deregister_agent(agent_id: str) -> None:
        if registry.get(agent_id) is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        registry.remove(agent_id)

    @app.post("/api/chat")
    async def chat(body: ChatRequest, request: Request) -> ChatResponse:
        agent = registry.get(body.agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")

        fwd_headers = {}
        if auth := request.headers.get("Authorization"):
            fwd_headers["Authorization"] = auth
        if token := request.headers.get("X-Forwarded-Access-Token"):
            fwd_headers["X-Forwarded-Access-Token"] = token

        ws = getattr(request.app.state, "workspace_client", None)
        return await proxy_chat(body, agent, headers=fwd_headers, ws=ws)

    # --- Static frontend ---

    if FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

    return app


def _refresh_registry(registry: AgentRegistry, ws: Any) -> None:
    """Synchronously refresh workspace-discovered agents."""
    for agent in discover_serving_endpoints(ws):
        registry.add(agent)
    for agent in discover_genie_spaces(ws):
        registry.add(agent)


async def _background_refresh(registry: AgentRegistry, app: Any) -> None:
    """Periodically refresh workspace agents and health-check apx agents."""
    health_counter = 0
    while True:
        await asyncio.sleep(60)
        health_counter += 1

        ws = app.state.workspace_client
        obo_token = getattr(app.state, "obo_token", None)

        apx_agents = registry.list(source="apx")
        for agent in apx_agents:
            if agent.url:
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.get(f"{agent.url.rstrip('/')}/health")
                        if resp.status_code == 200:
                            registry.update_status(agent.id, "online")
                        else:
                            registry.update_status(agent.id, "offline")
                except Exception:
                    registry.update_status(agent.id, "offline")

        if health_counter % 5 == 0:
            _refresh_registry(registry, ws)

        if obo_token:
            for agent in await discover_apps(ws, obo_token=obo_token):
                registry.add(agent)
