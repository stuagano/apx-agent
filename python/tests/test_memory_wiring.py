"""Phase 0 prototype tests for E3b principal threading."""
from __future__ import annotations

from typing import Annotated, Any
from unittest.mock import MagicMock, patch

from fastapi.params import Depends

import pytest

from apx_agent._defaults import _get_principal
from apx_agent._tool import tool


def _make_headers(user_id: str | None = None) -> Any:
    h = MagicMock()
    h.user_id = user_id
    h.token = None
    return h


def _make_ctx(user_id: str | None = None) -> Any:
    from apx_agent._compile import CompileContext
    ws = MagicMock()
    ctx = CompileContext(ws=ws, model="test", headers=_make_headers(user_id))
    return ctx


class TestGetPrincipal:
    def test_get_principal_dep_exported(self):
        from apx_agent._defaults import _get_principal, PrincipalDependency
        assert callable(_get_principal)
        import typing
        args = typing.get_args(PrincipalDependency)
        assert any(hasattr(a, "dependency") and a.dependency is _get_principal for a in args)

    def test_dependencies_principal_alias(self):
        from apx_agent._defaults import PrincipalDependency
        from apx_agent import Dependencies
        assert Dependencies.Principal is PrincipalDependency

    def test_make_dep_resolvers_includes_get_principal(self):
        from apx_agent._compile import _make_dep_resolvers
        from apx_agent._defaults import _get_principal
        ctx = _make_ctx(user_id="alice@example.com")
        resolvers = _make_dep_resolvers(ctx)
        assert _get_principal in resolvers
        assert resolvers[_get_principal] == "alice@example.com"

    def test_make_dep_resolvers_none_when_no_headers(self):
        from apx_agent._compile import _make_dep_resolvers
        from apx_agent._defaults import _get_principal
        ctx = _make_ctx(user_id=None)
        ctx.headers = None
        resolvers = _make_dep_resolvers(ctx)
        assert resolvers[_get_principal] is None


class TestPrincipalClosure:
    def _make_principal_tool(self):
        @tool
        def probe(query: str, principal: Annotated[str | None, Depends(_get_principal)]) -> str:
            """Return the principal seen inside the closure."""
            return principal or "NONE"
        return probe

    def test_sync_tool_sees_correct_principal(self):
        from apx_agent._compile import _make_langchain_tool
        probe = self._make_principal_tool()
        lt = _make_langchain_tool(probe, _make_ctx(user_id="alice"))
        assert lt.run({"query": "x"}) == "alice"

    def test_sync_tool_sees_different_principal_per_compile(self):
        from apx_agent._compile import _make_langchain_tool
        probe = self._make_principal_tool()
        lt_alice = _make_langchain_tool(probe, _make_ctx(user_id="alice"))
        lt_bob = _make_langchain_tool(probe, _make_ctx(user_id="bob"))
        assert lt_alice.run({"query": "q"}) == "alice"
        assert lt_bob.run({"query": "q"}) == "bob"

    def test_sync_tool_none_principal_when_header_absent(self):
        from apx_agent._compile import _make_langchain_tool
        probe = self._make_principal_tool()
        lt = _make_langchain_tool(probe, _make_ctx(user_id=None))
        assert lt.run({"query": "q"}) == "NONE"

    def test_async_tool_sees_correct_principal_via_thread_hop(self):
        """CRITICAL gate test: async tool compiled with carol — the
        ThreadPoolExecutor path must NOT lose the principal."""
        from apx_agent._compile import _make_langchain_tool

        @tool
        async def async_probe(query: str, principal: Annotated[str | None, Depends(_get_principal)]) -> str:
            """Async — wrapped via ThreadPoolExecutor."""
            return principal or "NONE"

        lt = _make_langchain_tool(async_probe, _make_ctx(user_id="carol"))
        result = lt.run({"query": "q"})
        assert result == "carol", (
            f"Principal lost across async→sync thread hop: got {result!r}. "
            "Dependencies.Principal mechanism is broken — GATE FAILS."
        )


# ---------------------------------------------------------------------------
# Task 0.3 — Cross-principal isolation (MANDATORY gate)
# ---------------------------------------------------------------------------
from apx_agent._memory import InMemoryMemoryStore, RecallOptions
from apx_agent._memory_tools import make_memory_tools, NO_PRINCIPAL


