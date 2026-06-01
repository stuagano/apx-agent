"""Phase 0 prototype tests for E3b principal threading."""
from __future__ import annotations

from typing import Annotated, Any
from unittest.mock import MagicMock

from fastapi.params import Depends

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


class TestResolveSessionStore:
    def test_override_wins_over_config(self):
        from apx_agent._memory_wiring import resolve_session_store
        from apx_agent._models import AgentConfig, SessionBackendConfig

        explicit = MagicMock()
        cfg = AgentConfig(name="t", session=SessionBackendConfig(type="inmemory"))
        result = resolve_session_store(cfg, ws=None, override=explicit)
        assert result is explicit

    def test_inmemory_config_returns_session_store(self):
        from apx_agent._memory_wiring import resolve_session_store
        from apx_agent._session import InMemorySessionStore
        from apx_agent._models import AgentConfig, SessionBackendConfig

        cfg = AgentConfig(name="t", session=SessionBackendConfig(type="inmemory"))
        result = resolve_session_store(cfg, ws=None, override=None)
        assert result is not None
        assert isinstance(result, InMemorySessionStore)

    def test_no_session_config_returns_none(self):
        from apx_agent._memory_wiring import resolve_session_store
        from apx_agent._models import AgentConfig

        cfg = AgentConfig(name="t")
        result = resolve_session_store(cfg, ws=None, override=None)
        assert result is None

    def test_lakebase_session_with_no_ws_returns_none_with_warning(self, caplog):
        from apx_agent._memory_wiring import resolve_session_store
        from apx_agent._models import AgentConfig, SessionBackendConfig
        import logging

        cfg = AgentConfig(name="t", session=SessionBackendConfig(
            type="lakebase", instance_name="inst", database="db"
        ))
        with caplog.at_level(logging.WARNING):
            result = resolve_session_store(cfg, ws=None, override=None)
        assert result is None
        assert "ws" in caplog.text.lower() or "lakebase" in caplog.text.lower()


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
