"""Tests for ``_prompt_assembly.py`` — system-prompt-ready context strings.

Mirrors ``typescript/tests/prompt-assembly.test.ts``.
"""

from __future__ import annotations

from apx_agent import (
    InMemoryExampleStore,
    InMemoryMemoryStore,
    assemble_context,
    assemble_example_context,
    assemble_memory_context,
)
from apx_agent._example import FindSimilarOptions
from apx_agent._memory import RecallOptions


# ---------------------------------------------------------------------------
# assemble_memory_context
# ---------------------------------------------------------------------------


class TestAssembleMemoryContext:
    def test_returns_empty_string_when_store_empty(self) -> None:
        store = InMemoryMemoryStore()
        out = assemble_memory_context(
            store, {"principal_id": "u1", "query": "x"}
        )
        assert out == ""

    def test_emits_default_header_and_one_bullet_for_single_match(self) -> None:
        store = InMemoryMemoryStore()
        store.add({"principal_id": "u1", "content": "first thought"})
        out = assemble_memory_context(
            store, {"principal_id": "u1", "query": "x"}
        )
        assert out == "### Relevant memories\n- first thought"

    def test_emits_header_plus_multiple_bullets(self) -> None:
        store = InMemoryMemoryStore()
        store.add({"principal_id": "u1", "content": "one"})
        store.add({"principal_id": "u1", "content": "two"})
        out = assemble_memory_context(
            store, {"principal_id": "u1", "query": "x", "k": 5}
        )
        assert out.startswith("### Relevant memories\n")
        assert "- one" in out
        assert "- two" in out
        # header + 2 bullet lines
        assert len(out.split("\n")) == 3

    def test_honors_custom_header(self) -> None:
        store = InMemoryMemoryStore()
        store.add({"principal_id": "u1", "content": "item"})
        out = assemble_memory_context(
            store,
            {"principal_id": "u1", "query": "x"},
            header="## Memories",
        )
        assert out == "## Memories\n- item"

    def test_respects_k_limit(self) -> None:
        store = InMemoryMemoryStore()
        for i in range(10):
            store.add({"principal_id": "u1", "content": f"m{i}"})
        out = assemble_memory_context(
            store, {"principal_id": "u1", "query": "x", "k": 3}
        )
        # header + 3 bullets
        assert len(out.split("\n")) == 4

    def test_accepts_recall_options_dataclass(self) -> None:
        """The helper accepts the strongly-typed dataclass too, not just dicts."""
        store = InMemoryMemoryStore()
        store.add({"principal_id": "u1", "content": "via-dataclass"})
        out = assemble_memory_context(
            store, RecallOptions(principal_id="u1", query="x")
        )
        assert "via-dataclass" in out

    def test_passes_tags_filter_through(self) -> None:
        store = InMemoryMemoryStore()
        store.add({"principal_id": "u1", "content": "hot", "tags": ["pin"]})
        store.add({"principal_id": "u1", "content": "cold", "tags": ["casual"]})
        out = assemble_memory_context(
            store, {"principal_id": "u1", "query": "x", "tags": ["pin"]}
        )
        assert "hot" in out
        assert "cold" not in out


# ---------------------------------------------------------------------------
# assemble_example_context
# ---------------------------------------------------------------------------


class TestAssembleExampleContext:
    def test_returns_empty_string_when_store_empty(self) -> None:
        store = InMemoryExampleStore()
        out = assemble_example_context(
            store, {"agent_id": "a1", "query": "x"}
        )
        assert out == ""

    def test_emits_q_a_block_for_single_match(self) -> None:
        store = InMemoryExampleStore()
        store.add({"agent_id": "a1", "input": "q?", "output": "a."})
        out = assemble_example_context(
            store, {"agent_id": "a1", "query": "x"}
        )
        assert out == "### Relevant examples\n**Q:** q?\n**A:** a."

    def test_separates_multiple_matches_with_dash_block(self) -> None:
        store = InMemoryExampleStore()
        store.add({"agent_id": "a1", "input": "q1", "output": "a1"})
        store.add({"agent_id": "a1", "input": "q2", "output": "a2"})
        out = assemble_example_context(
            store, {"agent_id": "a1", "query": "x", "k": 5}
        )
        assert "**Q:** q1" in out
        assert "**Q:** q2" in out
        assert "\n---\n" in out

    def test_honors_custom_header(self) -> None:
        store = InMemoryExampleStore()
        store.add({"agent_id": "a1", "input": "q", "output": "a"})
        out = assemble_example_context(
            store,
            {"agent_id": "a1", "query": "x"},
            header="## Examples",
        )
        assert out.startswith("## Examples\n")

    def test_accepts_find_similar_options_dataclass(self) -> None:
        store = InMemoryExampleStore()
        store.add({"agent_id": "a1", "input": "q", "output": "via-dc"})
        out = assemble_example_context(
            store, FindSimilarOptions(agent_id="a1", query="x")
        )
        assert "via-dc" in out


# ---------------------------------------------------------------------------
# assemble_context (combined)
# ---------------------------------------------------------------------------