class TestCrossPrincipalIsolation:
    def _find_tool(self, tools: list, name: str):
        for t in tools:
            if getattr(t, "__name__", None) == name or getattr(t, "name", None) == name:
                return t
        raise KeyError(f"Tool {name!r} not found")

    def test_principal_a_cannot_recall_bs_memory(self):
        store = InMemoryMemoryStore()
        tools_a = make_memory_tools(store=store, principal_id_resolver=lambda: "alice")
        tools_b = make_memory_tools(store=store, principal_id_resolver=lambda: "bob")
        remember_a = self._find_tool(tools_a, "remember")
        recall_b = self._find_tool(tools_b, "recall")
        remember_a(content="alice secret")
        result = recall_b(query="alice secret")
        assert "alice secret" not in result

    def test_principal_b_cannot_recall_as_memory(self):
        store = InMemoryMemoryStore()
        tools_a = make_memory_tools(store=store, principal_id_resolver=lambda: "alice")
        tools_b = make_memory_tools(store=store, principal_id_resolver=lambda: "bob")
        remember_b = self._find_tool(tools_b, "remember")
        recall_a = self._find_tool(tools_a, "recall")
        remember_b(content="bob secret")
        result = recall_a(query="bob secret")
        assert "bob secret" not in result

    def test_principal_can_recall_own_memory(self):
        store = InMemoryMemoryStore()
        tools = make_memory_tools(store=store, principal_id_resolver=lambda: "alice")
        remember = self._find_tool(tools, "remember")
        recall = self._find_tool(tools, "recall")
        remember(content="alice own")
        result = recall(query="alice own")
        assert "alice own" in result

    def test_no_principal_returns_no_principal_sentinel(self):
        store = InMemoryMemoryStore()
        tools_a = make_memory_tools(store=store, principal_id_resolver=lambda: "alice")
        self._find_tool(tools_a, "remember")(content="alice data")
        tools_none = make_memory_tools(store=store)
        recall_none = self._find_tool(tools_none, "recall")
        remember_none = self._find_tool(tools_none, "remember")
        assert NO_PRINCIPAL in recall_none(query="alice data")
        assert NO_PRINCIPAL in remember_none(content="anonymous write")

    def test_no_principal_write_does_not_pollute_alice_namespace(self):
        store = InMemoryMemoryStore()
        tools_none = make_memory_tools(store=store)
        self._find_tool(tools_none, "remember")(content="leaked write")
        tools_a = make_memory_tools(store=store, principal_id_resolver=lambda: "alice")
        result = self._find_tool(tools_a, "recall")(query="leaked write")
        assert "leaked write" not in result

    def test_dep_principal_path_isolation(self):
        """Config path: tools with a Depends(_get_principal) dep param isolate
        correctly end-to-end through the compile machinery — A's memory invisible to B."""
        from apx_agent._compile import _make_langchain_tool
        # _get_principal, Annotated, Depends, tool are MODULE-LEVEL imports (see top of file)

        store = InMemoryMemoryStore()

        @tool
        def dep_remember(content: str, principal: Annotated[str | None, Depends(_get_principal)]) -> str:
            """Remember with dep-resolved principal."""
            if not principal:
                return NO_PRINCIPAL
            store.add({"principal_id": principal, "content": content})
            return "stored"

        @tool
        def dep_recall(query: str, principal: Annotated[str | None, Depends(_get_principal)]) -> str:
            """Recall with dep-resolved principal."""
            if not principal:
                return NO_PRINCIPAL
            results = store.recall(RecallOptions(principal_id=principal, query=query))
            return " | ".join(r.memory.content for r in results) or "none"

        lt_remember_alice = _make_langchain_tool(dep_remember, _make_ctx("alice"))
        lt_recall_alice = _make_langchain_tool(dep_recall, _make_ctx("alice"))
        lt_recall_bob = _make_langchain_tool(dep_recall, _make_ctx("bob"))

        lt_remember_alice.run({"content": "alice dep memory"})
        alice_result = lt_recall_alice.run({"query": "dep memory"})
        bob_result = lt_recall_bob.run({"query": "dep memory"})
        assert "alice dep memory" in alice_result, (
            f"Alice's own dep-path memory invisible to her: {alice_result!r}. "
            "GATE FAILS — dep-principal mechanism broken."
        )
        assert "alice dep memory" not in bob_result, (
            f"Alice's dep-path memory visible to Bob: {bob_result!r}. "
            "GATE FAILS — dep-principal isolation broken."
        )


# ---------------------------------------------------------------------------
# Task 1.4 — attach_declared_memory + resolve_session_store
# ---------------------------------------------------------------------------
import textwrap  # noqa: E402
import pytest  # noqa: E402

