"""Tests for ``_example_tools.py`` — agent-facing tools wrapping an
ExampleStore.

Mirrors ``typescript/tests/example-tools.test.ts`` case-by-case.
"""

from __future__ import annotations

from typing import Any

import pytest

from apx_agent import InMemoryExampleStore, make_example_tools
from apx_agent._example import ExampleFilter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
        tools = make_example_tools(store=InMemoryExampleStore())
        assert sorted(t.__name__ for t in tools) == [
            "find_examples",
            "remove_example",
            "save_example",
        ]

    def test_honors_include_single(self) -> None:
        tools = make_example_tools(
            store=InMemoryExampleStore(), include=["find_examples"]
        )
        assert [t.__name__ for t in tools] == ["find_examples"]

    def test_honors_include_two_tool_subset(self) -> None:
        tools = make_example_tools(
            store=InMemoryExampleStore(),
            include=["find_examples", "save_example"],
        )
        assert sorted(t.__name__ for t in tools) == ["find_examples", "save_example"]

    def test_applies_tool_prefix(self) -> None:
        tools = make_example_tools(
            store=InMemoryExampleStore(), tool_prefix="ex_"
        )
        assert sorted(t.__name__ for t in tools) == [
            "ex_find_examples",
            "ex_remove_example",
            "ex_save_example",
        ]


# ---------------------------------------------------------------------------
# Agent resolution
# ---------------------------------------------------------------------------


class TestAgentResolution:
    def test_uses_resolver_when_present(self) -> None:
        store = InMemoryExampleStore()
        store.add({"agent_id": "a1", "input": "Q", "output": "A"})
        tools = make_example_tools(store=store, agent_id_resolver=lambda: "a1")
        out = _find_tool(tools, "find_examples")(query="Q")
        assert "**Q:** Q" in out
        assert "**A:** A" in out

    def test_falls_back_to_default_when_resolver_returns_none(self) -> None:
        store = InMemoryExampleStore()
        store.add({"agent_id": "a1", "input": "Q", "output": "A"})
        tools = make_example_tools(
            store=store,
            agent_id_resolver=lambda: None,
            default_agent_id="a1",
        )
        out = _find_tool(tools, "find_examples")(query="Q")
        assert "**Q:** Q" in out

    def test_returns_no_agent_string_when_neither_resolves(self) -> None:
        tools = make_example_tools(store=InMemoryExampleStore())
        out = _find_tool(tools, "find_examples")(query="x")
        assert out == "No agent_id available; cannot look up examples."

    def test_save_example_degrades_gracefully_without_agent(self) -> None:
        tools = make_example_tools(store=InMemoryExampleStore())
        out = _find_tool(tools, "save_example")(input="x", output="y")
        assert out == "No agent_id available; cannot look up examples."

    def test_resolver_called_per_invocation(self) -> None:
        store = InMemoryExampleStore()
        store.add({"agent_id": "a1", "input": "q1", "output": "a1-out"})
        store.add({"agent_id": "a2", "input": "q2", "output": "a2-out"})
        state = {"current": "a1"}
        tools = make_example_tools(
            store=store,
            agent_id_resolver=lambda: state["current"],
        )
        find = _find_tool(tools, "find_examples")

        out1 = find(query="x")
        assert "a1-out" in out1

        state["current"] = "a2"
        out2 = find(query="x")
        assert "a2-out" in out2


# ---------------------------------------------------------------------------
# find_examples
# ---------------------------------------------------------------------------


