"""Tests for ``_example_mining`` — mirror of the TS suite."""

from __future__ import annotations

from typing import Any

import pytest

from apx_agent._example import InMemoryExampleStore
from apx_agent._example_mining import Turn, mine_examples, pair_turns
from apx_agent._session import InMemorySessionStore, Session


def _seed(store: InMemorySessionStore, sid: str, history: list[dict[str, Any]]) -> None:
    store.put(Session(session_id=sid, history=list(history)))


# ---------------------------------------------------------------------------
# pair_turns
# ---------------------------------------------------------------------------


class TestPairTurns:
    def test_pairs_simple_user_assistant(self) -> None:
        turns = pair_turns(
            "s1",
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
        )
        assert len(turns) == 1
        assert turns[0].turn_index == 0
        assert turns[0].session_id == "s1"

    def test_skips_tool_messages_between(self) -> None:
        turns = pair_turns(
            "s1",
            [
                {"role": "user", "content": "ask"},
                {"role": "assistant", "content": ""},  # tool-call only
                {"role": "tool", "content": "tool result"},
                {"role": "assistant", "content": "final"},
            ],
        )
        assert len(turns) == 1
        assert turns[0].assistant_message["content"] == "final"

    def test_emits_multiple_turns_in_one_session(self) -> None:
        turns = pair_turns(
            "s1",
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
            ],
        )
        assert [t.turn_index for t in turns] == [0, 1]


# ---------------------------------------------------------------------------
# mine_examples
# ---------------------------------------------------------------------------


