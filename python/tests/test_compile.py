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
from fastapi import FastAPI

# Skip the whole module unless the optional extra is installed.
pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from apx_agent import (  # noqa: E402
    Dependencies,
    AgentConfig,
    LlmAgent,
    SequentialAgent,
    compile_to_langgraph,
    setup_agent,
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
    def test_compile_threads_distinct_service_and_user_clients(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apx_agent import _compile

        service_ws = MagicMock(name="service_ws")
        user_ws = MagicMock(name="user_ws")
        monkeypatch.setattr(_compile, "_compile_any", lambda _agent, ctx: ctx)

        ctx = compile_to_langgraph(
            LlmAgent(tools=[]),
            ws=user_ws,
            service_ws=service_ws,
            model="any",
        )

        assert ctx.service_ws is service_ws
        assert ctx.user_ws is user_ws

    def test_dependency_resolution_preserves_service_and_user_clients(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apx_agent._compile import CompileContext, _resolve_deps_for_fn

        service_ws = MagicMock(name="service_ws")
        user_ws = MagicMock(name="user_ws")
        run_sql = MagicMock(return_value=[{"value": 1}])
        monkeypatch.setattr("apx_agent._sql.run_sql", run_sql)
        ctx = CompileContext(service_ws=service_ws, user_ws=user_ws, model="any")

        def service_lookup(ws: Dependencies.Client) -> Any:
            return ws

        def user_lookup(user_client: Dependencies.UserClient) -> Any:
            return user_client

        def workspace_lookup(ws: Dependencies.Workspace) -> Any:
            return ws

        def sql_lookup(sql: Dependencies.Sql) -> Any:
            return sql

        assert _resolve_deps_for_fn(service_lookup, ctx) == {"ws": service_ws}
        assert _resolve_deps_for_fn(user_lookup, ctx) == {"user_client": user_ws}
        assert _resolve_deps_for_fn(workspace_lookup, ctx) == {"ws": user_ws}
        sql = _resolve_deps_for_fn(sql_lookup, ctx)["sql"]
        assert sql("SELECT 1") == [{"value": 1}]
        run_sql.assert_called_once_with(user_ws, "SELECT 1")

    @pytest.mark.parametrize(
        "dependency",
        [Dependencies.UserClient, Dependencies.Workspace, Dependencies.Sql],
    )
    def test_missing_user_client_fails_only_user_tools(self, dependency: Any) -> None:
        from apx_agent._compile import CompileContext, _resolve_deps_for_fn

        service_ws = MagicMock(name="service_ws")
        ctx = CompileContext(service_ws=service_ws, user_ws=None, model="any")

        def service_lookup(ws: Dependencies.Client) -> Any:
            return ws

        def pure_lookup() -> str:
            return "ok"

        def user_lookup(value: Any) -> Any:
            return value

        user_lookup.__annotations__["value"] = dependency

        assert _resolve_deps_for_fn(service_lookup, ctx) == {"ws": service_ws}
        assert _resolve_deps_for_fn(pure_lookup, ctx) == {}
        with pytest.raises(ValueError, match="user_lookup.*user WorkspaceClient"):
            _resolve_deps_for_fn(user_lookup, ctx)

    def test_missing_service_client_fails_only_service_tools(self) -> None:
        from apx_agent._compile import CompileContext, _resolve_deps_for_fn

        user_ws = MagicMock(name="user_ws")
        ctx = CompileContext(service_ws=None, user_ws=user_ws, model="any")

        def service_lookup(ws: Dependencies.Client) -> Any:
            return ws

        def user_lookup(ws: Dependencies.UserClient) -> Any:
            return ws

        def pure_lookup() -> str:
            return "ok"

        assert _resolve_deps_for_fn(user_lookup, ctx) == {"ws": user_ws}
        assert _resolve_deps_for_fn(pure_lookup, ctx) == {}
        with pytest.raises(ValueError, match="service_lookup.*service WorkspaceClient"):
            _resolve_deps_for_fn(service_lookup, ctx)

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

    @pytest.mark.asyncio
    async def test_builtin_flow_graph_tool_does_not_enter_compiled_tools(
        self, fake_ws: MagicMock
    ) -> None:
        app = FastAPI()
        agent = LlmAgent(tools=[], instructions="Help.")
        await setup_agent(app, agent, AgentConfig(name="graph-agent"))

        compiled = compile_to_langgraph(agent, ws=fake_ws, model="any")

        assert compiled is not None
        assert agent._tool_fns == []

    def test_resolved_dep_is_captured_in_closure(self, fake_ws: MagicMock) -> None:
        """Tool's ``ws`` parameter must be bound to OUR fake_ws at compile time."""
        from apx_agent._compile import CompileContext, _make_langchain_tool

        ctx = CompileContext(service_ws=None, user_ws=fake_ws, model="any")
        lc_tool = _make_langchain_tool(scan_demand, ctx)

        # Invoke the wrapped tool with only the plain param. The closure
        # supplies ws — and it MUST be our fake_ws, not anything else.
        lc_tool.invoke({"lookback_hours": 48})
        fake_ws.dummy_call.assert_called_once_with(48)

    def test_dependency_resolution_uses_inspection_callables(
        self, fake_ws: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apx_agent._compile import CompileContext, _resolve_deps_for_fn
        from apx_agent._defaults import _get_workspace_client

        monkeypatch.setattr(
            "apx_agent._compile._tool_dependency_callables",
            lambda _fn: {"ws": _get_workspace_client},
        )

        ctx = CompileContext(service_ws=fake_ws, user_ws=None, model="any")
        assert _resolve_deps_for_fn(scan_demand, ctx) == {"ws": fake_ws}

    def test_dependency_params_excluded_from_input_schema(
        self, fake_ws: MagicMock
    ) -> None:
        """The LLM-visible schema must NOT include ``ws`` (it's a dependency)."""
        from apx_agent._compile import CompileContext, _make_langchain_tool

        ctx = CompileContext(service_ws=None, user_ws=fake_ws, model="any")
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

        lc_tool = _make_langchain_tool(
            fetch_row, CompileContext(service_ws=None, user_ws=None, model="any")
        )
        # The bug: this raised ToolException "does not support sync invocation".
        assert lc_tool.invoke({"row_id": "42"}) == "row:42"

    @pytest.mark.asyncio
    async def test_async_tool_supports_async_invoke(self, fake_ws: MagicMock) -> None:
        from apx_agent._compile import CompileContext, _make_langchain_tool

        async def fetch_row(row_id: str) -> str:
            """Fetch a row."""
            return f"row:{row_id}"

        lc_tool = _make_langchain_tool(
            fetch_row, CompileContext(service_ws=None, user_ws=None, model="any")
        )
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

        ctx = CompileContext(
            service_ws=None,
            user_ws=None,
            model="m",
            headers=None,
        )
        resolvers = _make_dep_resolvers(ctx)
        assert resolvers[_get_progress] is emit_progress


class TestGovernanceExceptionMiddleware:
    """The middleware that keeps the agent loop alive on governance rejects.

    Without it, a PermissionError from a before_tool guard (Watchdog
    reject, PolicyGate DENY, ApprovalRequired) or a ToolCancelled kills
    the whole turn — the user sees a dead stream instead of the agent
    explaining the rejection and offering an alternative. Verified live
    2026-06-11; these tests pin the conversion behavior.
    """

    def _invoke(self, exc_or_result: Any) -> Any:
        """Run the middleware's wrap_tool_call with a scripted handler."""
        from apx_agent._compile import _governance_exception_middleware

        mw = _governance_exception_middleware()

        class _Request:
            tool_call = {"id": "call-123"}

        def handler(request: Any) -> Any:
            if isinstance(exc_or_result, BaseException):
                raise exc_or_result
            return exc_or_result

        return mw.wrap_tool_call(_Request(), handler)

    def test_permission_error_becomes_error_tool_message(self) -> None:
        from langchain_core.messages import ToolMessage

        result = self._invoke(PermissionError("PII export blocked (W-DATA-7)"))
        # The reject must surface as an error ToolMessage tied to the call —
        # NOT propagate (which would kill the turn before the LLM can react).
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert result.tool_call_id == "call-123"
        # The reason must reach the LLM verbatim so it can explain why and
        # offer an alternative.
        assert "PII export blocked (W-DATA-7)" in result.content

    def test_tool_cancelled_becomes_error_tool_message(self) -> None:
        from langchain_core.messages import ToolMessage

        from apx_agent import ToolCancelled

        result = self._invoke(ToolCancelled("slow_scan", "timed out after 10s"))
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "timed out after 10s" in result.content

    def test_approval_required_becomes_error_tool_message(self) -> None:
        """ApprovalRequired subclasses PermissionError — same conversion."""
        from langchain_core.messages import ToolMessage

        from apx_agent import ApprovalRequired, ApprovalStore

        approval = ApprovalStore().request("send_email", {"to": "b@x.com"})
        result = self._invoke(ApprovalRequired(approval))
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        # The approval ID must reach the LLM so it can relay it to the user.
        assert approval.id in result.content

    def test_other_exceptions_still_propagate(self) -> None:
        # Genuine bugs must fail loud — only governance signals convert.
        with pytest.raises(TypeError, match="real bug"):
            self._invoke(TypeError("real bug"))

    def test_successful_result_passes_through(self) -> None:
        assert self._invoke("tool output") == "tool output"


class TestZeroArgToolFmapiSchema:
    """Reality: the schema FMAPI actually receives for a zero-argument tool (#439).

    A no-parameter @tool served fine but 500'd every conversation: with
    args_schema=None langchain inferred a schema from the **kwargs wrapper —
    {"kwargs": {"additionalProperties": true, ...}} — and FMAPI rejected it
    with 400 'the "additionalProperties" keyword must be False or not
    specified'. These tests assert that exact invariant on the OpenAI-format
    payload the bind sends.
    """

    @staticmethod
    def _assert_fmapi_accepts(schema: Any) -> None:
        """Walk the schema: every additionalProperties must be absent or False."""
        if isinstance(schema, dict):
            if "additionalProperties" in schema:
                assert schema["additionalProperties"] is False, (
                    f"FMAPI rejects additionalProperties={schema['additionalProperties']!r}"
                )
            for value in schema.values():
                TestZeroArgToolFmapiSchema._assert_fmapi_accepts(value)
        elif isinstance(schema, list):
            for item in schema:
                TestZeroArgToolFmapiSchema._assert_fmapi_accepts(item)

    def test_zero_arg_tool_emits_valid_empty_object_schema(self, fake_ws: MagicMock) -> None:
        from langchain_core.utils.function_calling import convert_to_openai_tool

        from apx_agent._compile import CompileContext, _make_langchain_tool

        def secret_word() -> str:
            """Return the secret word."""
            return "swordfish"

        lc_tool = _make_langchain_tool(
            secret_word, CompileContext(service_ws=None, user_ws=None, model="any")
        )
        payload = convert_to_openai_tool(lc_tool)

        parameters = payload["function"]["parameters"]
        assert parameters["type"] == "object"
        assert parameters["properties"] == {}
        self._assert_fmapi_accepts(payload)
        # The tool must still be callable with empty args.
        assert lc_tool.invoke({}) == "swordfish"

    def test_dep_only_tool_emits_valid_empty_object_schema(self, fake_ws: MagicMock) -> None:
        """Dependency-only tools (ws injected, no LLM args) hit the same path."""
        from langchain_core.utils.function_calling import convert_to_openai_tool

        from apx_agent._compile import CompileContext, _make_langchain_tool

        def list_things(ws: Dependencies.Workspace) -> str:
            """List things."""
            return f"listed via {ws.config.host}"

        lc_tool = _make_langchain_tool(
            list_things,
            CompileContext(
                service_ws=None,
                user_ws=fake_ws,
                model="any",
            ),
        )
        payload = convert_to_openai_tool(lc_tool)

        parameters = payload["function"]["parameters"]
        assert parameters["type"] == "object"
        assert parameters["properties"] == {}
        self._assert_fmapi_accepts(payload)
        assert "fake.cloud.databricks.com" in lc_tool.invoke({})

    def test_zero_arg_async_tool_emits_valid_schema_and_invokes(self, fake_ws: MagicMock) -> None:
        from langchain_core.utils.function_calling import convert_to_openai_tool

        from apx_agent._compile import CompileContext, _make_langchain_tool

        async def ping() -> str:
            """Ping."""
            return "pong"

        lc_tool = _make_langchain_tool(
            ping, CompileContext(service_ws=None, user_ws=None, model="any")
        )
        self._assert_fmapi_accepts(convert_to_openai_tool(lc_tool))
        assert lc_tool.invoke({}) == "pong"

    def test_card_advertises_empty_object_input_schema(self) -> None:
        """The A2A card side: inputSchema is the same empty object, not null."""

        def secret_word() -> str:
            """Return the secret word."""
            return "swordfish"

        agent = LlmAgent(tools=[secret_word])
        (tool,) = agent.collect_tools()
        assert tool.input_schema == {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