class TestFindExamples:
    def test_returns_no_examples_found_when_empty(self) -> None:
        tools = make_example_tools(
            store=InMemoryExampleStore(),
            default_agent_id="a1",
        )
        out = _find_tool(tools, "find_examples")(query="x")
        assert out == "No examples found."

    def test_formats_as_q_a_markdown_blocks(self) -> None:
        store = InMemoryExampleStore()
        store.add({"agent_id": "a1", "input": "first?", "output": "first-answer"})
        store.add({"agent_id": "a1", "input": "second?", "output": "second-answer"})
        tools = make_example_tools(store=store, default_agent_id="a1")
        out = _find_tool(tools, "find_examples")(query="x")
        assert "**Q:** first?" in out
        assert "**A:** first-answer" in out
        assert "**Q:** second?" in out
        assert "**A:** second-answer" in out
        # Blocks separated by --- on its own line
        assert "\n---\n" in out

    def test_passes_intent_filter(self) -> None:
        store = InMemoryExampleStore()
        store.add(
            {"agent_id": "a1", "input": "p", "output": "a", "intent": "classify"}
        )
        store.add(
            {"agent_id": "a1", "input": "q", "output": "b", "intent": "extract"}
        )
        tools = make_example_tools(store=store, default_agent_id="a1")
        out = _find_tool(tools, "find_examples")(query="x", intent="classify")
        assert "**A:** a" in out
        assert "**A:** b" not in out

    def test_passes_tags_filter(self) -> None:
        store = InMemoryExampleStore()
        store.add(
            {"agent_id": "a1", "input": "p", "output": "gold-a", "tags": ["gold"]}
        )
        store.add(
            {
                "agent_id": "a1",
                "input": "q",
                "output": "bronze-a",
                "tags": ["bronze"],
            }
        )
        tools = make_example_tools(store=store, default_agent_id="a1")
        out = _find_tool(tools, "find_examples")(query="x", tags=["gold"])
        assert "gold-a" in out
        assert "bronze-a" not in out

    def test_honors_intent_default_when_handler_omits(self) -> None:
        store = InMemoryExampleStore()
        store.add(
            {
                "agent_id": "a1",
                "input": "p",
                "output": "in-classify",
                "intent": "classify",
            }
        )
        store.add(
            {
                "agent_id": "a1",
                "input": "q",
                "output": "in-other",
                "intent": "other",
            }
        )
        tools = make_example_tools(
            store=store,
            default_agent_id="a1",
            intent_default="classify",
        )
        out = _find_tool(tools, "find_examples")(query="x")
        assert "in-classify" in out
        assert "in-other" not in out

    def test_k_limits_returned_blocks(self) -> None:
        store = InMemoryExampleStore()
        for i in range(5):
            store.add({"agent_id": "a1", "input": f"q{i}", "output": f"a{i}"})
        tools = make_example_tools(store=store, default_agent_id="a1")
        out = _find_tool(tools, "find_examples")(query="x", k=2)
        # Two blocks => one '\n---\n' joiner
        assert out.count("\n---\n") == 1


# ---------------------------------------------------------------------------
# save_example
# ---------------------------------------------------------------------------


class TestSaveExample:
    def test_persists_an_example_and_returns_saved_id(self) -> None:
        store = InMemoryExampleStore()
        tools = make_example_tools(store=store, default_agent_id="a1")
        out = _find_tool(tools, "save_example")(input="q", output="a")
        assert out.startswith("Saved example ex_")
        rows = store.list(ExampleFilter(agent_id="a1"))
        assert len(rows) == 1

    def test_passes_intent_score_tags_through(self) -> None:
        store = InMemoryExampleStore()
        tools = make_example_tools(store=store, default_agent_id="a1")
        _find_tool(tools, "save_example")(
            input="q",
            output="a",
            intent="classify",
            score=0.9,
            tags=["gold"],
        )
        rows = store.list(ExampleFilter(agent_id="a1"))
        assert rows[0].intent == "classify"
        assert rows[0].score == pytest.approx(0.9)
        assert rows[0].tags == ("gold",)

    def test_rejects_score_outside_zero_one(self) -> None:
        tools = make_example_tools(
            store=InMemoryExampleStore(),
            default_agent_id="a1",
        )
        with pytest.raises(ValueError, match=r"score must be in \[0, 1\]"):
            _find_tool(tools, "save_example")(input="q", output="a", score=1.5)

    def test_rejects_negative_score(self) -> None:
        tools = make_example_tools(
            store=InMemoryExampleStore(),
            default_agent_id="a1",
        )
        with pytest.raises(ValueError, match=r"score must be in \[0, 1\]"):
            _find_tool(tools, "save_example")(input="q", output="a", score=-0.1)

    def test_save_uses_intent_default(self) -> None:
        store = InMemoryExampleStore()
        tools = make_example_tools(
            store=store,
            default_agent_id="a1",
            intent_default="classify",
        )
        _find_tool(tools, "save_example")(input="q", output="a")
        rows = store.list(ExampleFilter(agent_id="a1"))
        assert rows[0].intent == "classify"


# ---------------------------------------------------------------------------
# remove_example
# ---------------------------------------------------------------------------


class TestRemoveExample:
    def test_deletes_an_example_by_id_and_returns_success(self) -> None:
        store = InMemoryExampleStore()
        ex = store.add({"agent_id": "a1", "input": "q", "output": "a"})
        tools = make_example_tools(store=store, default_agent_id="a1")
        out = _find_tool(tools, "remove_example")(id=ex.id)
        assert out == f"Removed example {ex.id}."
        assert store.get(ex.id) is None

    def test_returns_missing_id_message_when_no_such_example(self) -> None:
        tools = make_example_tools(
            store=InMemoryExampleStore(),
            default_agent_id="a1",
        )
        out = _find_tool(tools, "remove_example")(id="ex_nope")
        assert out == "No example found with id ex_nope."

    def test_remove_works_without_agent(self) -> None:
        """Deletion is agent-agnostic — keyed by id only."""
        store = InMemoryExampleStore()
        ex = store.add({"agent_id": "a1", "input": "q", "output": "a"})
        tools = make_example_tools(store=store)
        out = _find_tool(tools, "remove_example")(id=ex.id)
        assert "Removed" in out
