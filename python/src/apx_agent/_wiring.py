"""FastAPI integration — ``setup_agent()`` and ``create_app()``.

Mounts the supported Mosaic AI surface on the agent:

  * ``POST /invocations`` — MLflow ChatAgent protocol (used by Model Serving,
    AI Playground, Review App, Agent Evaluation). Bridges Databricks Apps'
    ``X-Forwarded-Access-Token`` header into ``custom_inputs["user_token"]``
    so user-scoped OBO auth flows through.
  * ``GET /.well-known/agent.json`` — A2A discovery card.
  * ``GET /health`` — liveness probe.
  * ``GET|POST|DELETE /mcp`` — stateless MCP HTTP transport for Genie Code
    and AI Playground.
  * ``{api_prefix}/tools/<name>`` — per-tool FastAPI routes for direct invocation.

The legacy ``/responses`` endpoint, the custom apx-agent trace system, and
the apx-agent-specific request/response types were deleted when the framework
moved to the supported runtime (LangGraph + MLflow ChatAgent). ``BaseAgent``
subclasses' ``.run()`` / ``.stream()`` methods now compile to LangGraph; the
``/invocations`` route is the protocol surface.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from starlette.responses import Response

from ._agents import BaseAgent
from ._defaults import _make_workspace_client
from ._inspection import _load_agent_config
from ._mcp import _build_mcp_components
from ._models import (
    A2ASkill,
    AgentCard,
    AgentConfig,
    AgentContext,
)

logger = logging.getLogger(__name__)


def _resolve_env_var(value: str) -> str:
    """Resolve a ``$VAR`` or ``${VAR}`` reference to its environment value.

    Returns the original string unchanged if it doesn't start with ``$``
    or the variable is not set.
    """
    if not value.startswith("$"):
        return value
    var_name = value.lstrip("$").strip("{}")
    return os.environ.get(var_name, "")


async def setup_agent(
    app: FastAPI,
    agent: BaseAgent,
    config: AgentConfig | None = None,
    pyproject_path: str | None = None,
) -> AgentContext | None:
    """Wire agent protocol routes onto an existing FastAPI app.

    Mounts:
      * tool routes at ``{api_prefix}/tools/<name>``
      * ``GET /.well-known/agent.json`` (A2A discovery)
      * ``GET /health``
      * MCP transports at ``/mcp`` and ``/mcp/sse`` (when ``mcp`` extra installed)
      * Dev UI at ``/_apx/*`` (when ``_dev`` module loadable)

    The ``POST /invocations`` route is mounted separately by ``create_app``
    after ``setup_agent`` runs (it depends on the optional ``langgraph`` extra).
    Returns the ``AgentContext``, or ``None`` if config is missing.
    """
    if config is None:
        config = _load_agent_config(pyproject_path=pyproject_path)
    if config is None:
        logger.info("No agent config found — agent protocol disabled")
        app.state.agent_context = None
        return None

    # Merge sub_agents from config
    if config.sub_agents:
        sub_agent_urls: list[str] = getattr(agent, "_sub_agent_urls", [])
        existing = set(sub_agent_urls)
        for raw_url in config.sub_agents:
            resolved = _resolve_env_var(raw_url)
            if not resolved:
                logger.warning(
                    f"sub_agents config: {raw_url} resolved to empty — skipping"
                )
                continue
            if resolved not in existing:
                sub_agent_urls.append(resolved)
                existing.add(resolved)

    tools = agent.collect_tools()
    tools += await agent.fetch_remote_tools()
    card = AgentCard(
        name=config.name,
        description=config.description,
        skills=[
            A2ASkill(
                id=t.name,
                name=t.name,
                description=t.description,
                inputSchema=t.input_schema,
                outputSchema=t.output_schema,
            )
            for t in tools
        ],
    )
    ctx = AgentContext(config=config, tools=tools, card=card, agent=agent)
    app.state.agent_context = ctx

    logger.info(f"Agent protocol enabled: {config.name} ({len(tools)} tools)")

    # Per-tool FastAPI routes (live under {api_prefix}/tools/<name>)
    for router in agent.get_tool_routers():
        app.include_router(router, prefix=config.api_prefix)

    _mount_protocol_routes(app)

    # Dev UI (optional). Skipped silently if unavailable or broken.
    try:
        from ._dev import build_dev_ui_router

        app.include_router(build_dev_ui_router(config.api_prefix))
        logger.info("Dev UI mounted at /_apx/*")
    except Exception as e:
        logger.info(f"Dev UI not available: {e}")

    # Auto-register with agent registry (if configured)
    if config.registry:
        public_url = _resolve_env_var(config.url) if config.url else ""
        registry_url = _resolve_env_var(config.registry)
        if registry_url:
            _schedule_registration(app, registry_url, public_url)
        else:
            logger.warning(
                "registry env var resolved to empty — skipping registration"
            )

    return ctx


def _schedule_registration(
    app: FastAPI, registry_url: str, public_url: str
) -> None:
    """Fire-and-forget POST to the agent registry after the server is up.

    Registry crawls ``{public_url}/.well-known/agent.json`` to populate the card.
    """
    import asyncio

    import httpx
    from starlette.middleware.base import BaseHTTPMiddleware

    async def _register() -> None:
        await asyncio.sleep(2)

        url = registry_url.rstrip("/")
        payload: dict[str, Any] = {}
        if public_url:
            payload["url"] = public_url.rstrip("/")
        else:
            logger.warning(
                "No public URL configured (set url in [tool.apx.agent]) — "
                "registry may not be able to crawl this agent"
            )

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{url}/api/agents/register", json=payload
                )
                r.raise_for_status()
                data = r.json()
                logger.info(
                    "Registered with agent registry at %s as '%s'",
                    url,
                    data.get("id", "unknown"),
                )
        except Exception as e:
            logger.warning(
                "Failed to register with agent registry at %s: %s", url, e
            )

    class _RegisterOnceMiddleware(BaseHTTPMiddleware):
        _registered = False

        async def dispatch(self, request: Any, call_next: Any) -> Any:
            if not _RegisterOnceMiddleware._registered:
                _RegisterOnceMiddleware._registered = True
                asyncio.create_task(_register())
            return await call_next(request)

    app.add_middleware(_RegisterOnceMiddleware)


def _mount_protocol_routes(app: FastAPI) -> None:
    """Mount discovery + health + MCP routes."""
    protocol_router = APIRouter()

    @protocol_router.get("/.well-known/agent.json", include_in_schema=False)
    async def agent_card(request: Request) -> AgentCard:
        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            raise HTTPException(
                status_code=404, detail="Agent protocol not configured"
            )
        base = str(request.base_url).rstrip("/")
        mcp_available = (
            getattr(request.app.state, "mcp_server", None) is not None
        )
        return ctx.card.model_copy(
            update={
                "url": base,
                "mcpEndpoint": f"{base}/mcp" if mcp_available else None,
            }
        )

    @protocol_router.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    class _RawResponse(Response):
        """Sentinel for handlers that write directly to the ASGI socket."""

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            pass

    @protocol_router.get("/mcp/sse", include_in_schema=False)
    async def mcp_sse(request: Request) -> Response:
        """MCP SSE transport — connect MCP clients here."""
        mcp_server = getattr(request.app.state, "mcp_server", None)
        mcp_transport = getattr(request.app.state, "mcp_transport", None)
        if mcp_server is None or mcp_transport is None:
            raise HTTPException(
                status_code=503, detail="MCP server not available"
            )
        from ._mcp import set_mcp_auth

        set_mcp_auth(
            request.headers.get("Authorization", ""),
            request.headers.get("X-Forwarded-Access-Token", ""),
        )
        async with mcp_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await mcp_server.run(
                streams[0],
                streams[1],
                mcp_server.create_initialization_options(),
            )
        return _RawResponse()

    @protocol_router.post("/mcp/messages/", include_in_schema=False)
    async def mcp_messages(request: Request) -> Response:
        mcp_transport = getattr(request.app.state, "mcp_transport", None)
        if mcp_transport is None:
            raise HTTPException(
                status_code=503, detail="MCP server not available"
            )
        await mcp_transport.handle_post_message(
            request.scope, request.receive, request._send
        )
        return _RawResponse()

    async def _mcp_http(request: Request) -> Response:
        """Stateless MCP HTTP transport — used by Genie Code and AI Playground."""
        mcp_http_manager = getattr(request.app.state, "mcp_http_manager", None)
        if mcp_http_manager is None:
            raise HTTPException(
                status_code=503, detail="MCP server not available"
            )
        from ._mcp import set_mcp_auth

        set_mcp_auth(
            request.headers.get("Authorization", ""),
            request.headers.get("X-Forwarded-Access-Token", ""),
        )
        scope = dict(request.scope)
        headers = list(scope.get("headers", []))
        accept_vals = [v for k, v in headers if k.lower() == b"accept"]
        if not any(b"text/event-stream" in v for v in accept_vals):
            headers = [(k, v) for k, v in headers if k.lower() != b"accept"]
            existing = b", ".join(accept_vals)
            new_accept = b"text/event-stream" + (
                b", " + existing if existing else b""
            )
            headers.append((b"accept", new_accept))
            scope["headers"] = headers
        await mcp_http_manager.handle_request(
            scope, request.receive, request._send
        )
        return _RawResponse()

    protocol_router.add_api_route(
        "/mcp",
        endpoint=_mcp_http,
        methods=["GET", "POST", "DELETE"],
        include_in_schema=False,
    )

    app.include_router(protocol_router)


async def _setup_mcp(app: FastAPI, ctx: AgentContext) -> Any:
    """Initialize MCP server + transports. Returns lifecycle context manager."""
    from contextlib import nullcontext

    try:
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        mcp_server, mcp_transport = _build_mcp_components(
            ctx, app, ctx.config.api_prefix
        )
        app.state.mcp_server = mcp_server
        app.state.mcp_transport = mcp_transport
        mcp_http_manager = StreamableHTTPSessionManager(mcp_server, stateless=True)
        app.state.mcp_http_manager = mcp_http_manager
        logger.info(
            "MCP server enabled at /mcp/sse (SSE) and /mcp (stateless HTTP)"
        )
        return mcp_http_manager.run()
    except ImportError:
        app.state.mcp_server = None
        app.state.mcp_transport = None
        app.state.mcp_http_manager = None
        logger.warning(
            "mcp package not installed — /mcp endpoints disabled. "
            "pip install apx-agent[mcp]"
        )
        return nullcontext()


def create_app(
    agent: BaseAgent,
    config: AgentConfig | None = None,
    pyproject_path: str | None = None,
) -> FastAPI:
    """Create a complete FastAPI app: ``/invocations`` + discovery + MCP + dev UI.

    ``pyproject_path`` can be an explicit path to ``pyproject.toml``. When
    omitted, the config is discovered from the entry-point module's location
    or the current working directory.

    Example::

        from apx_agent import LlmAgent, Dependencies, create_app

        def get_billing(customer_id: str, ws: Dependencies.Workspace) -> dict:
            \"\"\"Get billing history.\"\"\"
            ...

        agent = LlmAgent(tools=[get_billing])
        app = create_app(agent)
        # uvicorn my_app:app --reload
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # Opt-in MLflow auto-tracing (off by default — autolog adds ~30s
        # overhead per run; selective spans in the compile path are always on).
        try:
            from ._mlflow_tracing import autolog_if_env

            autolog_if_env()
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("MLflow autolog setup skipped: %s", exc)

        app.state.workspace_client = _make_workspace_client()

        ctx = await setup_agent(
            app, agent, config, pyproject_path=pyproject_path
        )

        # Mount the supported /invocations route (MLflow ChatAgent protocol).
        # Best-effort — missing optional deps log a warning and skip.
        if ctx is not None:
            try:
                from ._invocations import mount_invocations_route

                mount_invocations_route(app, agent, ctx.config)
            except Exception as exc:
                logger.warning("Skipping /invocations mount: %s", exc)

        if ctx is not None:
            mcp_lifecycle = await _setup_mcp(app, ctx)
        else:
            from contextlib import nullcontext

            mcp_lifecycle = nullcontext()

        async with mcp_lifecycle:
            try:
                yield
            finally:
                logger.info("Shutting down agent runtime")
                ws = getattr(app.state, "workspace_client", None)
                if ws and hasattr(ws, "close"):
                    try:
                        ws.close()
                    except Exception:
                        pass

    return FastAPI(lifespan=lifespan)
