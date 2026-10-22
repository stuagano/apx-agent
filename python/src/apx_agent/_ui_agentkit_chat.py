"""AgentKit chat app on a DurableAgentServer host.

Ported from ``databricks-agentbricks==0.5.0``
``templates/ui/agent-langgraph/runtime/ui.py:install_ui``. The vendored browser
assets live in ``_static/agentkit_chat`` (see its VENDORED.md). The template's
paths move under ``/_apx``: assets go to ``/_apx/assets/agentkit``, and the
config and demo APIs go to ``/_apx/api/{ui,demo}``. The page itself is served
at ``/_apx/chat``, and at ``/`` unless the project mounts its own root UI. Chat turns use DurableAgentServer's own
``/api/invocations``.

Differences from the template:

- the default model is the model the server was compiled with, not
  ``agent.agent.MODEL``;
- transcript reload returns an empty transcript (#978).

Auth is the template's (accepted design risk D3): the panels call the
managed stores with the App service principal, and the actor comes from the
forwarded identity headers or an ``?actor=`` override.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from databricks.sdk.errors.base import DatabricksError
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

_UI_ROOT = Path(__file__).resolve().parent / "_static" / "agentkit_chat"
_ASSETS_PREFIX = "/_apx/assets/agentkit"
_PAGE_PATHS = ("/", "/_apx/chat")
_INSTANCE_ID = uuid.uuid4().hex[:12]  # identifies this process in the UI
_AGENTS_API = "/api/2.0/agents"
_USER_HEADERS = ("x-forwarded-email", "x-forwarded-user")
# Tracing turns on only with both a destination and an experiment, mirroring
# the runtime tracing setup so the UI card matches what the agent actually does.
_TRACING_DESTINATION_VARS = ("MLFLOW_TRACKING_URI", "MLFLOW_TRACING_DESTINATION")


class MemoryEntryRequest(BaseModel):
    path: str = Field(min_length=1, pattern=r"^/")
    content: str = Field(min_length=1)
    description: str | None = None


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=100)
    actor: str | None = None


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value is not None and value.strip() else None


def _memory_store() -> str | None:
    # Same resolution the agent uses (AGENT_MEMORY_STORE env → agent.toml binding).
    from databricks_agentkit.runtime.tool_manifest import resolve_memory_store

    store = resolve_memory_store()
    return store.strip().strip("/") if store and store.strip().strip("/") else None


def _session_store() -> str | None:
    from databricks_agentkit.runtime.tool_manifest import resolve_session_store

    store = resolve_session_store()
    return store.strip() if store and store.strip() else None


def _request_actor(request: Request) -> str:
    """The signed-in user from the forwarded identity headers; ``"agent"`` locally.

    Accepted risk (design D3): the panels run as the App SP with a caller-chosen
    actor (``?actor=``); see docs/design/a2a-trust-model.md.
    """
    for header in _USER_HEADERS:
        if value := request.headers.get(header):
            return value
    return "agent"


def _request_session_id(request: Request) -> str:
    """Read the chat session from the session_id query parameter that demoUrl() sends."""
    session_id = request.query_params.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    return session_id


def _is_deployed() -> bool:
    app_url = _env("DATABRICKS_APP_URL")
    is_local = app_url is not None and app_url.startswith(("http://localhost", "http://127.0.0.1"))
    return _env("DATABRICKS_APP_NAME") is not None and not is_local


def _workspace_host(ws: Any) -> str | None:
    """Workspace host for building MLflow links, from the host's service client."""
    host = ws.config.host if ws is not None else _env("DATABRICKS_HOST")
    return host.rstrip("/") if host else None


def _tracing(ws: Any) -> dict[str, Any]:
    """Whether MLflow tracing is configured, plus a best-effort link to its experiment."""
    destination = next((v for v in map(_env, _TRACING_DESTINATION_VARS) if v is not None), None)
    experiment_id = _env("MLFLOW_EXPERIMENT_ID")
    enabled = destination is not None and (experiment_id is not None or _env("MLFLOW_EXPERIMENT_NAME") is not None)
    url: str | None = None
    if enabled and destination is not None:
        if destination.startswith(("http://", "https://")) and "databricks" not in destination:
            # A local OSS MLflow server: link into its own UI.
            base = destination.rstrip("/")
            url = f"{base}/#/experiments/{experiment_id}" if experiment_id else base
        elif host := _workspace_host(ws):
            url = f"{host}/ml/experiments/{experiment_id}" if experiment_id else f"{host}/ml/experiments"
    return {"enabled": enabled, "experiment_id": experiment_id, "url": url}


def _rank_models(default: str, names: list[str]) -> list[str]:
    """Pin the default first, then every remaining gateway model, without duplicates."""
    return [default, *sorted({n for n in names if n != default})]


