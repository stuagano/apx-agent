"""Tests for the shared tool-factory builder (build_tool / resolve_description)."""

from __future__ import annotations

import pytest

from apx_agent import ResourceSpec, build_tool, get_tool_metadata, resolve_description, tool
from apx_agent._resources import get_resources


def _sample():
    async def _call(query: str) -> str:
        return query
    return _call


class TestBuildTool:
    def test_stamps_name_and_doc(self):
        fn = build_tool(_sample(), name="ask_genie", description="Ask Genie a question.")
        assert fn.__name__ == "ask_genie"
        assert fn.__qualname__ == "ask_genie"
        assert fn.__doc__ == "Ask Genie a question."

    def test_returns_same_callable(self):
        original = _sample()
        result = build_tool(original, name="x", description="d")
        assert result is original

    def test_execution_uses_tool_metadata_and_preserves_existing_metadata(self):
        @tool(effect="read")
        def lookup(query: str) -> str:
            return query

        result = build_tool(
            lookup,
            name="lookup",
            description="Lookup.",
            execution="user",
        )

        metadata = get_tool_metadata(result)
        assert metadata is not None
        assert metadata.effect == "read"
        assert metadata.execution == "user"

    @pytest.mark.parametrize("execution", ["", "app", "USER"])
    def test_rejects_invalid_execution_identity(self, execution: str):
        with pytest.raises(ValueError, match="user.*service"):
            build_tool(
                _sample(),
                name="lookup",
                description="Lookup.",
                execution=execution,  # type: ignore[arg-type]
            )

    def test_attaches_resources(self):
        fn = build_tool(
            _sample(),
            name="t",
            description="d",
            resources=[ResourceSpec("genie_space", "space-123")],
        )
        specs = get_resources(fn)
        assert len(specs) == 1
        assert specs[0].kind == "genie_space"
        assert specs[0].identifier == "space-123"

    def test_no_resources_attaches_nothing(self):
        fn = build_tool(_sample(), name="t", description="d")
        assert get_resources(fn) == []

    def test_empty_iterable_attaches_nothing(self):
        fn = build_tool(_sample(), name="t", description="d", resources=[])
        assert get_resources(fn) == []

    def test_none_entries_are_skipped(self):
        # Mirrors the conditional-resource pattern (sql_tool: warehouse may be None).
        fn = build_tool(
            _sample(),
            name="t",
            description="d",
            resources=[None, ResourceSpec("sql_warehouse", "wh-1"), None],  # type: ignore[list-item]
        )
        specs = get_resources(fn)
        assert len(specs) == 1
        assert specs[0].identifier == "wh-1"

    def test_multiple_resources(self):
        fn = build_tool(
            _sample(),
            name="t",
            description="d",
            resources=[
                ResourceSpec("uc_table", "main.s.t"),
                ResourceSpec("sql_warehouse", "wh-1"),
            ],
        )
        assert len(get_resources(fn)) == 2

    @pytest.mark.asyncio
    async def test_callable_still_runs(self):
        fn = build_tool(_sample(), name="echo", description="d")
        assert await fn("hello") == "hello"


class TestResolveDescription:
    def test_explicit_wins(self):
        assert resolve_description("explicit", uc_comment="comment", fallback="fb") == "explicit"

    def test_uc_comment_beats_fallback(self):
        assert resolve_description(None, uc_comment="comment", fallback="fb") == "comment"

    def test_fallback_when_nothing_else(self):
        assert resolve_description(None, fallback="fb") == "fb"

    def test_empty_explicit_falls_through(self):
        assert resolve_description("", uc_comment="comment", fallback="fb") == "comment"
