"""MCP server — SSE and stateless HTTP transports for Genie Code / Claude Desktop / Cursor."""

from __future__ import annotations

import contextvars
from typing import Any

from fastapi import FastAPI

from ._models import AgentContext

# Request-scoped auth context — avoids race conditions with concurrent MCP clients
_mcp_auth_header: contextvars.ContextVar[str] = contextvars.ContextVar("_mcp_auth_header", default="")
_mcp_obo_token: contextvars.ContextVar[str] = contextvars.ContextVar("_mcp_obo_token", default="")


def set_mcp_auth(auth_header: str, obo_token: str) -> None:
    """Set MCP auth context for the current request (called from protocol routes)."""
    _mcp_auth_header.set(auth_header)
    _mcp_obo_token.set(obo_token)


def _build_mcp_components(ctx: AgentContext, app: FastAPI, api_prefix: str = "/api") -> tuple[Any, Any]:
    """Build an MCP Server + SseServerTransport from the agent's tool registry.

    Returns (server, sse_transport) to be stored on app.state.
    Tool calls are dispatched via ASGI to the existing {api_prefix}/tools/<name> routes
    so they share the same FastAPI dependency injection (auth, workspace client, etc.).
    """
    from mcp.server import Server
    from mcp.server.sse import SseServerTransport
    import mcp.types as mcp_types

    server: Any = Server(ctx.config.name)
    sse = SseServerTransport("/mcp/messages/")

    @server.list_tools()
    async def _list_tools() -> list[Any]:
        return [
            mcp_types.Tool(
                name=t.name,
                description=t.description or "",
                inputSchema=t.input_schema or {"type": "object", "properties": {}},
            )
            for t in ctx.tools
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[Any]:
        from httpx import ASGITransport, AsyncClient

        # Read auth from request-scoped contextvars (set by protocol routes)
        mcp_auth = _mcp_auth_header.get()
        mcp_obo = _mcp_obo_token.get()
        extra_headers: dict[str, str] = {}
        if mcp_auth:
            extra_headers["Authorization"] = mcp_auth
        if mcp_obo:
            extra_headers["X-Forwarded-Access-Token"] = mcp_obo

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://internal",
        ) as client:
            response = await client.post(
                f"{api_prefix}/tools/{name}",
                json=arguments or {},
                headers=extra_headers,
            )

        import json as _json

        if response.status_code >= 400:
            text = f"Tool error ({response.status_code}): {response.text}"
        else:
            result = response.json()
            text = result if isinstance(result, str) else _json.dumps(result, indent=2)

        return [mcp_types.TextContent(type="text", text=text)]

    return server, sse