def _discover_chat_models(ws: Any, default: str) -> list[str]:
    """AI Gateway chat models for the picker; falls back to just the default."""
    from databricks_agentkit.runtime.model_services import list_ai_gateway_model_services

    try:
        names = list_ai_gateway_model_services(ws)
    except Exception:  # noqa: BLE001 - a broken listing must not break the picker
        names = []
    return _rank_models(default, list(names))


class _ManagedStateClient:
    def __init__(self, ws: Any) -> None:
        self._workspace = ws

    def _do(self, method: str, path: str, *, query: dict[str, Any] | None = None,
            body: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self._workspace.api_client.do(method, path, query=query, body=body)
        if not isinstance(result, dict):
            raise RuntimeError(f"Expected an object response from {path}, got {type(result).__name__}")
        return result

    def create_memory_entry(self, actor: str, request: MemoryEntryRequest, session_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {"actor_id": actor, "path": request.path, "content": request.content,
                                "session_id": session_id}
        if request.description:
            body["description"] = request.description
        return self._do("POST", f"{_AGENTS_API}/memory-stores/{_memory_store()}/entries", body=body)

    def list_memory_entries(self, actor: str, path_prefix: str | None) -> dict[str, Any]:
        query: dict[str, Any] = {"actor_id": actor, "page_size": 100}
        if path_prefix:
            query["path_prefix"] = path_prefix
        return self._do("GET", f"{_AGENTS_API}/memory-stores/{_memory_store()}/entries", query=query)

    def search_memory_entries(self, actor: str, request: MemorySearchRequest) -> dict[str, Any]:
        return self._do("POST", f"{_AGENTS_API}/memory-stores/{_memory_store()}/entries:search",
                        body={"actor_id": actor, "query": request.query, "limit": request.limit})

    def ensure_session(self, actor: str, session_id: str) -> dict[str, Any]:
        sessions = f"{_AGENTS_API}/session-stores/{_session_store()}/sessions"
        try:
            return self._do("POST", sessions, query={"session_id": session_id},
                            body={"actor_id": actor, "metadata": {"client": "agentbricks-demo-ui"}})
        except Exception as exc:
            code = str(exc.error_code).upper() if isinstance(exc, DatabricksError) else None
            if code not in {"ALREADY_EXISTS", "RESOURCE_ALREADY_EXISTS"} and "already exists" not in str(exc).lower():
                raise
            return self._do("GET", f"{sessions}/{session_id}")

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._do("GET", f"{_AGENTS_API}/session-stores/{_session_store()}/sessions/{session_id}")

    def list_sessions(self, actor: str) -> dict[str, Any]:
        return self._do("GET", f"{_AGENTS_API}/session-stores/{_session_store()}/sessions",
                        query={"filter": f"actor_id = {json.dumps(actor)}",
                               "order_by": "last_activity_time desc", "page_size": 50})


async def _managed_call(operation: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(operation, *args)
    except HTTPException:
        raise
    except Exception as exc:
        code = exc.error_code if isinstance(exc, DatabricksError) else None
        raise HTTPException(status_code=502, detail=f"{code}: {exc}" if code else str(exc)) from exc


def _require_memory() -> None:
    if _memory_store() is None:
        raise HTTPException(status_code=503,
                            detail="No memory store configured. Run `agentbricks memory bind <store>`.")


def _require_session() -> None:
    if _session_store() is None:
        raise HTTPException(status_code=503,
                            detail="No session store configured. Run `agentbricks sessions bind <store>`.")


def _chat_sessions(result: dict[str, Any]) -> list[dict[str, Any]]:
    sessions = []
    for session in result.get("sessions", []):
        if not isinstance(session, dict):
            continue
        metadata = session.get("metadata")
        if isinstance(metadata, dict) and metadata.get("public_session_id"):
            continue
        sessions.append(session)
    return sessions


def mount_agentkit_chat(app: FastAPI, *, model: str, ws: Any) -> None:
    """Mount the AgentKit chat page, its assets, and its config/demo APIs on ``app``.

    ``model`` is the model the host serves; the picker pins it as the default.
    ``ws`` is the host's service client (the App SP), reused for the panels so
    no route builds a client on the event loop.
    """
    state = _ManagedStateClient(ws)
    from databricks_agentkit.runtime.store import runtime_store_is_persistent_environment

    app.mount(_ASSETS_PREFIX, StaticFiles(directory=_UI_ROOT), name="apx-agentkit-assets")

    @app.middleware("http")
    async def disable_ui_caching(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        path = request.url.path
        if path in _PAGE_PATHS or path.startswith(f"{_ASSETS_PREFIX}/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    async def index() -> FileResponse:
        return FileResponse(_UI_ROOT / "index.html")

    app.add_api_route("/_apx/chat", index, methods=["GET"], include_in_schema=False)

    async def _add_root_page() -> None:
        # Appended at startup, so a project UI mounted at "/" after compile
        # (e.g. a client/dist StaticFiles catch-all) stays ahead and keeps "/".
        app.add_api_route("/", index, methods=["GET"], include_in_schema=False)

    from ._wiring import _chain_lifespan

    _chain_lifespan(app, _add_root_page)

    @app.get("/_apx/api/ui/config", include_in_schema=False)
    async def ui_config(request: Request) -> dict[str, Any]:
        actor = _request_actor(request)
        memory_store = _memory_store()
        session_store = _session_store()
        persistent = runtime_store_is_persistent_environment()
        mode = "Runtime Store" if persistent else "In-process Runtime Store"
        return {
            "session_id": _request_session_id(request),
            "instance_id": _INSTANCE_ID,
            "viewer": actor if actor != "agent" else "Local developer",
            "deployed": _is_deployed(),
            "models": {"default": model, "available": [model]},
            "streaming": {"enabled": True, "transport": "Server-sent events", "persistent": persistent, "mode": mode},
            "background": {"enabled": True, "persistent": persistent, "mode": mode},
            "session": {
                "durable": session_store is not None,
                "managed": session_store is not None,
                "history": True,
                "mode": "Managed Session Store" if session_store else "In-process checkpointer",
                "store": session_store,
                "actor": actor,
            },
            "memory": {
                "enabled": memory_store is not None,
                "store": f"memory-stores/{memory_store}" if memory_store else None,
                "actor": actor,
            },
            "tracing": _tracing(ws),
        }

    @app.get("/_apx/api/demo/models", include_in_schema=False)
    async def demo_models() -> dict[str, Any]:
        # Discovery can take seconds in a large workspace; kept off the config route.
        return {"default": model, "available": await asyncio.to_thread(_discover_chat_models, ws, model)}

    @app.post("/_apx/api/demo/memory/entries", include_in_schema=False)
    async def create_memory_entry(request: Request, payload: MemoryEntryRequest) -> dict[str, Any]:
        _require_memory()
        return await _managed_call(state.create_memory_entry, _request_actor(request), payload,
                                   _request_session_id(request))

    @app.get("/_apx/api/demo/memory/entries", include_in_schema=False)
    async def list_memory_entries(request: Request, path_prefix: str | None = Query(default=None),
                                  actor: str | None = Query(default=None)) -> dict[str, Any]:
        _require_memory()
        return await _managed_call(state.list_memory_entries, actor or _request_actor(request),
                                   path_prefix)

    @app.post("/_apx/api/demo/memory/search", include_in_schema=False)
    async def search_memory_entries(request: Request, payload: MemorySearchRequest) -> dict[str, Any]:
        _require_memory()
        return await _managed_call(state.search_memory_entries, payload.actor or _request_actor(request),
                                   payload)

    @app.post("/_apx/api/demo/sessions", include_in_schema=False)
    async def ensure_session(request: Request) -> dict[str, Any]:
        _require_session()
        return await _managed_call(state.ensure_session, _request_actor(request),
                                   _request_session_id(request))

    @app.get("/_apx/api/demo/sessions", include_in_schema=False)
    async def list_sessions(request: Request) -> dict[str, Any]:
        session_id = _request_session_id(request)
        actor = _request_actor(request)
        if _session_store() is None:
            return {
                "sessions": [{"session_id": session_id, "actor_id": actor,
                              "metadata": {"client": "agentbricks-demo-ui-local"}}],
                "current_session_id": session_id,
                "managed": False,
            }
        result = await _managed_call(state.list_sessions, actor)
        return {**result, "sessions": _chat_sessions(result), "current_session_id": session_id, "managed": True}

    @app.post("/_apx/api/demo/sessions/{session_id}/open", include_in_schema=False)
    async def open_session(request: Request, session_id: str) -> JSONResponse:
        _require_session()
        session = await _managed_call(state.get_session, session_id)
        if session.get("actor_id") != _request_actor(request):
            raise HTTPException(status_code=403, detail="Session belongs to another actor.")
        return JSONResponse({"session_id": session_id, "previous_session_id": _request_session_id(request),
                             "managed": True})

    @app.get("/_apx/api/demo/session/items", include_in_schema=False)
    async def list_session_items(request: Request) -> dict[str, Any]:
        # ponytail: apx persists LangGraph checkpoints, not message items; reading
        # them needs the compiled graph's checkpointer + thread key (#978).
        return {
            "session_id": _request_session_id(request),
            "session_items": [],
            "interrupts": [],
            "note": "Transcript reload is not wired for apx agents yet (#978); live turns render normally.",
        }