from unittest.mock import patch  # noqa: E402

from apx_agent import Agent  # noqa: E402
from apx_agent._models import AgentConfig, MemoryBackendConfig, SessionBackendConfig  # noqa: E402


class TestAttachDeclaredMemory:
    def _minimal_config(self, **kw) -> AgentConfig:
        return AgentConfig(name="t", memory=MemoryBackendConfig(**kw))

    def test_inmemory_type_attaches_recall_remember_forget(self):
        from apx_agent._memory_wiring import attach_declared_memory

        agent = Agent(tools=[])
        cfg = self._minimal_config(type="inmemory")
        attach_declared_memory(agent, cfg, ws=None)
        names = {fn.__name__ for fn in agent._tool_fns}
        assert "recall" in names
        assert "remember" in names
        assert "forget" in names

    def test_tools_appear_in_collect_tools_after_attach(self):
        from apx_agent._memory_wiring import attach_declared_memory

        agent = Agent(tools=[])
        cfg = self._minimal_config(type="inmemory")
        attach_declared_memory(agent, cfg, ws=None)
        tool_names = {t.name for t in agent.collect_tools()}
        assert "recall" in tool_names

    def test_idempotent_double_attach(self):
        from apx_agent._memory_wiring import attach_declared_memory

        agent = Agent(tools=[])
        cfg = self._minimal_config(type="inmemory")
        attach_declared_memory(agent, cfg, ws=None)
        attach_declared_memory(agent, cfg, ws=None)
        # Must not double-register: only one recall
        names = [fn.__name__ for fn in agent._tool_fns if fn.__name__ == "recall"]
        assert len(names) == 1

    def test_code_wired_recall_wins_over_declared(self, caplog):
        from apx_agent._memory_wiring import attach_declared_memory
        from apx_agent._memory_tools import make_memory_tools

        store = InMemoryMemoryStore()
        code_tools = make_memory_tools(store=store, principal_id_resolver=lambda: "alice")
        agent = Agent(tools=code_tools)

        import logging
        with caplog.at_level(logging.WARNING):
            cfg = self._minimal_config(type="inmemory")
            attach_declared_memory(agent, cfg, ws=None)

        # Code-wired recall kept; declared recall skipped; a warning was issued.
        names = [fn.__name__ for fn in agent._tool_fns if fn.__name__ == "recall"]
        assert len(names) == 1
        assert "recall" in caplog.text or "keeping" in caplog.text.lower()

    def test_tool_prefix_applied(self):
        from apx_agent._memory_wiring import attach_declared_memory

        agent = Agent(tools=[])
        cfg = self._minimal_config(type="inmemory", tool_prefix="mem_")
        attach_declared_memory(agent, cfg, ws=None)
        names = {fn.__name__ for fn in agent._tool_fns}
        assert "mem_recall" in names
        assert "recall" not in names

    def test_include_subset(self):
        from apx_agent._memory_wiring import attach_declared_memory

        agent = Agent(tools=[])
        cfg = self._minimal_config(type="inmemory", include=["recall"])
        attach_declared_memory(agent, cfg, ws=None)
        names = {fn.__name__ for fn in agent._tool_fns}
        assert "recall" in names
        assert "remember" not in names

    def test_no_memory_config_is_noop(self):
        from apx_agent._memory_wiring import attach_declared_memory

        agent = Agent(tools=[])
        cfg = AgentConfig(name="t")  # no memory
        attach_declared_memory(agent, cfg, ws=None)
        assert agent._tool_fns == []

    def test_lakebase_type_requires_ws_or_warns(self, caplog):
        """lakebase with ws=None logs a warning and skips (no crash)."""
        from apx_agent._memory_wiring import attach_declared_memory
        import logging

        agent = Agent(tools=[])
        cfg = self._minimal_config(
            type="lakebase",
            instance_name="inst",
            database="db",
            embedding_model="bge",
            embedding_dim=4,
        )
        with caplog.at_level(logging.WARNING):
            attach_declared_memory(agent, cfg, ws=None)

        # With ws=None, lakebase must skip and warn (not crash).
        assert agent._tool_fns == []
        assert "ws" in caplog.text.lower() or "lakebase" in caplog.text.lower() or "skip" in caplog.text.lower()

    def test_lakebase_missing_field_with_ws_warns_and_skips(self, caplog):
        """ws IS set but a required field is missing → build failed (not 'requires ws')."""
        from apx_agent._memory_wiring import attach_declared_memory
        import logging

        agent = Agent(tools=[])
        # ws IS provided, but instance_name is missing → _build_memory_store raises ValueError
        cfg = self._minimal_config(
            type="lakebase", database="db", embedding_model="bge", embedding_dim=4
        )  # no instance_name
        with caplog.at_level(logging.WARNING):
            attach_declared_memory(agent, cfg, ws=MagicMock())
        assert agent._tool_fns == []  # skipped, no crash
        assert "build failed" in caplog.text.lower() or "instance_name" in caplog.text.lower()
        # and NOT the misleading ws=None message (ws was provided):
        assert "ws=None at this point" not in caplog.text


