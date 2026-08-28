"""Tests for ``apx_agent._langgraph_executor.LangGraphExecutor``.

Covers:

1. ``handles_tools_internally()`` returns ``True``.
2. ``run_turn()`` yields a ``TurnComplete`` event when the compiled graph
   returns a normal ainvoke result.
3. The compiled graph is cached — ``compile_to_langgraph`` is called only
   ONCE even when ``run_turn`` is called twice with the same model.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from langchain_core.messages import AIMessage  # noqa: E402

from apx_agent import LlmAgent  # noqa: E402
from apx_agent._executor import TurnComplete  # noqa: E402
from apx_agent._langgraph_executor import LangGraphExecutor  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockCompiledGraph:
    """Fake compiled LangGraph graph used in tests.

    Implements the async API expected by :class:`LangGraphExecutor`:
    - ``ainvoke`` returns a dict with a ``"messages"`` key.
    - ``astream`` is intentionally absent so the executor falls back to
      ``ainvoke`` — this mirrors the MagicMock-only-has-ainvoke pattern
      that the existing ``test_compile_run.py`` tests rely on.
    """

    def __init__(self, final_text: str = "hi") -> None:
        """Create a graph that returns *final_text* as the assistant reply.

        :param final_text: The text the fake assistant should return.
        """
        self._final_text = final_text
        self.ainvoke_call_count = 0

    async def ainvoke(self, state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Return a messages dict containing one AIMessage with *final_text*.

        :param state: The LangGraph input state dict; ``state["messages"]``
            should be a list of BaseMessage objects.
        :param kwargs: Ignored additional keyword arguments.
        :returns: A dict with ``"messages"`` key containing the input messages
            plus one new ``AIMessage(content=self._final_text)``.
        """
        self.ainvoke_call_count += 1
        existing = list(state.get("messages", []))
        return {"messages": existing + [AIMessage(content=self._final_text)]}


def _make_fake_graph(final_text: str = "hi") -> MockCompiledGraph:
    """Construct a :class:`MockCompiledGraph` that returns *final_text*.

    :param final_text: The assistant text the fake graph should return.
    :returns: A configured :class:`MockCompiledGraph` instance.
    """
    return MockCompiledGraph(final_text=final_text)


def _runtime_tool(query: str) -> str:
    """Runtime tool."""
    return query


# ---------------------------------------------------------------------------
# handles_tools_internally
# ---------------------------------------------------------------------------


class TestHandlesToolsInternally:
    def test_handles_tools_internally(self) -> None:
        """LangGraphExecutor.handles_tools_internally() returns True."""
        agent = LlmAgent(tools=[])
        executor = LangGraphExecutor(agent, ws=None, model="any-model")
        assert executor.handles_tools_internally() is True


# ---------------------------------------------------------------------------
# run_turn yields TurnComplete
# ---------------------------------------------------------------------------


class TestRunTurnYieldsTurnComplete:
    @pytest.mark.asyncio
    async def test_run_turn_yields_turn_complete(self) -> None:
        """run_turn yields TurnComplete with the final assistant text.

        Mocks compile_to_langgraph at the canonical patch target
        (``apx_agent._compile.compile_to_langgraph``) since the executor uses
        a lazy import so the patch is visible at call time.
        """
        agent = LlmAgent(tools=[])
        fake_graph = _make_fake_graph("hello from mock")

        with patch(
            "apx_agent._compile.compile_to_langgraph",
            return_value=fake_graph,
        ):
            executor = LangGraphExecutor(agent, ws=None, model="test-model")
            events = [
                e
                async for e in executor.run_turn(
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    system_prompt="",
                    config=None,
                )
            ]

        # At least one TurnComplete must be present.
        turn_completes = [e for e in events if isinstance(e, TurnComplete)]
        assert len(turn_completes) >= 1, f"No TurnComplete in events: {events}"
        tc = turn_completes[-1]
        assert tc.response == "hello from mock"

    @pytest.mark.asyncio
    async def test_run_turn_falls_back_to_ainvoke_when_astream_absent(self) -> None:
        """run_turn falls back to ainvoke when astream is not available.

        MockCompiledGraph has no astream, so the executor should catch the
        AttributeError/TypeError and fall back gracefully to ainvoke.
        """
        agent = LlmAgent(tools=[])
        fake_graph = _make_fake_graph("fallback text")

        with patch(
            "apx_agent._compile.compile_to_langgraph",
            return_value=fake_graph,
        ):
            executor = LangGraphExecutor(agent, ws=None, model="test-model")
            events = [
                e
                async for e in executor.run_turn(
                    messages=[],
                    tools=[],
                    system_prompt="",
                    config=None,
                )
            ]

        turn_completes = [e for e in events if isinstance(e, TurnComplete)]
        assert len(turn_completes) == 1
        assert turn_completes[0].response == "fallback text"


