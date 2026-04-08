"""Tests for _llm_loop.py — tool dispatch, context trimming, invocation handler."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import Agent, AgentConfig, AgentContext, AgentTool, Message
from apx_agent._llm_loop import (
    _build_tool_schemas,
    _dispatch_tool_call,
    _handle_invocation,
    _maybe_trim_context,
    _post_with_retry,
)
from apx_agent._models import InvocationRequest

from .conftest import get_weather, make_llm_response, make_tool_call, query_genie


# ---------------------------------------------------------------------------
# _build_tool_schemas
# ---------------------------------------------------------------------------


class TestBuildToolSchemas:
    def test_converts_tools(self):
        tools = [
            AgentTool(
                name="get_weather",
                description="Get weather",
                input_schema={"type": "object", "properties": {"city": {"type": "string"}}},
            ),
        ]
        schemas = _build_tool_schemas(tools)
        assert len(schemas) == 1
        assert schemas[0]["type"] == "function"
        assert schemas[0]["function"]["name"] == "get_weather"
        assert "city" in schemas[0]["function"]["parameters"]["properties"]

    def test_empty_tools(self):
        assert _build_tool_schemas([]) == []

    def test_missing_input_schema(self):
        tools = [AgentTool(name="t", description="d", input_schema=None)]
        schemas = _build_tool_schemas(tools)
        assert schemas[0]["function"]["parameters"] == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# _dispatch_tool_call — local tool
# ---------------------------------------------------------------------------


class TestDispatchToolCall:
    @pytest.mark.asyncio
    async def test_local_tool_dispatch(self):
        """Local tools should be dispatched via ASGI to the tool route."""
        app = FastAPI()

        @app.post("/api/tools/my_tool")
        async def my_tool_route():
            return "tool result"

        tool = AgentTool(name="my_tool", description="test tool")
        ctx = MagicMock(spec=AgentContext)
        ctx.get_tool.return_value = tool
        ctx.config = AgentConfig(name="test", api_prefix="/api")

        request = MagicMock()
        request.app = app
        request.headers = {}

        tool_call = make_tool_call("my_tool", {"key": "value"})
        result = await _dispatch_tool_call(request, tool_call, ctx)
        assert "tool result" in str(result)

    @pytest.mark.asyncio
    async def test_sub_agent_dispatch(self):
        """Sub-agent tools should POST to the remote /invocations endpoint."""
        tool = AgentTool(
            name="remote_agent",
            description="Remote agent",
            sub_agent_url="http://remote-agent.com",
        )
        ctx = MagicMock(spec=AgentContext)
        ctx.get_tool.return_value = tool
        ctx.config = AgentConfig(name="test")

        request = MagicMock()
        request.headers = {}

        tool_call = make_tool_call("remote_agent", {"message": "hello"})

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "output": [{"content": [{"text": "agent response"}]}]
        }

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await _dispatch_tool_call(request, tool_call, ctx)
            assert result == "agent response"

    @pytest.mark.asyncio
    async def test_sub_agent_error_handling(self):
        """Sub-agent HTTP errors should return error message, not raise."""
        tool = AgentTool(
            name="failing_agent",
            description="Failing",
            sub_agent_url="http://failing.com",
        )
        ctx = MagicMock(spec=AgentContext)
        ctx.get_tool.return_value = tool
        ctx.config = AgentConfig(name="test")

        request = MagicMock()
        request.headers = {}

        tool_call = make_tool_call("failing_agent", {"message": "hello"})

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            result = await _dispatch_tool_call(request, tool_call, ctx)
            assert "Sub-agent error" in result
            assert "500" in result

    @pytest.mark.asyncio
    async def test_malformed_arguments_handled(self):
        """Invalid JSON in tool call arguments should default to empty dict."""
        app = FastAPI()

        @app.post("/api/tools/my_tool")
        async def my_tool_route():
            return "ok"

        tool = AgentTool(name="my_tool", description="test")
        ctx = MagicMock(spec=AgentContext)
        ctx.get_tool.return_value = tool
        ctx.config = AgentConfig(name="test", api_prefix="/api")

        request = MagicMock()
        request.app = app
        request.headers = {}

        tool_call = {
            "id": "call_1",
            "function": {"name": "my_tool", "arguments": "not valid json {{{"},
        }
        result = await _dispatch_tool_call(request, tool_call, ctx)
        assert result is not None


# ---------------------------------------------------------------------------
# _post_with_retry
# ---------------------------------------------------------------------------


class TestPostWithRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_try(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post.return_value = mock_response

        result = await _post_with_retry(
            mock_client, "http://example.com", json={}, headers={},
        )
        assert result.status_code == 200
        assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_503(self):
        mock_client = AsyncMock()
        fail_resp = MagicMock()
        fail_resp.status_code = 503
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        mock_client.post.side_effect = [fail_resp, ok_resp]

        with patch("apx_agent._llm_loop._RETRY_BACKOFF_BASE", 0.01):
            result = await _post_with_retry(
                mock_client, "http://example.com", json={}, headers={},
            )
        assert result.status_code == 200
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_retries_on_429(self):
        mock_client = AsyncMock()
        fail_resp = MagicMock()
        fail_resp.status_code = 429
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        mock_client.post.side_effect = [fail_resp, ok_resp]

        with patch("apx_agent._llm_loop._RETRY_BACKOFF_BASE", 0.01):
            result = await _post_with_retry(
                mock_client, "http://example.com", json={}, headers={},
            )
        assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_returns_error_after_max_retries(self):
        mock_client = AsyncMock()
        fail_resp = MagicMock()
        fail_resp.status_code = 503
        mock_client.post.return_value = fail_resp

        with patch("apx_agent._llm_loop._RETRY_BACKOFF_BASE", 0.01):
            result = await _post_with_retry(
                mock_client, "http://example.com", json={}, headers={},
            )
        # After max retries, returns the last response
        assert result.status_code == 503

    @pytest.mark.asyncio
    async def test_retries_on_exception(self):
        mock_client = AsyncMock()
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        mock_client.post.side_effect = [ConnectionError("timeout"), ok_resp]

        with patch("apx_agent._llm_loop._RETRY_BACKOFF_BASE", 0.01):
            result = await _post_with_retry(
                mock_client, "http://example.com", json={}, headers={},
            )
        assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_raises_after_all_retries_fail(self):
        mock_client = AsyncMock()
        mock_client.post.side_effect = ConnectionError("network down")

        with patch("apx_agent._llm_loop._RETRY_BACKOFF_BASE", 0.01):
            with pytest.raises(ConnectionError):
                await _post_with_retry(
                    mock_client, "http://example.com", json={}, headers={},
                )

    @pytest.mark.asyncio
    async def test_no_retry_on_400(self):
        """Client errors (4xx except 429) should not be retried."""
        mock_client = AsyncMock()
        bad_resp = MagicMock()
        bad_resp.status_code = 400
        mock_client.post.return_value = bad_resp

        result = await _post_with_retry(
            mock_client, "http://example.com", json={}, headers={},
        )
        assert result.status_code == 400
        assert mock_client.post.call_count == 1


# ---------------------------------------------------------------------------
# _maybe_trim_context
# ---------------------------------------------------------------------------


class TestMaybeTrimContext:
    @pytest.mark.asyncio
    async def test_no_trimming_under_budget(self):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        result = await _maybe_trim_context(messages, 10000, MagicMock(), "", {})
        assert result == messages

    @pytest.mark.asyncio
    async def test_trimming_over_budget(self):
        long_content = "x" * 4000  # ~1000 tokens
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": long_content},
            {"role": "assistant", "content": long_content},
            {"role": "user", "content": long_content},
            {"role": "assistant", "content": "Final"},
            {"role": "user", "content": "Last question"},
        ]

        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Summary of conversation"}}]
        }
        mock_client.post.return_value = mock_response

        result = await _maybe_trim_context(messages, 100, mock_client, "http://llm/invoke", {})
        # Should have: system + summary + last 2 messages
        assert len(result) == 4
        assert result[0]["role"] == "system"
        assert "Summary" in result[1]["content"]

    @pytest.mark.asyncio
    async def test_trimming_fallback_on_error(self):
        long_content = "x" * 4000
        messages = [
            {"role": "user", "content": long_content},
            {"role": "assistant", "content": long_content},
            {"role": "user", "content": long_content},
            {"role": "assistant", "content": "last"},
        ]

        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("LLM unavailable")

        result = await _maybe_trim_context(messages, 100, mock_client, "http://llm/invoke", {})
        assert result == messages

    @pytest.mark.asyncio
    async def test_no_trimming_few_messages(self):
        messages = [
            {"role": "user", "content": "x" * 4000},
            {"role": "assistant", "content": "x" * 4000},
        ]
        result = await _maybe_trim_context(messages, 100, MagicMock(), "", {})
        assert result == messages


# ---------------------------------------------------------------------------
# _handle_invocation
# ---------------------------------------------------------------------------


class TestHandleInvocation:
    @pytest.mark.asyncio
    async def test_returns_503_when_no_context(self):
        from fastapi import HTTPException

        request = MagicMock()
        request.app.state.agent_context = None
        body = InvocationRequest(input="test")

        with pytest.raises(HTTPException) as exc_info:
            await _handle_invocation(request, body)
        assert exc_info.value.status_code == 503
