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
