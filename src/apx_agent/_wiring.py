"""FastAPI integration — setup_agent() and create_app() for standalone use."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, FastAPI, HTTPException, Request
from starlette.responses import Response

from ._agents import BaseAgent
from ._inspection import _load_agent_config
from ._llm_loop import _handle_invocation
from ._mcp import _build_mcp_components
from ._models import (
    A2ASkill,
    AgentCard,
    AgentConfig,
    AgentContext,
    AgentTool,
    InvocationRequest,
    Message,
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

    Call this during your FastAPI lifespan. It:
    1. Loads config from pyproject.toml if not provided
    2. Collects tools + fetches remote sub-agent tools
    3. Builds the A2A discovery card
    4. Mounts protocol routes: /invocations, /.well-known/agent.json, /health
    5. Mounts tool routes under {api_prefix}/tools/<name>

    Returns the AgentContext, or None if config is missing.
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
                logger.warning(f"sub_agents config: {raw_url} resolved to empty — skipping")
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

    # Mount tool routers under api_prefix
    for router in agent.get_tool_routers():
        app.include_router(router, prefix=config.api_prefix)

    # Mount protocol routes
    _mount_protocol_routes(app)

    # Auto-register with agent registry (if configured)
    if config.registry:
        public_url = _resolve_env_var(config.url) if config.url else ""
        registry_url = _resolve_env_var(config.registry)
        if registry_url:
            _schedule_registration(app, registry_url, public_url)
        else:
            logger.warning("registry env var resolved to empty — skipping registration")

    return ctx


def _schedule_registration(app: FastAPI, registry_url: str, public_url: str) -> None:
    """Schedule a background task to register with an agent registry after startup.

    ``public_url`` is the agent's externally-reachable URL (e.g. its
    Databricks App URL). The registry will crawl ``{public_url}/.well-known/agent.json``
    to populate the agent card.

    Runs after the server is accepting requests. Failures are logged but
    don't block startup.
    """
    import asyncio

    import httpx

    async def _register() -> None:
        # Give the server a moment to start accepting requests
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
                    f"{url}/api/agents/register",
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
                logger.info(
                    "Registered with agent registry at %s as '%s'",
                    url, data.get("id", "unknown"),
                )
        except Exception as e:
            logger.warning("Failed to register with agent registry at %s: %s", url, e)

    @app.on_event("startup")
    async def _on_startup() -> None:
        asyncio.create_task(_register())


def _mount_protocol_routes(app: FastAPI) -> None:
    """Mount the agent protocol routes at the app root."""
    protocol_router = APIRouter()

    @protocol_router.get("/.well-known/agent.json", include_in_schema=False)
    async def agent_card(request: Request) -> AgentCard:
        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            raise HTTPException(status_code=404, detail="Agent protocol not configured")
        base = str(request.base_url).rstrip("/")
        mcp_available = getattr(request.app.state, "mcp_server", None) is not None
        return ctx.card.model_copy(update={
            "url": base,
            "mcpEndpoint": f"{base}/mcp" if mcp_available else None,
        })

    @protocol_router.post("/invocations", include_in_schema=False)
    async def invocations(request: Request, body: InvocationRequest) -> Any:
        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            raise HTTPException(status_code=404, detail="Agent protocol not configured")
        return await _handle_invocation(request, body)

    @protocol_router.post("/responses", include_in_schema=False)
    async def responses_api(request: Request) -> Any:
        """OpenAI Responses API compatibility endpoint.

        Accepts the Responses API format used by DatabricksOpenAI when calling
        ``model="apps/<name>"``. Translates to the internal InvocationRequest
        format and delegates to the same handler as /invocations.
        """
        ctx: AgentContext | None = request.app.state.agent_context
        if ctx is None:
            raise HTTPException(status_code=404, detail="Agent protocol not configured")

        import json as _json

        raw = await request.json()

        # Translate Responses API input format to InvocationRequest format
        raw_input = raw.get("input", [])
        if isinstance(raw_input, str):
            messages = [Message(role="user", content=raw_input)]
        else:
            messages = []
            for item in raw_input:
                if isinstance(item, dict):
                    role = item.get("role", "user")
                    content = item.get("content", "")
                    # Handle {"type": "message", "role": "user", "content": "..."}
                    if isinstance(content, list):
                        # Content array format: [{"type": "input_text", "text": "..."}]
                        text_parts = [
                            p.get("text", "") for p in content
                            if isinstance(p, dict) and p.get("type") in ("input_text", "text")
                        ]
                        content = " ".join(text_parts) if text_parts else str(content)
                    messages.append(Message(role=role, content=content))

        body = InvocationRequest(
            input=messages,
            stream=raw.get("stream", False),
            custom_inputs=raw.get("custom_inputs", {}),
        )
        result = await _handle_invocation(request, body)

        # Translate InvocationResponse to Responses API format
        if hasattr(result, 'body'):
            # StreamingResponse — pass through
            return result

        # JSON response — wrap in Responses API format
        if hasattr(result, 'output'):
            response_data = result.model_dump()
        else:
            response_data = {"output": []}

        return {
            "id": f"resp_{id(result)}",
            "object": "response",
            "status": "completed",
            "output": response_data.get("output", []),
            "output_text": _extract_output_text(response_data),
        }

    def _extract_output_text(response_data: dict) -> str:
        """Extract plain text from InvocationResponse output format."""
        for item in response_data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text", "")
        return ""

    @protocol_router.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    class _RawResponse(Response):
        """Sentinel for handlers that write directly to the ASGI socket."""

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            pass

    @protocol_router.get("/mcp/sse", include_in_schema=False)
    async def mcp_sse(request: Request) -> Response:
        """MCP SSE transport — connect MCP clients here (Claude Desktop, Cursor)."""
        mcp_server = getattr(request.app.state, "mcp_server", None)
        mcp_transport = getattr(request.app.state, "mcp_transport", None)
        if mcp_server is None or mcp_transport is None:
            raise HTTPException(status_code=503, detail="MCP server not available")
        from ._mcp import set_mcp_auth
        set_mcp_auth(request.headers.get("Authorization", ""), request.headers.get("X-Forwarded-Access-Token", ""))
        async with mcp_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await mcp_server.run(
                streams[0], streams[1],
                mcp_server.create_initialization_options(),
            )
        return _RawResponse()

    @protocol_router.post("/mcp/messages/", include_in_schema=False)
    async def mcp_messages(request: Request) -> Response:
        """MCP SSE transport — message channel."""
        mcp_transport = getattr(request.app.state, "mcp_transport", None)
        if mcp_transport is None:
            raise HTTPException(status_code=503, detail="MCP server not available")
        await mcp_transport.handle_post_message(
            request.scope, request.receive, request._send
        )
        return _RawResponse()

    async def _mcp_http(request: Request) -> Response:
        """MCP stateless HTTP transport — for Genie Code and AI Playground."""
        mcp_http_manager = getattr(request.app.state, "mcp_http_manager", None)
        if mcp_http_manager is None:
            raise HTTPException(status_code=503, detail="MCP server not available")
        from ._mcp import set_mcp_auth
        set_mcp_auth(request.headers.get("Authorization", ""), request.headers.get("X-Forwarded-Access-Token", ""))
        scope = dict(request.scope)
        headers = list(scope.get("headers", []))
        accept_vals = [v for k, v in headers if k.lower() == b"accept"]
        if not any(b"text/event-stream" in v for v in accept_vals):
            headers = [(k, v) for k, v in headers if k.lower() != b"accept"]
            existing = b", ".join(accept_vals)
            new_accept = b"text/event-stream" + (b", " + existing if existing else b"")
            headers.append((b"accept", new_accept))
            scope["headers"] = headers
        await mcp_http_manager.handle_request(scope, request.receive, request._send)
        return _RawResponse()

    protocol_router.add_api_route(
        "/mcp",
        endpoint=_mcp_http,
        methods=["GET", "POST", "DELETE"],
        include_in_schema=False,
    )

    app.include_router(protocol_router)


async def _setup_mcp(app: FastAPI, ctx: AgentContext) -> Any:
    """Initialize MCP server and transports. Returns the lifecycle context manager."""
    from contextlib import nullcontext

    try:
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        mcp_server, mcp_transport = _build_mcp_components(ctx, app, ctx.config.api_prefix)
        app.state.mcp_server = mcp_server
        app.state.mcp_transport = mcp_transport
        mcp_http_manager = StreamableHTTPSessionManager(mcp_server, stateless=True)
        app.state.mcp_http_manager = mcp_http_manager
        logger.info("MCP server enabled at /mcp/sse (SSE) and /mcp (stateless HTTP)")
        return mcp_http_manager.run()
    except ImportError:
        app.state.mcp_server = None
        app.state.mcp_transport = None
        app.state.mcp_http_manager = None
        logger.warning("mcp package not installed — /mcp endpoints disabled. pip install apx-agent[mcp]")
        return nullcontext()


def create_app(
    agent: BaseAgent,
    config: AgentConfig | None = None,
    pyproject_path: str | None = None,
) -> FastAPI:
    """Create a complete FastAPI app with agent protocol. No APX needed.

    ``pyproject_path`` can be an explicit path to pyproject.toml. When
    omitted, the config is discovered from the entry-point module's location
    or the current working directory.

    Example::

        from apx_agent import Agent, Dependencies, create_app

        def get_billing(customer_id: str, ws: Dependencies.Client) -> dict:
            \"\"\"Get billing history.\"\"\"
            ...

        agent = Agent(tools=[get_billing])
        app = create_app(agent)
        # uvicorn my_app:app --reload
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # Initialize workspace client
        app.state.workspace_client = WorkspaceClient()

        # Setup agent protocol
        ctx = await setup_agent(app, agent, config, pyproject_path=pyproject_path)

        # Setup MCP if agent is configured
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
                # Clean up workspace client if it has a close method
                ws = getattr(app.state, "workspace_client", None)
                if ws and hasattr(ws, "close"):
                    try:
                        ws.close()
                    except Exception:
                        pass

    app = FastAPI(lifespan=lifespan)
    return app