class TestMineExamples:
    def test_mines_single_turn(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        _seed(
            session_store,
            "s1",
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
        )
        result = mine_examples(
            session_store=session_store,
            example_store=example_store,
            agent_id="triage",
        )
        assert result.sessions_scanned == 1
        assert result.turns_considered == 1
        assert result.examples_added == 1
        from apx_agent._example import ExampleFilter

        stored = example_store.list(ExampleFilter(agent_id="triage"))
        assert len(stored) == 1
        assert stored[0].input == "hi"
        assert stored[0].output == "hello"

    def test_default_filter_excludes_whitespace_only_assistant(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        _seed(
            session_store,
            "s1",
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "   "},
            ],
        )
        result = mine_examples(
            session_store=session_store,
            example_store=example_store,
            agent_id="a",
        )
        assert result.examples_added == 0

    def test_intent_fn_forwarded(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        _seed(
            session_store,
            "s1",
            [
                {"role": "user", "content": "bill"},
                {"role": "assistant", "content": "check"},
            ],
        )
        mine_examples(
            session_store=session_store,
            example_store=example_store,
            agent_id="a",
            intent_fn=lambda _t: "billing",
        )
        from apx_agent._example import ExampleFilter

        [ex] = example_store.list(ExampleFilter(agent_id="a"))
        assert ex.intent == "billing"

    def test_score_fn_forwarded(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        _seed(
            session_store,
            "s1",
            [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ],
        )
        mine_examples(
            session_store=session_store,
            example_store=example_store,
            agent_id="a",
            score_fn=lambda _t: 0.9,
        )
        from apx_agent._example import ExampleFilter

        [ex] = example_store.list(ExampleFilter(agent_id="a"))
        assert ex.score == 0.9

    def test_min_score_filters_low_turns(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        _seed(
            session_store,
            "s1",
            [
                {"role": "user", "content": "good"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "bad"},
                {"role": "assistant", "content": "meh"},
            ],
        )
        result = mine_examples(
            session_store=session_store,
            example_store=example_store,
            agent_id="a",
            score_fn=lambda t: 0.9 if t.user_message["content"] == "good" else 0.1,
            min_score=0.5,
        )
        assert result.examples_added == 1
        from apx_agent._example import ExampleFilter

        [ex] = example_store.list(ExampleFilter(agent_id="a"))
        assert ex.input == "good"

    def test_min_score_keeps_none_scored(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        _seed(
            session_store,
            "s1",
            [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ],
        )
        result = mine_examples(
            session_store=session_store,
            example_store=example_store,
            agent_id="a",
            score_fn=lambda _t: None,
            min_score=0.9,
        )
        assert result.examples_added == 1

    def test_limit_caps_examples(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        _seed(
            session_store,
            "s1",
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
                {"role": "user", "content": "q3"},
                {"role": "assistant", "content": "a3"},
            ],
        )
        result = mine_examples(
            session_store=session_store,
            example_store=example_store,
            agent_id="a",
            limit=2,
        )
        assert result.examples_added == 2

    def test_dry_run_does_not_write(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        _seed(
            session_store,
            "s1",
            [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ],
        )
        result = mine_examples(
            session_store=session_store,
            example_store=example_store,
            agent_id="a",
            dry_run=True,
        )
        assert len(result.examples) == 1
        assert result.examples_added == 0
        from apx_agent._example import ExampleFilter

        assert example_store.list(ExampleFilter(agent_id="a")) == []

    def test_multi_session(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        _seed(
            session_store,
            "s1",
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
            ],
        )
        _seed(
            session_store,
            "s2",
            [
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
            ],
        )
        result = mine_examples(
            session_store=session_store,
            example_store=example_store,
            agent_id="a",
        )
        assert result.sessions_scanned == 2
        assert result.examples_added == 2

    def test_session_ids_subset(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        _seed(
            session_store,
            "s1",
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
            ],
        )
        _seed(
            session_store,
            "s2",
            [
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
            ],
        )
        result = mine_examples(
            session_store=session_store,
            example_store=example_store,
            agent_id="a",
            session_ids=["s2"],
        )
        assert result.examples_added == 1
        from apx_agent._example import ExampleFilter

        [ex] = example_store.list(ExampleFilter(agent_id="a"))
        assert ex.input == "q2"

    def test_tags_and_metadata_forwarded(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        _seed(
            session_store,
            "s1",
            [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ],
        )
        mine_examples(
            session_store=session_store,
            example_store=example_store,
            agent_id="a",
            tags_fn=lambda _t: ("mined", "cold-start"),
            metadata_fn=lambda _t: {"source": "mining"},
        )
        from apx_agent._example import ExampleFilter

        [ex] = example_store.list(ExampleFilter(agent_id="a"))
        assert ex.tags == ("mined", "cold-start")
        assert ex.metadata["source"] == "mining"
        assert ex.metadata["session_id"] == "s1"
        assert ex.metadata["turn_index"] == 0

    def test_tool_messages_skipped(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        _seed(
            session_store,
            "s1",
            [
                {"role": "user", "content": "ask"},
                {"role": "assistant", "content": ""},
                {"role": "tool", "content": "42"},
                {"role": "assistant", "content": "the answer is 42"},
            ],
        )
        result = mine_examples(
            session_store=session_store,
            example_store=example_store,
            agent_id="a",
        )
        assert result.examples_added == 1
        from apx_agent._example import ExampleFilter

        [ex] = example_store.list(ExampleFilter(agent_id="a"))
        assert ex.output == "the answer is 42"

    def test_missing_snapshot_skipped(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        result = mine_examples(
            session_store=session_store,
            example_store=example_store,
            agent_id="a",
            session_ids=["ghost"],
        )
        assert result.sessions_scanned == 0
        assert result.examples_added == 0

    def test_custom_filter_fn(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        _seed(
            session_store,
            "s1",
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "long answer"},
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "short"},
            ],
        )
        result = mine_examples(
            session_store=session_store,
            example_store=example_store,
            agent_id="a",
            filter_fn=lambda t: len(t.assistant_message["content"]) > 5,
        )
        assert result.examples_added == 1
        from apx_agent._example import ExampleFilter

        [ex] = example_store.list(ExampleFilter(agent_id="a"))
        assert ex.output == "long answer"

    def test_turns_considered_counts_before_filter(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        _seed(
            session_store,
            "s1",
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
            ],
        )
        result = mine_examples(
            session_store=session_store,
            example_store=example_store,
            agent_id="a",
            filter_fn=lambda _t: False,
        )
        assert result.turns_considered == 2
        assert result.examples_added == 0

    def test_dry_run_id_has_prefix(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        _seed(
            session_store,
            "s1",
            [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ],
        )
        result = mine_examples(
            session_store=session_store,
            example_store=example_store,
            agent_id="a",
            dry_run=True,
        )
        assert result.examples[0].id.startswith("ex_")

    def test_async_callable_rejected(self) -> None:
        session_store = InMemorySessionStore()
        example_store = InMemoryExampleStore()
        _seed(
            session_store,
            "s1",
            [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ],
        )

        async def async_intent(_t: Turn) -> str:
            return "boom"

        with pytest.raises(TypeError):
            mine_examples(
                session_store=session_store,
                example_store=example_store,
                agent_id="a",
                intent_fn=async_intent,  # type: ignore[arg-type]
            )

    def test_no_list_raises(self) -> None:
        # A store with no .list() and no _sessions dict.
        class BareStore:
            def get(self, _sid: str) -> Session | None:
                return None

        with pytest.raises(ValueError):
            mine_examples(
                session_store=BareStore(),
                example_store=InMemoryExampleStore(),
                agent_id="a",
            )
