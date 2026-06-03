"""Tests for _compile.py — apx-agent → LangGraph compilation.

These tests verify the compile shape works end-to-end with mocks (no live LLM
call). They prove:

  1. ``compile_to_langgraph`` produces a CompiledStateGraph.
  2. Plain typed tool parameters become the langchain tool's input schema.
  3. ``Dependencies.Workspace`` / ``Dependencies.Sql`` parameters are resolved
     against the per-request ws and captured in closures — the LLM never sees
     them.
  4. ``SequentialAgent`` sub-agents become graph nodes with meaningful names
     (when an ``LlmAgent(name=...)`` is provided).
  5. Closure-based user-scoped auth is preserved: tools call the captured ws,
     not a global one.

Skips if the optional ``langgraph`` extra is not installed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

# Skip the whole module unless the optional extra is installed.
pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from apx_agent import (  # noqa: E402
    Dependencies,
    LlmAgent,
    SequentialAgent,
    compile_to_langgraph,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_ws() -> MagicMock:
    """A WorkspaceClient stand-in. Records calls so tests can prove the
    compiled tool called THIS ws (not some other one)."""
    ws = MagicMock(name="fake_user_scoped_ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    return ws


@pytest.fixture(autouse=True)
def _stub_chat_databricks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``_build_chat_databricks`` so tests don't need a live endpoint
    or the langchain-databricks package."""
    from apx_agent import _compile

    monkeypatch.setattr(
        _compile,
        "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: MagicMock(
            name=f"fake_chat_model:{endpoint}"
        ),
    )


# ---------------------------------------------------------------------------
# Tool fixtures (these are what apx-agent users write)
# ---------------------------------------------------------------------------


def scan_demand(lookback_hours: int, ws: Dependencies.Workspace) -> str:
    """Scan demand clusters for the lookback window."""
    return ws.dummy_call(lookback_hours)


def validate_market(component_id: str, ws: Dependencies.Workspace) -> str:
    """Validate a signal against market news."""
    return ws.dummy_call(component_id)


def report_generator(summary: str) -> str:  # no dependencies — pure tool
    """Generate the final report from a summary."""
    return f"REPORT: {summary}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCompileLlmAgent:
    def test_compiles_to_runnable(self, fake_ws: MagicMock) -> None:
        agent = LlmAgent(
            name="scanner",
            tools=[scan_demand],
            instructions="Scan and report.",
        )
        compiled = compile_to_langgraph(
            agent, ws=fake_ws, model="databricks-claude-sonnet-4-6"
        )
        assert compiled is not None
        # create_react_agent returns a Pregel/Runnable — must be invokable.
        assert hasattr(compiled, "invoke") or hasattr(compiled, "ainvoke")

    def test_resolved_dep_is_captured_in_closure(self, fake_ws: MagicMock) -> None:
        """Tool's ``ws`` parameter must be bound to OUR fake_ws at compile time."""
        from apx_agent._compile import CompileContext, _make_langchain_tool

        ctx = CompileContext(ws=fake_ws, model="any")
        lc_tool = _make_langchain_tool(scan_demand, ctx)

        # Invoke the wrapped tool with only the plain param. The closure
        # supplies ws — and it MUST be our fake_ws, not anything else.
        lc_tool.invoke({"lookback_hours": 48})
        fake_ws.dummy_call.assert_called_once_with(48)

    def test_dependency_params_excluded_from_input_schema(
        self, fake_ws: MagicMock
    ) -> None:
        """The LLM-visible schema must NOT include ``ws`` (it's a dependency)."""
        from apx_agent._compile import CompileContext, _make_langchain_tool

        ctx = CompileContext(ws=fake_ws, model="any")
        lc_tool = _make_langchain_tool(scan_demand, ctx)

        schema = lc_tool.args_schema.model_json_schema()
        assert "lookback_hours" in schema["properties"]
        assert "ws" not in schema["properties"]


