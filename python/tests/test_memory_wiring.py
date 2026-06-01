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
