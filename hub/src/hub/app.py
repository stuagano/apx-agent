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
from .discovery import discover_serving_endpoints, discover_genie_spaces
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
        app.state.workspace_client = _get_workspace_client()

        # Initial workspace discovery
        _refresh_registry(registry, app.state.workspace_client)

        # Start background refresh task
        task = asyncio.create_task(_background_refresh(registry, app.state.workspace_client))
        yield
        task.cancel()

    app = FastAPI(title="Agent Hub", lifespan=lifespan)
    # Expose registry on state immediately so tests and direct access work
    # even outside of lifespan context (lifespan will also set it, but this
    # ensures it's available before the lifespan runs).
    app.state.hub_registry = registry

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


async def _background_refresh(registry: AgentRegistry, ws: Any) -> None:
    """Periodically refresh workspace agents and health-check apx agents."""
    health_counter = 0
    while True:
        await asyncio.sleep(60)
        health_counter += 1

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
