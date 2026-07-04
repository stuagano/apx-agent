"""Tests for ``_memory_tools.py`` — agent-facing tools wrapping a MemoryStore.

Mirrors ``typescript/tests/memory-tools.test.ts`` case-by-case so the
two implementations stay in lock-step.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from apx_agent import InMemoryMemoryStore, make_memory_tools
from apx_agent._memory import EmbeddingFn, MemoryFilter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _toy_embedder() -> EmbeddingFn:
    """Deterministic 26-dim embedder used in store-recall tests."""

    def _fn(texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            vec = [0.0] * 26
            for word in t.lower().split():
                if not word:
                    continue
                idx = ord(word[0]) - ord("a")
                if 0 <= idx < 26:
                    vec[idx] += 1.0
            out.append(vec)
        return out

    return _fn


def _find_tool(tools: list[Any], name: str) -> Any:
    """Look up a tool by its ``__name__`` (set by ``@tool(name=...)``)."""
    for t in tools:
        if t.__name__ == name:
            return t
    raise AssertionError(
        f"tool not found: {name} (have: {[t.__name__ for t in tools]})"
    )


# ---------------------------------------------------------------------------
# Build / include / prefix
# ---------------------------------------------------------------------------


class TestBuild:
    def test_returns_three_tools_by_default(self) -> None:
        tools = make_memory_tools(store=InMemoryMemoryStore())
        assert sorted(t.__name__ for t in tools) == ["forget", "recall", "remember"]

    def test_honors_include_single(self) -> None:
        tools = make_memory_tools(store=InMemoryMemoryStore(), include=["recall"])
        assert [t.__name__ for t in tools] == ["recall"]

    def test_honors_include_two_tool_subset(self) -> None:
        tools = make_memory_tools(
            store=InMemoryMemoryStore(),
            include=["recall", "forget"],
        )
        assert sorted(t.__name__ for t in tools) == ["forget", "recall"]

    def test_applies_tool_prefix(self) -> None:
        tools = make_memory_tools(
            store=InMemoryMemoryStore(),
            tool_prefix="memory_",
        )
        assert sorted(t.__name__ for t in tools) == [
            "memory_forget",
            "memory_recall",
            "memory_remember",
        ]

    def test_returns_apx_tool_metadata(self) -> None:
        """Every minted tool carries the @tool decorator's metadata."""
        tools = make_memory_tools(store=InMemoryMemoryStore())
        for t in tools:
            assert getattr(t, "_apx_tool", None) is not None


# ---------------------------------------------------------------------------
# Principal resolution
# ---------------------------------------------------------------------------


class TestPrincipalResolution:
    def test_uses_resolver_when_present(self) -> None:
        store = InMemoryMemoryStore()
        store.add({"principal_id": "alice", "content": "remember me"})
        tools = make_memory_tools(
            store=store,
            principal_id_resolver=lambda: "alice",
        )
        recall = _find_tool(tools, "recall")
        out = recall(query="me")
        assert "remember me" in out

    def test_falls_back_to_default_when_resolver_returns_none(self) -> None:
        store = InMemoryMemoryStore()
        store.add({"principal_id": "bob", "content": "fallback works"})
        tools = make_memory_tools(
            store=store,
            principal_id_resolver=lambda: None,
            default_principal_id="bob",
        )
        out = _find_tool(tools, "recall")(query="fallback")
        assert "fallback works" in out

    def test_returns_no_principal_string_when_neither_resolves(self) -> None:
        tools = make_memory_tools(store=InMemoryMemoryStore())
        out = _find_tool(tools, "recall")(query="x")
        assert out == "No principal_id available; cannot recall memories."

    def test_remember_degrades_gracefully_without_principal(self) -> None:
        tools = make_memory_tools(store=InMemoryMemoryStore())
        out = _find_tool(tools, "remember")(content="x")
        assert out == "No principal_id available; cannot recall memories."

    def test_resolver_called_per_invocation(self) -> None:
        """Changing resolver state between calls is reflected on next call."""
        store = InMemoryMemoryStore()
        store.add({"principal_id": "alice", "content": "alice-mem"})
        store.add({"principal_id": "bob", "content": "bob-mem"})
        state = {"current": "alice"}
        tools = make_memory_tools(
            store=store,
            principal_id_resolver=lambda: state["current"],
        )
        recall = _find_tool(tools, "recall")

        out1 = recall(query="mem")
        assert "alice-mem" in out1

        state["current"] = "bob"
        out2 = recall(query="mem")
        assert "bob-mem" in out2

        state["current"] = None  # type: ignore[assignment]
        out3 = recall(query="mem")
        assert "No principal_id available" in out3

    def test_resolver_result_takes_precedence_over_default(self) -> None:
        store = InMemoryMemoryStore()
        store.add({"principal_id": "resolved", "content": "r"})
        store.add({"principal_id": "default", "content": "d"})
        tools = make_memory_tools(
            store=store,
            principal_id_resolver=lambda: "resolved",
            default_principal_id="default",
        )
        out = _find_tool(tools, "recall")(query="x")
        assert "r" in out
        assert "d" not in out