class TestAssembleContext:
    def test_returns_empty_when_neither_store_has_rows(self) -> None:
        memory = InMemoryMemoryStore()
        examples = InMemoryExampleStore()
        out = assemble_context(
            memory={"store": memory, "opts": {"principal_id": "u1", "query": "x"}},
            examples={"store": examples, "opts": {"agent_id": "a1", "query": "x"}},
        )
        assert out == ""

    def test_joins_both_blocks_with_default_separator(self) -> None:
        memory = InMemoryMemoryStore()
        memory.add({"principal_id": "u1", "content": "fact-1"})
        examples = InMemoryExampleStore()
        examples.add({"agent_id": "a1", "input": "q", "output": "a"})
        out = assemble_context(
            memory={"store": memory, "opts": {"principal_id": "u1", "query": "x"}},
            examples={"store": examples, "opts": {"agent_id": "a1", "query": "x"}},
        )
        assert "### Relevant memories" in out
        assert "- fact-1" in out
        assert "### Relevant examples" in out
        assert "**Q:** q" in out
        assert "\n\n" in out

    def test_returns_only_memory_block_when_examples_empty(self) -> None:
        memory = InMemoryMemoryStore()
        memory.add({"principal_id": "u1", "content": "fact"})
        examples = InMemoryExampleStore()
        out = assemble_context(
            memory={"store": memory, "opts": {"principal_id": "u1", "query": "x"}},
            examples={"store": examples, "opts": {"agent_id": "a1", "query": "x"}},
        )
        assert out.startswith("### Relevant memories")
        assert "### Relevant examples" not in out

    def test_returns_only_examples_block_when_memory_empty(self) -> None:
        memory = InMemoryMemoryStore()
        examples = InMemoryExampleStore()
        examples.add({"agent_id": "a1", "input": "q", "output": "a"})
        out = assemble_context(
            memory={"store": memory, "opts": {"principal_id": "u1", "query": "x"}},
            examples={"store": examples, "opts": {"agent_id": "a1", "query": "x"}},
        )
        assert out.startswith("### Relevant examples")
        assert "### Relevant memories" not in out

    def test_honors_per_block_custom_headers(self) -> None:
        memory = InMemoryMemoryStore()
        memory.add({"principal_id": "u1", "content": "fact"})
        examples = InMemoryExampleStore()
        examples.add({"agent_id": "a1", "input": "q", "output": "a"})
        out = assemble_context(
            memory={
                "store": memory,
                "opts": {"principal_id": "u1", "query": "x"},
                "header": "## M",
            },
            examples={
                "store": examples,
                "opts": {"agent_id": "a1", "query": "x"},
                "header": "## E",
            },
        )
        assert "## M\n" in out
        assert "## E\n" in out
        assert "### Relevant" not in out

    def test_honors_custom_separator(self) -> None:
        memory = InMemoryMemoryStore()
        memory.add({"principal_id": "u1", "content": "a"})
        examples = InMemoryExampleStore()
        examples.add({"agent_id": "a1", "input": "q", "output": "a"})
        out = assemble_context(
            memory={"store": memory, "opts": {"principal_id": "u1", "query": "x"}},
            examples={"store": examples, "opts": {"agent_id": "a1", "query": "x"}},
            separator="\n\n===\n\n",
        )
        assert "\n\n===\n\n" in out

    def test_only_memory_passed_returns_memory_block_only(self) -> None:
        memory = InMemoryMemoryStore()
        memory.add({"principal_id": "u1", "content": "only-mem"})
        out = assemble_context(
            memory={"store": memory, "opts": {"principal_id": "u1", "query": "x"}},
        )
        assert out == "### Relevant memories\n- only-mem"

    def test_only_examples_passed_returns_examples_block_only(self) -> None:
        examples = InMemoryExampleStore()
        examples.add({"agent_id": "a1", "input": "qq", "output": "aa"})
        out = assemble_context(
            examples={"store": examples, "opts": {"agent_id": "a1", "query": "x"}},
        )
        assert out == "### Relevant examples\n**Q:** qq\n**A:** aa"

    def test_no_args_returns_empty(self) -> None:
        assert assemble_context() == ""

    def test_opts_forwarding_filters_take_effect(self) -> None:
        """opts dict is forwarded faithfully — namespace filter still applies."""
        memory = InMemoryMemoryStore()
        memory.add(
            {"principal_id": "u1", "content": "in-profile", "namespace": "profile"}
        )
        memory.add(
            {"principal_id": "u1", "content": "in-other", "namespace": "other"}
        )
        out = assemble_context(
            memory={
                "store": memory,
                "opts": {
                    "principal_id": "u1",
                    "query": "x",
                    "namespace": "profile",
                },
            },
        )
        assert "in-profile" in out
        assert "in-other" not in out

    def test_separator_omitted_with_single_block(self) -> None:
        """Single-block output has no stray separator at edges."""
        memory = InMemoryMemoryStore()
        memory.add({"principal_id": "u1", "content": "solo"})
        out = assemble_context(
            memory={"store": memory, "opts": {"principal_id": "u1", "query": "x"}},
            separator="|SEP|",
        )
        assert "|SEP|" not in out

    def test_tag_filter_passes_through_for_examples(self) -> None:
        examples = InMemoryExampleStore()
        examples.add(
            {"agent_id": "a1", "input": "q1", "output": "gold", "tags": ["gold"]}
        )
        examples.add(
            {"agent_id": "a1", "input": "q2", "output": "bronze", "tags": ["bronze"]}
        )
        out = assemble_context(
            examples={
                "store": examples,
                "opts": {"agent_id": "a1", "query": "x", "tags": ["gold"]},
            },
        )
        assert "gold" in out
        assert "bronze" not in out
