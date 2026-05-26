"""Tests for mcp_tool / mcp_toolkit — consuming remote MCP servers as local tools."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apx_agent import mcp_tool, mcp_toolkit
from apx_agent._resources import get_resources


def _tool(name, description="desc"):
    return SimpleNamespace(name=name, description=description, inputSchema={})


def _text_result(text, is_error=False):
    return SimpleNamespace(
        structuredContent=None,
        content=[SimpleNamespace(type="text", text=text)],
        isError=is_error,
    )


def _fake_session(*, tools=None, call_result=None):
    """A fake MCP ClientSession with list_tools / call_tool."""
    s = MagicMock()
    s.list_tools = AsyncMock(return_value=SimpleNamespace(tools=tools or []))
    s.call_tool = AsyncMock(return_value=call_result or _text_result("ok"))
    return s


def _patch_session(session):
    """Patch _open_session to yield the given fake session."""
    @asynccontextmanager
    async def _fake_open(server_url, headers, transport):
        _fake_open.last_headers = headers  # type: ignore[attr-defined]
        yield session
    return patch("apx_agent.mcp_consume._open_session", _fake_open), _fake_open


class TestMcpToolkit:
    def test_wraps_every_remote_tool(self):
        session = _fake_session(tools=[_tool("search"), _tool("fetch"), _tool("write")])
        p, _ = _patch_session(session)
        with p:
            tools = mcp_toolkit("https://srv/mcp")
        assert [t.__name__ for t in tools] == ["search", "fetch", "write"]

    def test_include_filter(self):
        session = _fake_session(tools=[_tool("search_docs"), _tool("fetch"), _tool("search_web")])
        p, _ = _patch_session(session)
        with p:
            tools = mcp_toolkit("https://srv/mcp", include=["search"])
        assert {t.__name__ for t in tools} == {"search_docs", "search_web"}

    def test_remote_description_used(self):
        session = _fake_session(tools=[_tool("search", "Search the corpus.")])
        p, _ = _patch_session(session)
        with p:
            (tool,) = mcp_toolkit("https://srv/mcp")
        assert tool.__doc__ == "Search the corpus."

    def test_name_slugified(self):
        session = _fake_session(tools=[_tool("Search Docs!")])
        p, _ = _patch_session(session)
        with p:
            (tool,) = mcp_toolkit("https://srv/mcp")
        assert tool.__name__ == "search_docs"

    def test_empty_server(self):
        session = _fake_session(tools=[])
        p, _ = _patch_session(session)
        with p:
            assert mcp_toolkit("https://srv/mcp") == []


class TestMcpTool:
    def test_single_tool_fetches_description(self):
        session = _fake_session(tools=[_tool("search", "Find things.")])
        p, _ = _patch_session(session)
        with p:
            t = mcp_tool("https://srv/mcp", "search")
        assert t.__name__ == "search"
        assert t.__doc__ == "Find things."

    def test_explicit_description_skips_fetch(self):
        # No session needed — description provided, so no discovery call.
        t = mcp_tool("https://srv/mcp", "search", description="Custom.", name="s")
        assert t.__doc__ == "Custom."

    def test_no_resources_by_default(self):
        t = mcp_tool("https://srv/mcp", "search", description="d", name="s")
        assert get_resources(t) == []


class TestCallMechanics:
    @pytest.mark.asyncio
    async def test_call_forwards_arguments_and_extracts_text(self):
        session = _fake_session(call_result=_text_result("hello world"))
        p, _ = _patch_session(session)
        with p:
            t = mcp_tool("https://srv/mcp", "echo", description="d", name="echo")
            out = await t(arguments={"msg": "hi"}, ws=MagicMock(config=None))
        session.call_tool.assert_awaited_once_with("echo", {"msg": "hi"})
        assert out == {"text": "hello world", "is_error": False}

    @pytest.mark.asyncio
    async def test_obo_token_forwarded_to_same_host(self):
        session = _fake_session(call_result=_text_result("ok"))
        p, fake_open = _patch_session(session)
        ws = MagicMock(config=SimpleNamespace(host="https://srv", token="tok-xyz"))
        with p:
            t = mcp_tool("https://srv/mcp", "x", description="d", name="x")
            await t(arguments={}, ws=ws)
        assert fake_open.last_headers["Authorization"] == "Bearer tok-xyz"

    @pytest.mark.asyncio
    async def test_obo_token_NOT_forwarded_to_other_host(self):
        # The Databricks token must never leak to a third-party MCP server.
        session = _fake_session(call_result=_text_result("ok"))
        p, fake_open = _patch_session(session)
        ws = MagicMock(config=SimpleNamespace(host="https://my-workspace", token="tok-secret"))
        with p:
            t = mcp_tool("https://evil-third-party.com/mcp", "x", description="d", name="x")
            await t(arguments={}, ws=ws)
        assert "Authorization" not in (fake_open.last_headers or {})

    @pytest.mark.asyncio
    async def test_call_error_returned_not_raised(self):
        session = _fake_session()
        session.call_tool = AsyncMock(side_effect=RuntimeError("boom"))
        p, _ = _patch_session(session)
        with p:
            t = mcp_tool("https://srv/mcp", "x", description="d", name="x")
            out = await t(arguments={}, ws=MagicMock(config=None))
        assert out["error"] == "boom"

    @pytest.mark.asyncio
    async def test_structured_content_preferred(self):
        result = SimpleNamespace(
            structuredContent={"rows": [1, 2, 3]}, content=[], isError=False
        )
        session = _fake_session(call_result=result)
        p, _ = _patch_session(session)
        with p:
            t = mcp_tool("https://srv/mcp", "q", description="d", name="q")
            out = await t(arguments={}, ws=MagicMock(config=None))
        assert out == {"result": {"rows": [1, 2, 3]}, "is_error": False}