# ---------------------------------------------------------------------------
# recall
# ---------------------------------------------------------------------------


class TestRecall:
    def test_returns_no_memories_found_when_empty(self) -> None:
        tools = make_memory_tools(
            store=InMemoryMemoryStore(),
            default_principal_id="u1",
        )
        out = _find_tool(tools, "recall")(query="x")
        assert out == "No memories found."

    def test_formats_results_as_markdown_bullet_with_score(self) -> None:
        store = InMemoryMemoryStore()
        store.add({"principal_id": "u1", "content": "first thought"})
        store.add({"principal_id": "u1", "content": "second thought"})
        tools = make_memory_tools(store=store, default_principal_id="u1")
        out = _find_tool(tools, "recall")(query="thought")
        # Every line is "- [score=X.XX] ..."
        for line in out.split("\n"):
            assert line.startswith("- [score=")
            # score is two-decimal float
            assert "] " in line
        assert "first thought" in out
        assert "second thought" in out

    def test_passes_namespace_filter(self) -> None:
        store = InMemoryMemoryStore()
        store.add(
            {"principal_id": "u1", "content": "profile fact", "namespace": "profile"}
        )
        store.add(
            {"principal_id": "u1", "content": "episodic fact", "namespace": "episodic"}
        )
        tools = make_memory_tools(store=store, default_principal_id="u1")
        out = _find_tool(tools, "recall")(query="fact", namespace="profile")
        assert "profile fact" in out
        assert "episodic fact" not in out

    def test_passes_tags_filter(self) -> None:
        store = InMemoryMemoryStore()
        store.add({"principal_id": "u1", "content": "pinned", "tags": ["pin"]})
        store.add({"principal_id": "u1", "content": "unpinned", "tags": ["casual"]})
        tools = make_memory_tools(store=store, default_principal_id="u1")
        out = _find_tool(tools, "recall")(query="x", tags=["pin"])
        assert "pinned" in out
        assert "unpinned" not in out

    def test_honors_namespace_default_when_handler_omits(self) -> None:
        store = InMemoryMemoryStore()
        store.add(
            {"principal_id": "u1", "content": "in profile", "namespace": "profile"}
        )
        store.add({"principal_id": "u1", "content": "in other", "namespace": "other"})
        tools = make_memory_tools(
            store=store,
            default_principal_id="u1",
            namespace_default="profile",
        )
        out = _find_tool(tools, "recall")(query="x")
        assert "in profile" in out
        assert "in other" not in out

    def test_k_limits_returned_results(self) -> None:
        store = InMemoryMemoryStore()
        for i in range(5):
            store.add({"principal_id": "u1", "content": f"item-{i}"})
        tools = make_memory_tools(store=store, default_principal_id="u1")
        out = _find_tool(tools, "recall")(query="x", k=2)
        assert len(out.split("\n")) == 2

    def test_recall_ranks_with_embedder_when_present(self) -> None:
        """When an embedder is wired, recall returns cosine-similarity scores."""
        store = InMemoryMemoryStore(embedding_fn=_toy_embedder())
        store.add({"principal_id": "u1", "content": "apple"})
        store.add({"principal_id": "u1", "content": "banana"})
        tools = make_memory_tools(store=store, default_principal_id="u1")
        out = _find_tool(tools, "recall")(query="apple")
        # Top result should be apple, score should be 1.00 for an exact-letter
        # match under the toy embedder.
        first_line = out.split("\n")[0]
        assert "apple" in first_line
        assert "[score=1.00]" in first_line


