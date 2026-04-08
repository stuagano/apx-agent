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

from apx_agent import AgentConfig, AgentContext, AgentTool
from apx_agent._agents import BaseAgent
from apx_agent._mcp import _build_mcp_components
from apx_agent._models import AgentCard


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

    app.state.mcp_auth_header = ""
    app.state.mcp_obo_token = ""

    return ctx, app


class TestBuildMcpComponents:
    def test_builds_server_and_transport(self, mcp_ctx_and_app):
        ctx, app = mcp_ctx_and_app
        server, transport = _build_mcp_components(ctx, app, "/api")
        assert server is not None
        assert transport is not None

    @pytest.mark.asyncio
    async def test_list_tools(self, mcp_ctx_and_app):
        ctx, app = mcp_ctx_and_app
        server, _ = _build_mcp_components(ctx, app, "/api")

        handler = server.request_handlers[ListToolsRequest]
        result = await handler(MagicMock())
        # ServerResult wraps the actual response in .root
        inner = result.root
        assert len(inner.tools) == 1
        assert inner.tools[0].name == "my_tool"
        assert "q" in inner.tools[0].inputSchema["properties"]

    @pytest.mark.asyncio
    async def test_call_tool(self, mcp_ctx_and_app):
        ctx, app = mcp_ctx_and_app
        server, _ = _build_mcp_components(ctx, app, "/api")

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
        server, _ = _build_mcp_components(ctx, app, "/api")

        handler = server.request_handlers[CallToolRequest]
        params = CallToolRequestParams(name="nonexistent_tool", arguments={})
        request = CallToolRequest(method="tools/call", params=params)
        result = await handler(request)
        inner = result.root
        assert len(inner.content) == 1
        text = inner.content[0].text.lower()
        assert "error" in text or "404" in text or "not found" in text
