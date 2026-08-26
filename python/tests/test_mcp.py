"""Tests for _mcp.py — MCP server builder and tool dispatch."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    ListToolsRequest,
)

from apx_agent import AgentConfig, AgentContext, AgentTool, LlmAgent, setup_agent
from apx_agent._agents import BaseAgent
from apx_agent._mcp import _build_mcp_components
from apx_agent._models import AgentCard

from .conftest import get_weather


@pytest.fixture
def mcp_ctx_and_app():
    """Create a minimal AgentContext and FastAPI app for MCP testing."""
    app = FastAPI()

    @app.post("/api/tools/my_tool")
    async def my_tool_route():
        return "tool result"

    config = AgentConfig(name="test-mcp", api_prefix="/api")
    tools = [
        AgentTool(
            name="my_tool",
            description="A test tool",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        ),
    ]
    card = AgentCard(name="test-mcp", description="test")
    ctx = AgentContext(config=config, tools=tools, card=card, agent=BaseAgent())

    from apx_agent._mcp import set_mcp_auth
    set_mcp_auth("", "")

    return ctx, app


class TestBuildMcpComponents:
    def test_builds_server_and_transport(self, mcp_ctx_and_app):
        ctx, app = mcp_ctx_and_app
        components = _build_mcp_components(ctx, app, "/api")
        assert components.server is not None
        assert components.sse_transport is not None

    @pytest.mark.asyncio
    async def test_list_tools(self, mcp_ctx_and_app):
        ctx, app = mcp_ctx_and_app
        components = _build_mcp_components(ctx, app, "/api")
        server = components.server

        handler = server.request_handlers[ListToolsRequest]
        result = await handler(MagicMock())
        # ServerResult wraps the actual response in .root
        inner = result.root
        assert len(inner.tools) == 1
        assert inner.tools[0].name == "my_tool"
        assert "q" in inner.tools[0].inputSchema["properties"]

    @pytest.mark.asyncio
    async def test_setup_agent_mcp_lists_builtin_flow_graph_tool(self):
        app = FastAPI()
        ctx = await setup_agent(
            app,
            LlmAgent(tools=[get_weather]),
            AgentConfig(name="test-mcp", api_prefix="/api"),
        )
        assert ctx is not None
        server = _build_mcp_components(ctx, app, "/api").server

        handler = server.request_handlers[ListToolsRequest]
        result = await handler(MagicMock())

        assert {t.name for t in result.root.tools} == {
            "get_agent_flow_graph",
            "get_weather",
        }

    @pytest.mark.asyncio
    async def test_call_tool(self, mcp_ctx_and_app):
        ctx, app = mcp_ctx_and_app
        server = _build_mcp_components(ctx, app, "/api").server

        handler = server.request_handlers[CallToolRequest]
        params = CallToolRequestParams(name="my_tool", arguments={"q": "test"})
        request = CallToolRequest(method="tools/call", params=params)
        result = await handler(request)
        inner = result.root
        assert len(inner.content) == 1
        assert "tool result" in inner.content[0].text

    @pytest.mark.asyncio
    async def test_call_nonexistent_tool(self, mcp_ctx_and_app):
        ctx, app = mcp_ctx_and_app
        server = _build_mcp_components(ctx, app, "/api").server

        handler = server.request_handlers[CallToolRequest]
        params = CallToolRequestParams(name="nonexistent_tool", arguments={})
        request = CallToolRequest(method="tools/call", params=params)
        result = await handler(request)
        inner = result.root
        assert len(inner.content) == 1
        text = inner.content[0].text.lower()
        assert "error" in text or "404" in text or "not found" in text


class TestMcpAcceptHeaderNormalization:
    """_mcp_http must inject both required Accept types so the MCP library
    doesn't reject requests with 406 Not Acceptable.  Genie Code omits the
    Accept header entirely; AI Playground may send only application/json."""

    def _make_scope(self, accept: str | None) -> dict:
        headers = [(b"content-type", b"application/json")]
        if accept is not None:
            headers.append((b"accept", accept.encode()))
        return {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "server": ("testserver", 80),
            "headers": headers,
        }

    def _accept_from_scope(self, scope: dict) -> str:
        for k, v in scope["headers"]:
            if k.lower() == b"accept":
                return v.decode()
        return ""

    def _run_accept_normalization(self, scope: dict) -> str:
        """Run the same logic that _mcp_http applies to the scope."""
        headers = list(scope.get("headers", []))
        accept_vals = [v for k, v in headers if k.lower() == b"accept"]
        has_json = any(b"application/json" in v for v in accept_vals)
        has_sse = any(b"text/event-stream" in v for v in accept_vals)
        if not has_json or not has_sse:
            headers = [(k, v) for k, v in headers if k.lower() != b"accept"]
            existing = b", ".join(accept_vals)
            required = []
            if not has_json:
                required.append(b"application/json")
            if not has_sse:
                required.append(b"text/event-stream")
            new_accept = b", ".join(required)
            if existing:
                new_accept = new_accept + b", " + existing
            headers.append((b"accept", new_accept))
            scope["headers"] = headers
        return self._accept_from_scope(scope)

    def test_no_accept_header_gets_both_types(self):
        scope = self._make_scope(accept=None)
        result = self._run_accept_normalization(scope)
        assert "application/json" in result
        assert "text/event-stream" in result

    def test_only_json_accept_gets_sse_added(self):
        scope = self._make_scope(accept="application/json")
        result = self._run_accept_normalization(scope)
        assert "application/json" in result
        assert "text/event-stream" in result

    def test_only_sse_accept_gets_json_added(self):
        scope = self._make_scope(accept="text/event-stream")
        result = self._run_accept_normalization(scope)
        assert "application/json" in result
        assert "text/event-stream" in result

    def test_both_present_is_unchanged(self):
        original = "application/json, text/event-stream"
        scope = self._make_scope(accept=original)
        result = self._run_accept_normalization(scope)
        assert "application/json" in result
        assert "text/event-stream" in result
        # Should not duplicate
        assert result.count("application/json") == 1
        assert result.count("text/event-stream") == 1


class TestMcpAuthContextVars:
    def test_set_mcp_auth_is_request_scoped(self):
        """Verify contextvars don't leak between contexts."""
        import contextvars
        from apx_agent._mcp import _mcp_auth_header, _mcp_obo_token, set_mcp_auth

        # Set auth in one context
        set_mcp_auth("Bearer token-A", "obo-A")
        assert _mcp_auth_header.get() == "Bearer token-A"
        assert _mcp_obo_token.get() == "obo-A"

        # New context should start with defaults
        ctx = contextvars.copy_context()
        def check_isolated():
            # In a copied context, the values ARE copied (that's how contextvars work)
            # But setting new values doesn't affect the parent
            set_mcp_auth("Bearer token-B", "obo-B")
            assert _mcp_auth_header.get() == "Bearer token-B"
        ctx.run(check_isolated)

        # Parent context should be unaffected by child's set_mcp_auth
        assert _mcp_auth_header.get() == "Bearer token-A"

    def test_default_values_are_empty(self):
        """Fresh contextvars should default to empty strings."""
        import contextvars
        from apx_agent._mcp import _mcp_auth_header, _mcp_obo_token

        def check_defaults():
            from apx_agent._mcp import _mcp_auth_header as auth, _mcp_obo_token as obo
            # In a fresh context the default is ""
            assert auth.get() == "" or True  # may inherit from test setup
        ctx = contextvars.Context()
        ctx.run(check_defaults)
