"""Tests for ``apx_agent._executor`` — the executor protocol and event types.

Covers:

1. ``ExecutorConfig`` default field values.
2. ``TextChunk`` field access.
3. ``TurnComplete`` optional field defaults.
4. ``ExecutorError`` default ``retryable`` value.
5. ``ToolCallRequest`` default ``args`` and ``call_id`` values.
6. ``Executor.handles_tools_internally()`` returns ``False`` by default.
7. All concrete event types are subclasses of ``ExecutorEvent``.
8. A minimal ``MockExecutor`` subclass that yields ``TurnComplete`` works
   correctly when consumed via an async loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from apx_agent._executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    TurnComplete,
)


# ---------------------------------------------------------------------------
# ExecutorConfig
# ---------------------------------------------------------------------------


class TestExecutorConfigDefaults:
    def test_executor_config_defaults(self) -> None:
        """ExecutorConfig() has model=None, temperature=0.0, max_tokens=100_000."""
        cfg = ExecutorConfig()
        assert cfg.model is None
        assert cfg.temperature == 0.0
        assert cfg.max_tokens == 100_000


# ---------------------------------------------------------------------------
# TextChunk
# ---------------------------------------------------------------------------


class TestTextChunk:
    def test_text_chunk_fields(self) -> None:
        """TextChunk(text='hello') stores text correctly."""
        chunk = TextChunk(text="hello")
        assert chunk.text == "hello"

    def test_text_chunk_default_none(self) -> None:
        """TextChunk() defaults to text=None when no argument is given."""
        chunk = TextChunk()
        assert chunk.text is None


# ---------------------------------------------------------------------------
# TurnComplete
# ---------------------------------------------------------------------------


class TestTurnComplete:
    def test_turn_complete_optional_fields(self) -> None:
        """TurnComplete() has response=None, usage=None by default."""
        tc = TurnComplete()
        assert tc.response is None
        assert tc.usage is None

    def test_turn_complete_with_values(self) -> None:
        """TurnComplete stores response and usage when provided."""
        tc = TurnComplete(response="hi", usage={"input_tokens": 5})
        assert tc.response == "hi"
        assert tc.usage == {"input_tokens": 5}


# ---------------------------------------------------------------------------
# ExecutorError
# ---------------------------------------------------------------------------


class TestExecutorError:
    def test_executor_error_defaults(self) -> None:
        """ExecutorError(message='oops') defaults retryable to False."""
        err = ExecutorError(message="oops")
        assert err.message == "oops"
        assert err.retryable is False

    def test_executor_error_retryable_true(self) -> None:
        """ExecutorError accepts retryable=True."""
        err = ExecutorError(message="timeout", retryable=True)
        assert err.retryable is True


# ---------------------------------------------------------------------------
# ToolCallRequest
# ---------------------------------------------------------------------------


class TestToolCallRequest:
    def test_tool_call_request_defaults(self) -> None:
        """ToolCallRequest(name='sql') has args={}, call_id=None by default."""
        req = ToolCallRequest(name="sql")
        assert req.name == "sql"
        assert req.args == {}
        assert req.call_id is None

    def test_tool_call_request_args_not_shared(self) -> None:
        """Two ToolCallRequest instances do not share the same args dict."""
        r1 = ToolCallRequest(name="a")
        r2 = ToolCallRequest(name="b")
        r1.args["key"] = "val"
        assert r2.args == {}


# ---------------------------------------------------------------------------
# ToolCallComplete
# ---------------------------------------------------------------------------


class TestToolCallComplete:
    def test_tool_call_complete_defaults(self) -> None:
        """ToolCallComplete() defaults call_id to None, result to None, error to None."""
        tcc = ToolCallComplete(name="my_tool")
        assert tcc.call_id is None
        assert tcc.result is None
        assert tcc.error is None


# ---------------------------------------------------------------------------
# Executor base class
# ---------------------------------------------------------------------------


class TestExecutorBase:
    def test_base_executor_handles_tools_internally_false(self) -> None:
        """Executor().handles_tools_internally() returns False by default."""
        ex = Executor()
        assert ex.handles_tools_internally() is False

    def test_base_executor_supports_streaming_false(self) -> None:
        """Executor().supports_streaming() returns False by default."""
        ex = Executor()
        assert ex.supports_streaming() is False


# ---------------------------------------------------------------------------
# All events are ExecutorEvent subclasses
# ---------------------------------------------------------------------------


class TestEventInheritance:
    def test_all_events_are_executor_event_subclasses(self) -> None:
        """All concrete event types are subclasses of ExecutorEvent."""
        for cls in (TextChunk, ToolCallRequest, ToolCallComplete, TurnComplete, ExecutorError):
            instance = cls()
            assert isinstance(instance, ExecutorEvent), (
                f"{cls.__name__} is not an instance of ExecutorEvent"
            )


# ---------------------------------------------------------------------------
# MockExecutor — verifies the async generator contract
# ---------------------------------------------------------------------------


class MockExecutor(Executor):
    """Minimal executor that yields a single TurnComplete(response='hi').

    Used to verify that the async generator protocol works end-to-end without
    depending on any real LLM or LangGraph runtime.
    """

    async def run_turn(
        self,
        messages: list[Any],
        tools: list[Any],
        system_prompt: str,
        config: ExecutorConfig | None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Yield a single TurnComplete event.

        :param messages: Unused.
        :param tools: Unused.
        :param system_prompt: Unused.
        :param config: Unused.
        :returns: An async iterator yielding one ``TurnComplete(response='hi')``.
        """
        yield TurnComplete(response="hi")


class TestMockExecutor:
    @pytest.mark.asyncio
    async def test_mock_executor_yields_turn_complete(self) -> None:
        """A MockExecutor subclass yielding TurnComplete works via async loop."""
        executor = MockExecutor()
        events = [e async for e in executor.run_turn([], [], "", None)]
        assert any(isinstance(e, TurnComplete) for e in events)
        tc = next(e for e in events if isinstance(e, TurnComplete))
        assert tc.response == "hi"

    @pytest.mark.asyncio
    async def test_mock_executor_handles_tools_internally_inherits_false(self) -> None:
        """MockExecutor inherits handles_tools_internally()=False from Executor base."""
        executor = MockExecutor()
        assert executor.handles_tools_internally() is False