# ---------------------------------------------------------------------------
# Task 1.6 — Spec §6 MANDATORY end-to-end isolation through the
#             config-declared finalize_agent path
# ---------------------------------------------------------------------------


class TestEndToEndIsolation:
    """Spec §6 MANDATORY isolation — through the config-declared finalize_agent path."""

    @pytest.fixture
    def agent_with_memory(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""
            [tool.apx.agent]
            name = "isolation-test"
            model = "databricks-meta-llama-3-3-70b-instruct"
            [tool.apx.agent.memory]
            type = "inmemory"
        """))
        from apx_agent import Agent
        from apx_agent._wiring import finalize_agent
        agent = Agent(tools=[])
        finalize_agent(agent, pyproject_path=str(tmp_path / "pyproject.toml"), ws=None)
        names = {fn.__name__ for fn in agent._tool_fns}
        assert "recall" in names and "remember" in names
        return agent

    def test_config_memory_isolates_alice_from_bob_end_to_end(self, agent_with_memory):
        from apx_agent._compile import _make_langchain_tool, CompileContext

        def _ctx(user_id: str):
            ws = MagicMock()
            headers = MagicMock()
            headers.user_id = user_id
            headers.token = None
            return CompileContext(ws=ws, model="m", headers=headers)

        agent = agent_with_memory
        recall_fn = next(fn for fn in agent._tool_fns if fn.__name__ == "recall")
        remember_fn = next(fn for fn in agent._tool_fns if fn.__name__ == "remember")

        lt_remember_alice = _make_langchain_tool(remember_fn, _ctx("alice"))
        lt_recall_alice = _make_langchain_tool(recall_fn, _ctx("alice"))
        lt_recall_bob = _make_langchain_tool(recall_fn, _ctx("bob"))

        lt_remember_alice.run({"content": "alice e2e memory"})
        alice_result = lt_recall_alice.run({"query": "e2e memory"})
        bob_result = lt_recall_bob.run({"query": "e2e memory"})

        assert "alice e2e memory" in alice_result, "Alice must recall her own memory end-to-end"
        assert "alice e2e memory" not in bob_result, (
            "Bob must NOT see Alice's memory — isolation breach in the config-declared "
            "_use_dep_principal path"
        )


# ---------------------------------------------------------------------------
# Task 1.8 — REAL-SEAM isolation test: custom_inputs.user_id → predict →
#             compile_to_langgraph(headers=) → _get_principal → memory
# ---------------------------------------------------------------------------
#
# This test guards the model-serving seam where identity comes from
# custom_inputs["user_id"], NOT a hand-built CompileContext(headers=MagicMock).
# It MUST fail on current code (predict omits headers= → principal=None → memory
# never stores/recalls).  After the fix it MUST pass.
#
# Strategy: spy on compile_to_langgraph and capture the kwargs predict passes.
# Return a minimal fake graph so predict runs to completion without a live LLM.
# Confirm (a) headers kwarg is present and (b) headers.user_id matches the
# custom_inputs value.  This directly covers the broken seam.

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("mlflow")


class TestPredictSeamPrincipalThreading:
    """CRITICAL regression guard: predict must thread custom_inputs.user_id as
    headers= into compile_to_langgraph so config-declared memory tools get a
    non-None principal at request time.

    Fails-before: predict passes no headers kwarg → captured headers is None.
    Passes-after: predict builds DatabricksAppsHeaders(user_id="alice") and
    passes it through.
    """

    def _make_fake_graph(self):
        from langchain_core.messages import AIMessage
        graph = MagicMock(name="fake_graph")

        def _invoke(state):
            return {"messages": [*state["messages"], AIMessage(content="ok")]}

        def _stream(state, stream_mode="updates"):
            yield {"node": {"messages": [AIMessage(content="ok")]}}

        graph.invoke.side_effect = _invoke
        graph.stream.side_effect = _stream
        return graph

    def test_predict_threads_user_id_as_headers_into_compile(self, tmp_path):
        """SEAM TEST: custom_inputs['user_id'] must reach compile_to_langgraph
        as headers=<DatabricksAppsHeaders with user_id set>.

        FAILS-BEFORE the fix (captured_headers is None → seam is broken).
        PASSES-AFTER the fix (captured_headers.user_id == 'alice').
        """
        from mlflow.types.agent import ChatAgentMessage
        from apx_agent import Agent, chat_agent_for
        from apx_agent._wiring import finalize_agent

        (tmp_path / "pyproject.toml").write_text(
            "[tool.apx.agent]\nname = 'seam-test'\nmodel = 'ep'\n"
            "[tool.apx.agent.memory]\ntype = 'inmemory'\n"
        )
        agent = Agent(tools=[])
        finalize_agent(agent, pyproject_path=str(tmp_path / "pyproject.toml"), ws=None)
        wrapped = chat_agent_for(agent, model="ep")
        fake_graph = self._make_fake_graph()

        captured: dict[str, Any] = {}

        def _spy_compile(agent_arg, *, ws, model, headers=None):
            captured["headers"] = headers
            return fake_graph

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._chat_agent.compile_to_langgraph",
            side_effect=_spy_compile,
        ):
            wrapped.predict(
                messages=[ChatAgentMessage(role="user", content="hi", id="u1")],
                custom_inputs={"user_id": "alice"},
            )

        headers = captured.get("headers")
        assert headers is not None, (
            "SEAM BROKEN: predict did not pass headers= to compile_to_langgraph. "
            "Memory tools will always get principal=None in production. "
            "Fix: build DatabricksAppsHeaders from obo and pass headers= to compile."
        )
        assert headers.user_id == "alice", (
            f"headers.user_id should be 'alice' (from custom_inputs) but got {headers.user_id!r}"
        )

    def test_predict_stream_threads_user_id_as_headers_into_compile(self, tmp_path):
        """Same seam guard for predict_stream."""
        from mlflow.types.agent import ChatAgentMessage
        from apx_agent import Agent, chat_agent_for
        from apx_agent._wiring import finalize_agent

        (tmp_path / "pyproject.toml").write_text(
            "[tool.apx.agent]\nname = 'seam-test-stream'\nmodel = 'ep'\n"
            "[tool.apx.agent.memory]\ntype = 'inmemory'\n"
        )
        agent = Agent(tools=[])
        finalize_agent(agent, pyproject_path=str(tmp_path / "pyproject.toml"), ws=None)
        wrapped = chat_agent_for(agent, model="ep")
        fake_graph = self._make_fake_graph()

        captured: dict[str, Any] = {}

        def _spy_compile(agent_arg, *, ws, model, headers=None):
            captured["headers"] = headers
            return fake_graph

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._chat_agent.compile_to_langgraph",
            side_effect=_spy_compile,
        ):
            list(wrapped.predict_stream(
                messages=[ChatAgentMessage(role="user", content="hi", id="u1")],
                custom_inputs={"user_id": "alice"},
            ))

        headers = captured.get("headers")
        assert headers is not None, (
            "SEAM BROKEN (stream): predict_stream did not pass headers= to compile_to_langgraph. "
            "Fix: mirror the predict fix in predict_stream."
        )
        assert headers.user_id == "alice", (
            f"predict_stream: headers.user_id should be 'alice' but got {headers.user_id!r}"
        )

    def test_predict_headers_none_when_no_user_id(self, tmp_path):
        """When custom_inputs has no user_id, headers= must be None (not an all-None
        DatabricksAppsHeaders that would change existing tools' None check)."""
        from mlflow.types.agent import ChatAgentMessage
        from apx_agent import Agent, chat_agent_for
        from apx_agent._wiring import finalize_agent

        (tmp_path / "pyproject.toml").write_text(
            "[tool.apx.agent]\nname = 'no-uid'\nmodel = 'ep'\n"
        )
        agent = Agent(tools=[])
        finalize_agent(agent, pyproject_path=str(tmp_path / "pyproject.toml"), ws=None)
        wrapped = chat_agent_for(agent, model="ep")
        fake_graph = self._make_fake_graph()

        captured: dict[str, Any] = {}

        def _spy_compile(agent_arg, *, ws, model, headers=None):
            captured["headers"] = headers
            return fake_graph

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._chat_agent.compile_to_langgraph",
            side_effect=_spy_compile,
        ):
            wrapped.predict(
                messages=[ChatAgentMessage(role="user", content="hi", id="u1")],
                custom_inputs=None,
            )

        # With no user_id, headers should be None (principal stays None = NO_PRINCIPAL)
        assert captured.get("headers") is None, (
            "No user_id in custom_inputs → headers must be None to preserve "
            "the existing null-principal behavior"
        )


class TestAgentCarriedConfig:
    def _agent(self):
        # Minimal leaf agent with the registration hooks attach_declared_memory needs.
        from apx_agent import Agent
        return Agent(instructions="x", tools=[])

    def test_attach_uses_agent_memory_config_when_no_block(self):
        from apx_agent._memory_wiring import attach_declared_memory
        from apx_agent._models import AgentConfig, MemoryBackendConfig
        agent = self._agent()
        agent.memory_config = MemoryBackendConfig(type="inmemory")  # carried
        cfg = AgentConfig(name="c", description="d")                # no memory block
        attach_declared_memory(agent, cfg, ws=None)
        names = {getattr(fn, "__name__", "") for fn in getattr(agent, "_tool_fns", [])}
        assert any("recall" in n or "remember" in n for n in names)

    def test_explicit_block_overrides_agent_config(self):
        from apx_agent._memory_wiring import attach_declared_memory
        from apx_agent._models import AgentConfig, MemoryBackendConfig
        agent = self._agent()
        agent.memory_config = MemoryBackendConfig(type="delta")     # carried (would need ws)
        cfg = AgentConfig(name="c", description="d",
                          memory=MemoryBackendConfig(type="inmemory"))  # explicit wins
        attach_declared_memory(agent, cfg, ws=None)
        names = {getattr(fn, "__name__", "") for fn in getattr(agent, "_tool_fns", [])}
        assert any("recall" in n or "remember" in n for n in names)  # inmemory built (no ws needed)


class TestLocalDevPrincipal:
    """Local ``apx-agent run`` has no OBO ``X-Forwarded-User`` header, so memory tools
    would have no principal and recall/remember would no-op. Resolve a default
    principal from the Databricks CLI profile identity (``ws.current_user``) when
    NOT in a deployed App, so memory works in the dev loop. Deployed: returns
    None (the per-request OBO principal wins)."""

    def test_resolves_profile_user_locally(self, monkeypatch):
        import apx_agent._obo as obo, apx_agent._memory_wiring as mw
        from types import SimpleNamespace
        monkeypatch.setattr(obo, "_in_databricks_app", lambda: False)
        ws = SimpleNamespace(current_user=SimpleNamespace(
            me=lambda: SimpleNamespace(user_name="me@corp.com")))
        assert mw._resolve_default_principal(ws) == "me@corp.com"

    def test_none_in_databricks_app(self, monkeypatch):
        import apx_agent._obo as obo, apx_agent._memory_wiring as mw
        from types import SimpleNamespace
        monkeypatch.setattr(obo, "_in_databricks_app", lambda: True)
        ws = SimpleNamespace(current_user=SimpleNamespace(
            me=lambda: SimpleNamespace(user_name="sp")))
        assert mw._resolve_default_principal(ws) is None

    def test_none_without_ws(self, monkeypatch):
        import apx_agent._obo as obo, apx_agent._memory_wiring as mw
        monkeypatch.setattr(obo, "_in_databricks_app", lambda: False)
        assert mw._resolve_default_principal(None) is None

    def test_attach_passes_default_principal(self, monkeypatch):
        import apx_agent._obo as obo, apx_agent._memory_wiring as mw
        import apx_agent._memory_tools as mt
        from apx_agent import Agent
        from apx_agent._models import AgentConfig, MemoryBackendConfig
        from types import SimpleNamespace
        monkeypatch.setattr(obo, "_in_databricks_app", lambda: False)
        captured: dict = {}
        monkeypatch.setattr(mt, "make_memory_tools",
                            lambda **kw: captured.update(kw) or [])
        agent = Agent(instructions="x", tools=[])
        cfg = AgentConfig(name="c", description="d",
                          memory=MemoryBackendConfig(type="inmemory"))
        ws = SimpleNamespace(current_user=SimpleNamespace(
            me=lambda: SimpleNamespace(user_name="me@corp.com")))
        mw.attach_declared_memory(agent, cfg, ws=ws)
        assert captured.get("default_principal_id") == "me@corp.com"
