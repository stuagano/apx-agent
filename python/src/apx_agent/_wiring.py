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


def apply_config_knobs(agent: BaseAgent, config: AgentConfig) -> None:
    """Apply ``[tool.apx.agent]`` config values onto the live agent instance.

    This is the **shared config→instance seam** that both serve paths must run:

      * ``apx run`` / Apps target — via ``setup_agent`` (this module).
      * model-serving deploy — via ``apx deploy`` calling this right before
        ``log_agent``, because MLflow captures the agent *at log time*; nothing
        re-applies config inside the logged model's per-request compile.

    Keeping both paths on one helper is what prevents cross-target drift (a
    knob that works under ``apx run`` but silently no-ops on a deploy). Future
    declarative features that likewise need to land on the instance before it
    is captured — tools merge, memory attach, guard attach — should extend this
    same function rather than re-implementing the merge at one call site.

    Semantics for the generation knobs: the compile path (``_compile.py``) reads
    ``temperature`` / ``max_tokens`` / ``max_iterations`` off the instance, not
    off config. Constructor wins — only copy when the instance left the attr at
    ``None``. Uses ``is None`` (not a truthy check) so a deliberate
    ``temperature=0.0`` / ``max_iterations=0`` isn't clobbered, and ``hasattr``
    guards composition agents (e.g. ``SequentialAgent``) that don't define
    every knob. Idempotent: a second call sees a non-``None`` attr and no-ops.
    """
    for attr, config_value in (
        ("_temperature", config.temperature),
        ("_max_tokens", config.max_tokens),
        ("_max_iterations", config.max_iterations),
    ):
        if (
            config_value is not None
            and hasattr(agent, attr)
            and getattr(agent, attr) is None
        ):
            setattr(agent, attr, config_value)


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
        if not hasattr(agent, "_sub_agent_urls"):
            # Only LlmAgent defines _sub_agent_urls; on a composition root
            # getattr(..., []) would return a throwaway list, silently dropping
            # config-declared sub_agents from the A2A/MCP discovery surface
            # (audit M7). Warn loudly instead of failing silently.
            logger.warning(
                "config sub_agents %s set on a %s root, which does not support "
                "sub-agent merging (only LlmAgent does) — these are ignored. "
                "Declare sub_agents on a leaf LlmAgent instead.",
                config.sub_agents,
                type(agent).__name__,
            )
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

    # Merge config-declared knobs from [tool.apx.agent] onto the agent instance.
    apply_config_knobs(agent, config)

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


class _ResponsesStringInputAdapter:
    """ASGI middleware that rewrites ``input: "<text>"`` on ``POST /responses``
    to the list shape the underlying ``ResponsesAgentRequest`` expects.

    The OpenAI Responses API allows both ``input: string`` and ``input: list``;
    the upstream pydantic schema (in ``mlflow.genai.agent_server``) only
    accepts the list form, so a curl-first user trying the simplest payload
    hits a 422 with "Input should be a valid list" before their request ever
    reaches the agent. This bridges the string form by wrapping it as
    ``[{"role": "user", "content": "<text>"}]`` — the canonical "user said
    this" envelope.

    Implemented as pure ASGI (not FastAPI's ``@app.middleware("http")``)
    because BaseHTTPMiddleware buffers responses in a way that breaks
    streaming AND has a known interaction with body modification.

    Only ``POST /responses`` is touched. Malformed JSON is forwarded
    unchanged so the upstream handler returns its native 400/422 error.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if not (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/responses"
        ):
            await self.app(scope, receive, send)
            return

        import json as _json

        # Buffer the full body (responses agents send a single small payload).
        body = b""
        more = True
        while more:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            if event["type"] == "http.request":
                body += event.get("body", b"") or b""
                more = event.get("more_body", False)
            else:
                more = False

        # Rewrite string ``input`` → user-message list. Best-effort: anything
        # we don't understand passes through so the real handler can 422 it.
        if body:
            try:
                payload = _json.loads(body)
                if isinstance(payload, dict) and isinstance(payload.get("input"), str):
                    payload["input"] = [
                        {"role": "user", "content": payload["input"]}
                    ]
                    body = _json.dumps(payload).encode()
            except Exception:
                pass

        sent = False

        async def _replay() -> dict[str, Any]:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, _replay, send)


def _install_responses_input_adapter(app: FastAPI) -> None:
    """Install the ``/responses`` string-input adapter as ASGI middleware."""
    app.add_middleware(_ResponsesStringInputAdapter)


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
    session_store: Any | None = None,
) -> FastAPI:
    """Create a complete FastAPI app: ``/invocations`` + discovery + MCP + dev UI.

    ``pyproject_path`` can be an explicit path to ``pyproject.toml``. When
    omitted, the config is discovered from the entry-point module's location
    or the current working directory.

    ``session_store`` is an optional ``SessionStore`` (e.g. ``DeltaSessionStore``)
    for multi-turn memory. When provided, conversation history is persisted
    across requests keyed by ``conversation_id``.

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
        # MLflow auto-tracing. ``apx run`` sets ``APX_AGENT_MLFLOW_AUTOLOG=1``
        # before importing the user's module so the dev loop gets per-tool +
        # per-LLM spans by default. Deploy paths reach this lifespan with the
        # env unset, so autolog stays off there (selective spans in the
        # compile path are always on either way).
        try:
            from ._mlflow_tracing import autolog_if_env

            autolog_if_env()
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("MLflow autolog setup skipped: %s", exc)

        app.state.workspace_client = _make_workspace_client()
        app.state.session_store = session_store

        ctx = await setup_agent(
            app, agent, config, pyproject_path=pyproject_path
        )

        # Mount the supported /invocations route (MLflow ChatAgent protocol).
        # Best-effort — missing optional deps log a warning and skip.
        if ctx is not None:
            try:
                from ._invocations import mount_invocations_route

                mount_invocations_route(app, agent, ctx.config, session_store=session_store)
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