# ---------------------------------------------------------------------------
# remember
# ---------------------------------------------------------------------------


class TestRemember:
    def test_persists_a_memory_and_returns_saved_id(self) -> None:
        store = InMemoryMemoryStore()
        tools = make_memory_tools(store=store, default_principal_id="u1")
        out = _find_tool(tools, "remember")(content="new memory")
        assert out.startswith("Saved memory mem_")
        rows = store.list(MemoryFilter(principal_id="u1"))
        assert len(rows) == 1
        assert rows[0].content == "new memory"

    def test_passes_namespace_tags_importance_through(self) -> None:
        store = InMemoryMemoryStore()
        tools = make_memory_tools(store=store, default_principal_id="u1")
        _find_tool(tools, "remember")(
            content="fact",
            namespace="profile",
            tags=["high"],
            importance=0.95,
        )
        rows = store.list(MemoryFilter(principal_id="u1"))
        assert rows[0].namespace == "profile"
        assert rows[0].tags == ("high",)
        assert rows[0].importance == pytest.approx(0.95)

    def test_rejects_importance_outside_zero_one(self) -> None:
        tools = make_memory_tools(
            store=InMemoryMemoryStore(),
            default_principal_id="u1",
        )
        with pytest.raises(ValueError, match=r"importance must be in \[0, 1\]"):
            _find_tool(tools, "remember")(content="x", importance=1.5)

    def test_rejects_negative_importance(self) -> None:
        tools = make_memory_tools(
            store=InMemoryMemoryStore(),
            default_principal_id="u1",
        )
        with pytest.raises(ValueError, match=r"importance must be in \[0, 1\]"):
            _find_tool(tools, "remember")(content="x", importance=-0.1)

    def test_remember_uses_namespace_default(self) -> None:
        store = InMemoryMemoryStore()
        tools = make_memory_tools(
            store=store,
            default_principal_id="u1",
            namespace_default="profile",
        )
        _find_tool(tools, "remember")(content="hello")
        rows = store.list(MemoryFilter(principal_id="u1"))
        assert rows[0].namespace == "profile"


# ---------------------------------------------------------------------------
# forget
# ---------------------------------------------------------------------------


class TestForget:
    def test_deletes_a_memory_by_id_and_returns_success(self) -> None:
        store = InMemoryMemoryStore()
        mem = store.add({"principal_id": "u1", "content": "doomed"})
        tools = make_memory_tools(store=store, default_principal_id="u1")
        out = _find_tool(tools, "forget")(id=mem.id)
        assert out == f"Forgot memory {mem.id}."
        assert store.get(mem.id) is None

    def test_returns_missing_id_message_when_no_such_memory(self) -> None:
        tools = make_memory_tools(
            store=InMemoryMemoryStore(),
            default_principal_id="u1",
        )
        out = _find_tool(tools, "forget")(id="mem_xyz")
        assert out == "No memory with id mem_xyz."

    def test_forget_without_principal_deletes_nothing(self) -> None:
        """Fail-closed: with no resolvable principal, forget deletes nothing (#466)."""
        store = InMemoryMemoryStore()
        mem = store.add({"principal_id": "u1", "content": "x"})
        tools = make_memory_tools(store=store)  # no resolver, no default
        out = _find_tool(tools, "forget")(id=mem.id)
        assert out == f"No memory with id {mem.id}."
        assert store.get(mem.id) is not None  # untouched

    def test_forget_refuses_another_principals_memory(self) -> None:
        """#466: a caller cannot delete another principal's memory by id.

        The reply is identical to a genuine miss so the caller can't probe
        which ids exist in another principal's scope.
        """
        store = InMemoryMemoryStore()
        victim = store.add({"principal_id": "victim", "content": "secret"})
        tools = make_memory_tools(store=store, default_principal_id="attacker")
        out = _find_tool(tools, "forget")(id=victim.id)
        assert out == f"No memory with id {victim.id}."
        assert store.get(victim.id) is not None  # victim's memory survives