# ---------------------------------------------------------------------------
# Compiled graph caching
# ---------------------------------------------------------------------------


class TestCompiledGraphCached:
    @pytest.mark.asyncio
    async def test_compiled_graph_cached(self) -> None:
        """compile_to_langgraph is called only once for the same model.

        Even when run_turn is called twice with the same effective model, the
        compiled graph should be reused from the cache.
        """
        agent = LlmAgent(tools=[])
        fake_graph = _make_fake_graph("cached")
        call_count = 0

        def _compile_spy(*args: Any, **kwargs: Any) -> MockCompiledGraph:
            """Count calls to compile_to_langgraph and return the fake graph."""
            nonlocal call_count
            call_count += 1
            return fake_graph

        with patch("apx_agent._compile.compile_to_langgraph", side_effect=_compile_spy):
            executor = LangGraphExecutor(agent, ws=None, model="cached-model")

            # First turn — should trigger compilation.
            events1 = [
                e
                async for e in executor.run_turn(
                    messages=[],
                    tools=[],
                    system_prompt="",
                    config=None,
                )
            ]
            # Second turn with the same model — should use the cache.
            events2 = [
                e
                async for e in executor.run_turn(
                    messages=[],
                    tools=[],
                    system_prompt="",
                    config=None,
                )
            ]

        assert call_count == 1, (
            f"compile_to_langgraph was called {call_count} times; expected 1 (cached)"
        )
        # Both turns should have succeeded.
        for events in (events1, events2):
            assert any(isinstance(e, TurnComplete) for e in events)

    @pytest.mark.asyncio
    async def test_tool_mutation_invalidates_cache(self) -> None:
        """A runtime tool change triggers a fresh compile for the same model."""
        agent = LlmAgent(tools=[])
        seen_tool_names: list[list[str]] = []

        def _compile_spy(agent_arg: LlmAgent, **kwargs: Any) -> MockCompiledGraph:
            seen_tool_names.append([fn.__name__ for fn in agent_arg._tool_fns])
            return _make_fake_graph("ok")

        with patch("apx_agent._compile.compile_to_langgraph", side_effect=_compile_spy):
            executor = LangGraphExecutor(agent, ws=None, model="cached-model")

            await _drain(
                executor.run_turn(
                    messages=[],
                    tools=[],
                    system_prompt="",
                    config=None,
                )
            )
            agent.register_tool(_runtime_tool)
            await _drain(
                executor.run_turn(
                    messages=[],
                    tools=[],
                    system_prompt="",
                    config=None,
                )
            )

        assert seen_tool_names == [[], ["_runtime_tool"]]

    @pytest.mark.asyncio
    async def test_different_models_compile_separately(self) -> None:
        """A different model string triggers a fresh compilation.

        If run_turn is called twice with different model values, compile must
        be invoked for each distinct model.
        """
        agent = LlmAgent(tools=[])
        call_count = 0

        def _compile_spy(*args: Any, **kwargs: Any) -> MockCompiledGraph:
            """Return a fresh graph each call and record invocations."""
            nonlocal call_count
            call_count += 1
            return _make_fake_graph("ok")

        from apx_agent._executor import ExecutorConfig

        with patch("apx_agent._compile.compile_to_langgraph", side_effect=_compile_spy):
            executor = LangGraphExecutor(agent, ws=None, model="model-a")

            await _drain(
                executor.run_turn(
                    messages=[],
                    tools=[],
                    system_prompt="",
                    config=ExecutorConfig(model="model-a"),
                )
            )
            await _drain(
                executor.run_turn(
                    messages=[],
                    tools=[],
                    system_prompt="",
                    config=ExecutorConfig(model="model-b"),
                )
            )

        assert call_count == 2, (
            f"Expected 2 compile calls for 2 distinct models, got {call_count}"
        )


async def _drain(gen: Any) -> list[Any]:
    """Exhaust an async generator and return all events.

    :param gen: An async generator to drain.
    :returns: A list of all yielded values.
    """
    return [e async for e in gen]