class TestAsyncToolInvocation:
    """Regression: async tools (sql_tool, genie_tool, uc_function_tool, ...) must
    work in the SYNC graph.invoke() path used by the Apps + ChatAgent runtimes.
    A coroutine-only StructuredTool raised "does not support sync invocation"."""

    def test_async_tool_supports_sync_invoke(self, fake_ws: MagicMock) -> None:
        from apx_agent._compile import CompileContext, _make_langchain_tool

        async def fetch_row(row_id: str) -> str:
            """Fetch a row."""
            return f"row:{row_id}"

        lc_tool = _make_langchain_tool(fetch_row, CompileContext(ws=fake_ws, model="any"))
        # The bug: this raised ToolException "does not support sync invocation".
        assert lc_tool.invoke({"row_id": "42"}) == "row:42"

    @pytest.mark.asyncio
    async def test_async_tool_supports_async_invoke(self, fake_ws: MagicMock) -> None:
        from apx_agent._compile import CompileContext, _make_langchain_tool

        async def fetch_row(row_id: str) -> str:
            """Fetch a row."""
            return f"row:{row_id}"

        lc_tool = _make_langchain_tool(fetch_row, CompileContext(ws=fake_ws, model="any"))
        # Sync bridge must also work even when called from within a running loop.
        assert lc_tool.invoke({"row_id": "9"}) == "row:9"
        assert await lc_tool.ainvoke({"row_id": "7"}) == "row:7"


class TestCompileSequentialAgent:
    def test_pipeline_becomes_state_graph(self, fake_ws: MagicMock) -> None:
        pipeline = SequentialAgent(
            agents=[
                LlmAgent(name="scanner", tools=[scan_demand], instructions="..."),
                LlmAgent(name="validator", tools=[validate_market], instructions="..."),
                LlmAgent(name="reporter", tools=[report_generator], instructions="..."),
            ],
        )
        compiled = compile_to_langgraph(
            pipeline, ws=fake_ws, model="databricks-claude-sonnet-4-6"
        )

        # The compiled graph exposes its nodes via .get_graph().nodes.
        graph_repr = compiled.get_graph()
        node_names = set(graph_repr.nodes.keys())
        # __start__ and __end__ are LangGraph internals; our three are present.
        assert {"scanner", "validator", "reporter"}.issubset(node_names)

    def test_unnamed_sub_agents_get_positional_names(
        self, fake_ws: MagicMock
    ) -> None:
        pipeline = SequentialAgent(
            agents=[
                LlmAgent(tools=[scan_demand], instructions="..."),
                LlmAgent(tools=[validate_market], instructions="..."),
            ],
        )
        compiled = compile_to_langgraph(
            pipeline, ws=fake_ws, model="databricks-claude-sonnet-4-6"
        )
        node_names = set(compiled.get_graph().nodes.keys())
        assert {"step_0", "step_1"}.issubset(node_names)


class TestUnsupportedAgentTypeRaises:
    def test_unknown_subclass_raises_helpful_error(self, fake_ws: MagicMock) -> None:
        """An unsupported BaseAgent subclass fails with a clear message."""
        from apx_agent._agents import BaseAgent

        class _MysteryAgent(BaseAgent):
            pass

        with pytest.raises(NotImplementedError, match="_MysteryAgent"):
            compile_to_langgraph(_MysteryAgent(), ws=fake_ws, model="any")


class TestDependenciesProgress:
    def test_dependencies_progress_resolves_to_emitter(self) -> None:
        """``Dependencies.Progress`` exists and the resolver wires
        ``_get_progress`` to the ``emit_progress`` span-event emitter."""
        from apx_agent import Dependencies
        from apx_agent._compile import CompileContext, _make_dep_resolvers
        from apx_agent._defaults import _get_progress
        from apx_agent._mlflow_tracing import emit_progress

        assert Dependencies.Progress is not None

        ctx = CompileContext(ws=None, model="m", headers=None)  # type: ignore[arg-type]
        resolvers = _make_dep_resolvers(ctx)
        assert resolvers[_get_progress] is emit_progress