# ---------------------------------------------------------------------------
# Tool name + prefix interaction
# ---------------------------------------------------------------------------


class TestNaming:
    def test_prefix_applied_with_include_filter(self) -> None:
        tools = make_memory_tools(
            store=InMemoryMemoryStore(),
            tool_prefix="m_",
            include=["recall"],
        )
        assert [t.__name__ for t in tools] == ["m_recall"]

    def test_empty_prefix_does_not_alter_names(self) -> None:
        tools = make_memory_tools(store=InMemoryMemoryStore(), tool_prefix="")
        assert sorted(t.__name__ for t in tools) == ["forget", "recall", "remember"]


# ---------------------------------------------------------------------------
# _use_dep_principal path
# ---------------------------------------------------------------------------


class TestDepPrincipalPath:
    def test_use_dep_principal_true_emits_dep_param(self) -> None:
        from apx_agent._memory_tools import make_memory_tools as _make
        from apx_agent._inspection import _inspect_tool_fn

        store = InMemoryMemoryStore()
        tools = _make(store=store, _use_dep_principal=True)
        recall = next(t for t in tools if getattr(t, "__name__", None) == "recall")
        plain_params, dep_names = _inspect_tool_fn(recall)
        assert "principal" in dep_names, (
            "principal must be a DEP (if it's in plain_params the annotation "
            "didn't resolve — the __future__ annotations trap)"
        )
        assert "principal" not in plain_params, (
            "principal must NOT be in plain_params (would pollute LLM schema)"
        )

    def test_use_dep_principal_false_no_dep_param(self) -> None:
        from apx_agent._memory_tools import make_memory_tools as _make
        from apx_agent._inspection import _inspect_tool_fn

        store = InMemoryMemoryStore()
        tools = _make(store=store)  # default False
        recall = next(t for t in tools if getattr(t, "__name__", None) == "recall")
        _, dep_names = _inspect_tool_fn(recall)
        assert "principal" not in dep_names

    def test_dep_principal_tool_uses_principal_arg(self) -> None:
        from apx_agent._memory_tools import make_memory_tools as _make

        store = InMemoryMemoryStore()
        store.add({"principal_id": "alice", "content": "alice dep data"})
        tools = _make(store=store, _use_dep_principal=True)
        recall = next(t for t in tools if getattr(t, "__name__", None) == "recall")
        result = recall(query="dep data", principal="alice")
        assert "alice dep data" in result

    def test_dep_principal_none_returns_no_principal(self) -> None:
        from apx_agent._memory_tools import make_memory_tools as _make, NO_PRINCIPAL

        store = InMemoryMemoryStore()
        tools = _make(store=store, _use_dep_principal=True)
        recall = next(t for t in tools if getattr(t, "__name__", None) == "recall")
        assert NO_PRINCIPAL in recall(query="anything", principal=None)

    def test_dep_principal_remember_writes_under_injected_principal(self) -> None:
        from apx_agent._memory_tools import make_memory_tools as _make, NO_PRINCIPAL

        store = InMemoryMemoryStore()
        tools = _make(store=store, _use_dep_principal=True)
        remember = next(t for t in tools if getattr(t, "__name__", None) == "remember")
        recall = next(t for t in tools if getattr(t, "__name__", None) == "recall")
        # remember with injected principal=alice
        remember(content="alice remembered this", principal="alice")
        # alice can recall it; bob cannot; no-principal write is refused
        assert "alice remembered this" in recall(query="remembered", principal="alice")
        assert "alice remembered this" not in recall(query="remembered", principal="bob")
        assert NO_PRINCIPAL in remember(content="anon", principal=None)

    def test_dep_principal_isolation_alice_vs_bob(self) -> None:
        from apx_agent._memory_tools import make_memory_tools as _make

        store = InMemoryMemoryStore()
        store.add({"principal_id": "alice", "content": "alice only"})
        store.add({"principal_id": "bob", "content": "bob only"})
        tools = _make(store=store, _use_dep_principal=True)
        recall = next(t for t in tools if getattr(t, "__name__", None) == "recall")
        alice_result = recall(query="only", principal="alice")
        bob_result = recall(query="only", principal="bob")
        assert "alice only" in alice_result and "bob only" not in alice_result
        assert "bob only" in bob_result and "alice only" not in bob_result


