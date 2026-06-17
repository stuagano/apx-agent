"""Tests for ``_compile_run.py`` — the runtime that replaces the legacy
``run_via_sdk`` for ``BaseAgent.run()`` / ``BaseAgent.stream()``.

Verifies:

  1. ``_resolve_request_ws`` prefers the OBO header over the SP fallback.
  2. ``_resolve_request_ws`` falls back to ``request.app.state.workspace_client``
     when the OBO header is absent.
  3. ``run_via_compile`` compiles the agent against the resolved ws, invokes
     the graph, and returns the final assistant text.
  4. ``stream_via_compile`` yields one chunk per non-tool-call AIMessage from
     the graph's node updates.
  5. The agent classes' public ``.run()`` / ``.stream()`` API still works
     end-to-end after the swap (smoke test through ``LlmAgent.run``).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from apx_agent import LlmAgent, AgentConfig, Message  # noqa: E402
from apx_agent._compile_run import (  # noqa: E402
    _final_text,
    _resolve_request_ws,
    _to_langchain,
    run_via_compile,
    stream_via_compile,
)


# ---------------------------------------------------------------------------
# Helpers + fixtures
# ---------------------------------------------------------------------------


def _trivial_tool(query: str) -> str:
    """Return the query."""
    return f"got: {query}"


def _fake_request(
    *,
    obo_token: str | None = None,
    sp_ws: Any = None,
    model: str = "databricks-claude-sonnet-4-6",
) -> MagicMock:
    """Build a request stand-in that ``run_via_compile`` can consume."""
    req = MagicMock(name="fake_request")
    req.headers = {}
    if obo_token:
        req.headers["X-Forwarded-Access-Token"] = obo_token
    req.app.state.workspace_client = sp_ws or MagicMock(name="sp_ws")
    req.app.state.agent_context.config.model = model
    return req


# ---------------------------------------------------------------------------
# _resolve_request_ws
# ---------------------------------------------------------------------------


class TestResolveRequestWs:
    def test_obo_header_wins(self) -> None:
        sp_ws = MagicMock(name="sp_ws")
        req = _fake_request(obo_token="user-tok", sp_ws=sp_ws)
        with patch(
            "apx_agent._defaults._make_workspace_client"
        ) as mock_factory:
            obo_ws = MagicMock(name="obo_ws")
            mock_factory.return_value = obo_ws
            result = _resolve_request_ws(req)
            mock_factory.assert_called_once()
            assert result is obo_ws
            # SP client must NOT be returned when OBO is present.
            assert result is not sp_ws

    def test_falls_back_to_app_state(self) -> None:
        sp_ws = MagicMock(name="sp_ws")
        req = _fake_request(obo_token=None, sp_ws=sp_ws)
        result = _resolve_request_ws(req)
        assert result is sp_ws


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


class TestMessageConversion:
    def test_to_langchain_prepends_system_prompt_when_absent(self) -> None:
        msgs = [Message(role="user", content="hi")]
        out = _to_langchain(msgs, system_prompt="You are helpful.")
        from langchain_core.messages import SystemMessage

        assert isinstance(out[0], SystemMessage)
        assert out[0].content == "You are helpful."

    def test_to_langchain_skips_system_when_already_present(self) -> None:
        msgs = [
            Message(role="system", content="Existing system"),
            Message(role="user", content="hi"),
        ]
        out = _to_langchain(msgs, system_prompt="Would-be-duplicate")
        # Only one system message, and it's the existing one.
        from langchain_core.messages import SystemMessage

        systems = [m for m in out if isinstance(m, SystemMessage)]
        assert len(systems) == 1
        assert systems[0].content == "Existing system"

    def test_final_text_picks_last_assistant_message(self) -> None:
        msgs = [
            HumanMessage(content="hi"),
            AIMessage(content="thinking", tool_calls=[{"id": "t1", "name": "x", "args": {}}]),
            AIMessage(content="final answer"),
        ]
        assert _final_text(msgs) == "final answer"


# ---------------------------------------------------------------------------
# run_via_compile / stream_via_compile
# ---------------------------------------------------------------------------


class TestRunViaCompile:
    @pytest.mark.asyncio
    async def test_compiles_invokes_returns_text(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        req = _fake_request()

        fake_graph = MagicMock()
        # H1: run_via_compile now awaits compiled.ainvoke(...) — the async
        # graph API — so ainvoke must be an awaitable returning the result dict.
        fake_graph.ainvoke = AsyncMock(return_value={
            "messages": [HumanMessage(content="hi"), AIMessage(content="hello!")]
        })

        with patch(
            "apx_agent._compile.compile_to_langgraph", return_value=fake_graph
        ) as mock_compile:
            text = await run_via_compile(
                agent, [Message(role="user", content="hi")], req
            )

        assert text == "hello!"
        mock_compile.assert_called_once()
        # The model came from request.app.state.agent_context.config.model
        _, kwargs = mock_compile.call_args
        assert kwargs["model"] == "databricks-claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_obo_ws_threaded_into_compile(self) -> None:
        """The OBO header builds a user-scoped ws and that ws is what compile
        gets — the whole OBO chain stays intact through this runtime."""
        agent = LlmAgent(tools=[_trivial_tool])
        req = _fake_request(obo_token="user-tok")
        fake_graph = MagicMock()
        fake_graph.ainvoke = AsyncMock(
            return_value={"messages": [AIMessage(content="ok")]}
        )
        obo_ws = MagicMock(name="obo_ws")

        with patch(
            "apx_agent._defaults._make_workspace_client", return_value=obo_ws
        ), patch(
            "apx_agent._compile.compile_to_langgraph", return_value=fake_graph
        ) as mock_compile:
            await run_via_compile(agent, [Message(role="user", content="hi")], req)

        _, kwargs = mock_compile.call_args
        assert kwargs["ws"] is obo_ws


class TestStreamViaCompile:
    @pytest.mark.asyncio
    async def test_yields_text_chunks_per_ai_message(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        req = _fake_request()

        # H1: stream_via_compile now consumes compiled.astream(...) via
        # ``async for`` — the async graph API — so astream must return an
        # async generator yielding the stream_mode="updates" chunks.
        chunks_to_yield = [
            {"node_a": {"messages": [AIMessage(content="first")]}},
            {"node_b": {"messages": [AIMessage(
                content="thinking",
                tool_calls=[{"id": "t1", "name": "x", "args": {}}],
            )]}},
            {"node_c": {"messages": [AIMessage(content="last")]}},
        ]

        async def _fake_astream(*args: Any, **kwargs: Any) -> Any:
            for chunk in chunks_to_yield:
                yield chunk

        fake_graph = MagicMock()
        fake_graph.astream = _fake_astream

        with patch(
            "apx_agent._compile.compile_to_langgraph", return_value=fake_graph
        ):
            chunks = [
                c
                async for c in stream_via_compile(
                    agent, [Message(role="user", content="hi")], req
                )
            ]

        # Tool-call AIMessage is filtered out; only "first" and "last" stream.
        assert chunks == ["first", "last"]


# ---------------------------------------------------------------------------
# End-to-end: agent class .run() now flows through compile
# ---------------------------------------------------------------------------


class TestAgentRunIntegratesCompile:
    @pytest.mark.asyncio
    async def test_llm_agent_run_uses_compile_runtime(self) -> None:
        """``LlmAgent.run`` no longer reaches ``run_via_sdk`` — it goes
        through ``run_via_compile`` and out to the compile path."""
        agent = LlmAgent(tools=[_trivial_tool])
        req = _fake_request()

        fake_graph = MagicMock()
        fake_graph.ainvoke = AsyncMock(
            return_value={"messages": [AIMessage(content="answer")]}
        )

        with patch(
            "apx_agent._compile.compile_to_langgraph", return_value=fake_graph
        ) as mock_compile:
            result = await agent.run([Message(role="user", content="hi")], req)

        assert result == "answer"
        mock_compile.assert_called_once()  # the compile path was exercised
