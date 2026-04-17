"""Tests for genie_tool() factory."""

from __future__ import annotations

import inspect
from typing import get_type_hints
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apx_agent.genie import genie_tool
from apx_agent._inspection import _inspect_tool_fn


class TestGenieToolFactory:
    def test_returns_callable(self):
        tool = genie_tool("space-123")
        assert callable(tool)

    def test_default_name(self):
        tool = genie_tool("space-123")
        assert tool.__name__ == "ask_genie"

    def test_custom_name(self):
        tool = genie_tool("space-123", name="sales_data")
        assert tool.__name__ == "sales_data"

    def test_default_description_mentions_space_id(self):
        tool = genie_tool("space-abc")
        assert "space-abc" in (tool.__doc__ or "")

    def test_custom_description(self):
        tool = genie_tool("space-123", description="Answer sales questions")
        assert tool.__doc__ == "Answer sales questions"

    def test_is_coroutine_function(self):
        tool = genie_tool("space-123")
        assert inspect.iscoroutinefunction(tool)

    def test_inspection_sees_question_as_plain_param(self):
        """_inspect_tool_fn should treat 'question' as a plain LLM parameter."""
        tool = genie_tool("space-123")
        plain_params, dep_params = _inspect_tool_fn(tool)
        assert "question" in plain_params
        assert plain_params["question"][0] is str

    def test_inspection_sees_ws_as_dependency(self):
        """_inspect_tool_fn should exclude 'ws' from the tool schema."""
        tool = genie_tool("space-123")
        plain_params, dep_params = _inspect_tool_fn(tool)
        assert "question" not in dep_params
        assert "ws" in dep_params


class TestGenieToolExecution:
    def _make_ws(self, status: str = "COMPLETED", content: str = "Revenue was $1M") -> MagicMock:
        ws = MagicMock()
        ws.api_client.do.side_effect = [
            # start_conversation response
            {"conversation_id": "conv-1", "message_id": "msg-1"},
            # poll response (COMPLETED)
            {
                "status": status,
                "attachments": [{"text": {"content": content}}],
            },
        ]
        return ws

    @pytest.mark.asyncio
    async def test_returns_answer_on_completed(self):
        ws = self._make_ws(status="COMPLETED", content="Revenue was $1M")
        tool = genie_tool("space-123")
        result = await tool(question="What was revenue?", ws=ws)
        assert result == "Revenue was $1M"

    @pytest.mark.asyncio
    async def test_calls_start_conversation_with_question(self):
        ws = self._make_ws()
        tool = genie_tool("space-123")
        await tool(question="Show me sales", ws=ws)

        first_call = ws.api_client.do.call_args_list[0]
        assert first_call.args[0] == "POST"
        assert "space-123" in first_call.args[1]
        assert first_call.kwargs["body"]["content"] == "Show me sales"

    @pytest.mark.asyncio
    async def test_returns_empty_string_when_no_attachments(self):
        ws = MagicMock()
        ws.api_client.do.side_effect = [
            {"conversation_id": "conv-1", "message_id": "msg-1"},
            {"status": "COMPLETED", "attachments": []},
        ]
        tool = genie_tool("space-123")
        result = await tool(question="test", ws=ws)
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_failure_message_on_failed(self):
        ws = MagicMock()
        ws.api_client.do.side_effect = [
            {"conversation_id": "conv-1", "message_id": "msg-1"},
            {"status": "FAILED", "attachments": []},
        ]
        tool = genie_tool("space-123")
        result = await tool(question="bad query", ws=ws)
        assert "failed" in result.lower()

    @pytest.mark.asyncio
    async def test_polls_until_completed(self):
        """Should retry if status is not terminal on first poll."""
        ws = MagicMock()
        ws.api_client.do.side_effect = [
            {"conversation_id": "conv-1", "message_id": "msg-1"},
            {"status": "EXECUTING_QUERY", "attachments": []},
            {"status": "COMPLETED", "attachments": [{"text": {"content": "Done"}}]},
        ]
        tool = genie_tool("space-123")
        with patch("apx_agent.genie.asyncio.sleep", new_callable=AsyncMock):
            result = await tool(question="test", ws=ws)
        assert result == "Done"