class TestDepPrincipalFallsBackToDefault:
    """With ``_use_dep_principal=True`` (the config / agent-carried path), when
    the per-request OBO principal is absent (None — e.g. local ``apx-agent run`` with
    no X-Forwarded-User), the tools must fall back to ``default_principal_id``
    rather than no-op. Regression: memory was dead in the local dev loop because
    the dep branch ignored default_principal_id."""

    def test_remember_uses_default_when_dep_principal_none(self) -> None:
        store = InMemoryMemoryStore()
        tools = make_memory_tools(
            store=store, _use_dep_principal=True, default_principal_id="bob"
        )
        out = _find_tool(tools, "remember")(content="hello", principal=None)
        assert out != "No principal_id available; cannot recall memories."
        rows = store.list(MemoryFilter(principal_id="bob"))
        assert len(rows) == 1 and rows[0].content == "hello"

    def test_dep_principal_wins_over_default(self) -> None:
        store = InMemoryMemoryStore()
        tools = make_memory_tools(
            store=store, _use_dep_principal=True, default_principal_id="bob"
        )
        _find_tool(tools, "remember")(content="hi", principal="alice")
        assert len(store.list(MemoryFilter(principal_id="alice"))) == 1
        assert len(store.list(MemoryFilter(principal_id="bob"))) == 0

    def test_no_principal_and_no_default_still_degrades(self) -> None:
        tools = make_memory_tools(store=InMemoryMemoryStore(), _use_dep_principal=True)
        out = _find_tool(tools, "remember")(content="x", principal=None)
        assert out == "No principal_id available; cannot recall memories."


# ---------------------------------------------------------------------------
# dep-path forget publishes the per-request principal for scoped backends
# ---------------------------------------------------------------------------


class _ScopeRecordingStore(InMemoryMemoryStore):
    """Records the principal visible via current_principal() at delete time."""

    def __init__(self) -> None:
        super().__init__()
        self.delete_scope: str | None = "UNSET"

    def delete(self, memory_id: str) -> bool:
        from apx_agent._memory_tools import current_principal

        self.delete_scope = current_principal()
        return super().delete(memory_id)


class TestForgetPublishesPrincipal:
    @pytest.fixture(autouse=True)
    def _reset_principal_ctx(self) -> Any:
        # forget sets the request-scoped contextvar; ASGI isolates it per
        # request but pytest shares one context, so reset around each test.
        from apx_agent._memory_tools import _PRINCIPAL_CTX

        tok = _PRINCIPAL_CTX.set("")
        yield
        _PRINCIPAL_CTX.reset(tok)

    def test_dep_forget_carries_principal_dep(self) -> None:
        from apx_agent._inspection import _inspect_tool_fn

        tools = make_memory_tools(store=InMemoryMemoryStore(), _use_dep_principal=True)
        _, dep_names = _inspect_tool_fn(_find_tool(tools, "forget"))
        assert "principal" in dep_names

    def test_non_dep_forget_has_no_principal_dep(self) -> None:
        from apx_agent._inspection import _inspect_tool_fn

        tools = make_memory_tools(store=InMemoryMemoryStore())  # default False
        plain, dep_names = _inspect_tool_fn(_find_tool(tools, "forget"))
        assert "principal" not in dep_names
        assert "id" in plain

    def test_dep_forget_publishes_injected_principal(self) -> None:
        store = _ScopeRecordingStore()
        mem = store.add({"principal_id": "alice", "content": "x"})
        tools = make_memory_tools(store=store, _use_dep_principal=True)
        _find_tool(tools, "forget")(id=mem.id, principal="alice")
        assert store.delete_scope == "alice"

    def test_dep_forget_falls_back_to_default_principal(self) -> None:
        store = _ScopeRecordingStore()
        mem = store.add({"principal_id": "bob", "content": "x"})
        tools = make_memory_tools(
            store=store, _use_dep_principal=True, default_principal_id="bob"
        )
        _find_tool(tools, "forget")(id=mem.id, principal=None)
        assert store.delete_scope == "bob"