# ---------------------------------------------------------------------------
# Mounting helpers — for embedding apx-agent's protocol surface (MCP + A2A
# discovery + health) onto an existing FastAPI app (e.g. one produced by
# ``mlflow.genai.agent_server.AgentServer`` in the Databricks Apps target).
# ---------------------------------------------------------------------------


def mount_mcp_endpoints(
    app: FastAPI,
    agent: BaseAgent,
    config: AgentConfig | None = None,
    pyproject_path: str | None = None,
) -> None:
    """Mount apx-agent's ``/mcp`` + ``/.well-known/agent.json`` + ``/health``
    on an existing FastAPI app.

    Designed for the Databricks Apps target: pair the
    ``mlflow.genai.agent_server.AgentServer`` (which provides ``/invocations``
    + ``/responses``) with apx-agent's MCP surface so Genie / Genie Code can
    consume the same agent as an MCP source.

    Mounted routes are inert until the FastAPI lifespan completes startup
    (they ``503`` if ``app.state.mcp_server`` isn't populated yet). The
    lifespan registers an ``async def startup_event_handler`` that runs
    ``setup_agent`` + ``_setup_mcp`` and stores the resulting state on
    ``app.state``. Shutdown closes the MCP HTTP manager cleanly.

    Usage::

        from mlflow.genai.agent_server import AgentServer
        from apx_agent import mount_mcp_endpoints

        server = AgentServer(agent_type="ResponsesAgent")
        from agent_server import agent  # your apx-agent BaseAgent

        mount_mcp_endpoints(server.app, agent.agent)
        app = server.app

    The mount is best-effort: when the ``mcp`` extra is missing, the routes
    are still registered but return 503 — same behavior as ``create_app``.

    Requires installing ``apx-agent[mcp]`` for full functionality.
    """
    # Mount the route shells immediately. They read from app.state — which
    # gets populated in the lifespan startup event below.
    _mount_protocol_routes(app)

    # Install the /responses string-input adapter so a curl-first user trying
    # the simplest payload (`input: "<text>"`) doesn't hit a pydantic 400 on
    # the upstream ResponsesAgentRequest schema.
    _install_responses_input_adapter(app)

    # Track the in-flight MCP lifecycle so shutdown can close it cleanly.
    _state_key = "_apx_mount_state"

    @app.on_event("startup")
    async def _apx_mount_startup() -> None:  # type: ignore[misc]
        # MLflow auto-tracing. This is the apps-target path (the AgentServer app
        # is not created via create_app, so create_app's lifespan never runs
        # here). Under ``apx run --reload`` the worker subprocess re-imports the
        # module and re-runs this startup, but never re-runs cli run()'s body —
        # so autolog must be (re)applied in-process here or per-tool/per-LLM
        # spans stop emitting under --reload (audit M5).
        try:
            from ._mlflow_tracing import autolog_if_env

            autolog_if_env()
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("MLflow autolog setup skipped: %s", exc)

        ctx = await setup_agent(app, agent, config, pyproject_path=pyproject_path)
        if ctx is None:
            logger.info("mount_mcp_endpoints: no agent config — /mcp will 503")
            return
        try:
            mcp_lifecycle = await _setup_mcp(app, ctx)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "mount_mcp_endpoints: MCP setup failed (%s) — /mcp will 503",
                exc,
            )
            return
        cm = mcp_lifecycle.__aenter__()
        try:
            await cm
        except Exception as exc:  # pragma: no cover
            logger.warning("mount_mcp_endpoints: failed to enter MCP lifecycle: %s", exc)
            setattr(app.state, _state_key, None)
            return
        setattr(app.state, _state_key, mcp_lifecycle)
        logger.info("mount_mcp_endpoints: /mcp ready (HTTP + SSE)")

    @app.on_event("shutdown")
    async def _apx_mount_shutdown() -> None:  # type: ignore[misc]
        lifecycle = getattr(app.state, _state_key, None)
        if lifecycle is None:
            return
        try:
            await lifecycle.__aexit__(None, None, None)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("mount_mcp_endpoints: clean shutdown failed: %s", exc)
