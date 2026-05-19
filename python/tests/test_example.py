"""Tests for _example.py — durable few-shot examples and InMemoryExampleStore.

Mirrors typescript/tests/example.test.ts case-by-case.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import pytest

from apx_agent._example import (
    ExampleFilter,
    FindSimilarOptions,
    InMemoryExampleStore,
    apply_example_patch,
    materialize_example,
    new_example_id,
)
from apx_agent._memory import EmbeddingFn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_toy_embedder() -> EmbeddingFn:
    """Deterministic toy embedder: bumps a 26-dim vec at each word's first letter."""

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


def make_input(**overrides: object) -> dict[str, object]:
    """Default example input — triage / billing question."""
    base: dict[str, object] = {
        "agent_id": "triage",
        "input": "Why is my bill so high?",
        "output": "Let me check your usage.",
    }
    base.update(overrides)
    return base


# ===========================================================================
# materialize_example
# ===========================================================================


def test_materialize_mints_id_and_timestamps_defaults_intent() -> None:
    ex = materialize_example(make_input())
    assert ex.id.startswith("ex_")
    assert ex.intent == "default"
    assert ex.created_at == ex.updated_at


def test_materialize_preserves_caller_supplied_id() -> None:
    ex = materialize_example(make_input(id="fixed"))
    assert ex.id == "fixed"


def test_materialize_attaches_embedding_when_provided() -> None:
    ex = materialize_example(make_input(), embedding=[0.1, 0.2])
    assert ex.embedding == (0.1, 0.2)


def test_materialize_score_optional() -> None:
    ex_none = materialize_example(make_input())
    assert ex_none.score is None
    ex_score = materialize_example(make_input(score=0.7))
    assert ex_score.score == 0.7


def test_new_example_id_uses_prefix() -> None:
    assert new_example_id().startswith("ex_")


# ===========================================================================
# apply_example_patch
# ===========================================================================


def test_apply_patch_updates_only_provided_fields_and_bumps_updated_at() -> None:
    ex = materialize_example(make_input(output="old"))
    time.sleep(0.005)
    patched = apply_example_patch(ex, {"output": "new"})
    assert patched.output == "new"
    assert patched.input == ex.input
    assert patched.updated_at > ex.updated_at


# ===========================================================================
# InMemoryExampleStore — CRUD
# ===========================================================================


def test_add_persists_with_materialized_id_and_timestamps() -> None:
    store = InMemoryExampleStore()
    ex = store.add(make_input(input="q1"))
    assert store.get(ex.id) == ex


def test_add_batch_persists_multiple() -> None:
    store = InMemoryExampleStore()
    exs = store.add_batch([make_input(input="q1"), make_input(input="q2")])
    assert len(exs) == 2


def test_add_batch_empty_returns_empty() -> None:
    store = InMemoryExampleStore()
    assert store.add_batch([]) == []


def test_update_applies_patch_and_bumps_updated_at() -> None:
    store = InMemoryExampleStore()
    ex = store.add(make_input(output="old"))
    updated = store.update(ex.id, {"output": "new", "score": 0.9})
    assert updated is not None
    assert updated.output == "new"
    assert updated.score == 0.9


def test_update_returns_none_for_missing_id() -> None:
    store = InMemoryExampleStore()
    assert store.update("nope", {"output": "x"}) is None


def test_delete_returns_true_on_hit_false_on_miss() -> None:
    store = InMemoryExampleStore()
    ex = store.add(make_input())
    assert store.delete(ex.id) is True
    assert store.delete(ex.id) is False


def test_list_scopes_by_agent_id() -> None:
    store = InMemoryExampleStore()
    store.add(make_input(agent_id="triage"))
    store.add(make_input(agent_id="billing"))
    assert len(store.list(ExampleFilter(agent_id="triage"))) == 1


def test_list_filters_by_intent_tags_min_score() -> None:
    store = InMemoryExampleStore()
    store.add(make_input(intent="classify", tags=["x"], score=0.4))
    store.add(make_input(intent="classify", tags=["y"], score=0.9))
    store.add(make_input(intent="extract", tags=["x"], score=0.5))
    assert (
        len(store.list(ExampleFilter(agent_id="triage", intent="classify"))) == 2
    )
    assert len(store.list(ExampleFilter(agent_id="triage", tags=("x",)))) == 2
    assert len(store.list(ExampleFilter(agent_id="triage", min_score=0.6))) == 1


def test_list_excludes_rows_with_no_score_when_min_score_set() -> None:
    store = InMemoryExampleStore()
    store.add(make_input())  # no score
    assert len(store.list(ExampleFilter(agent_id="triage", min_score=0.5))) == 0


def test_list_respects_limit_and_sorts_by_updated_at_desc() -> None:
    store = InMemoryExampleStore()
    store.add(make_input(input="first"))
    time.sleep(0.005)
    store.add(make_input(input="second"))
    rows = store.list(ExampleFilter(agent_id="triage", limit=1))
    assert rows[0].input == "second"


# ===========================================================================
# InMemoryExampleStore — find_similar
# ===========================================================================


def test_find_similar_ranks_by_cosine_similarity() -> None:
    store = InMemoryExampleStore(embedding_fn=make_toy_embedder())
    store.add(make_input(input="apple apple"))
    store.add(make_input(input="zebra zebra"))
    results = store.find_similar(
        FindSimilarOptions(agent_id="triage", query="apple", k=2)
    )
    assert results[0].example.input == "apple apple"
    assert results[0].score > results[1].score


def test_find_similar_embeds_input_field_not_output() -> None:
    calls: list[list[str]] = []

    def fn(texts: Sequence[str]) -> list[list[float]]:
        calls.append(list(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]

    store = InMemoryExampleStore(embedding_fn=fn)
    store.add(make_input(input="IN", output="OUT"))
    assert calls[0] == ["IN"]


def test_find_similar_embedder_called_on_update_when_input_changes() -> None:
    calls: list[list[str]] = []

    def fn(texts: Sequence[str]) -> list[list[float]]:
        calls.append(list(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]

    store = InMemoryExampleStore(embedding_fn=fn)
    ex = store.add(make_input(input="X"))
    store.update(ex.id, {"input": "Y"})
    assert len(calls) == 2
    assert calls[1] == ["Y"]


def test_find_similar_falls_back_to_recency_without_embedder() -> None:
    store = InMemoryExampleStore()
    store.add(make_input(input="older"))
    time.sleep(0.005)
    store.add(make_input(input="newer"))
    results = store.find_similar(
        FindSimilarOptions(agent_id="triage", query="x", k=2)
    )
    assert results[0].example.input == "newer"


def test_find_similar_respects_intent_filter() -> None:
    store = InMemoryExampleStore(embedding_fn=make_toy_embedder())
    store.add(make_input(input="apple", intent="classify"))
    store.add(make_input(input="apple", intent="extract"))
    results = store.find_similar(
        FindSimilarOptions(agent_id="triage", query="apple", intent="classify", k=5)
    )
    assert len(results) == 1
    assert results[0].example.intent == "classify"


def test_find_similar_returns_empty_when_no_candidates() -> None:
    store = InMemoryExampleStore(embedding_fn=make_toy_embedder())
    assert (
        store.find_similar(FindSimilarOptions(agent_id="nobody", query="x")) == []
    )


@pytest.mark.parametrize("k_value,expected_len", [(1, 1), (5, 2), (10, 2)])
def test_find_similar_respects_k(k_value: int, expected_len: int) -> None:
    store = InMemoryExampleStore(embedding_fn=make_toy_embedder())
    store.add(make_input(input="apple"))
    store.add(make_input(input="banana"))
    results = store.find_similar(
        FindSimilarOptions(agent_id="triage", query="apple", k=k_value)
    )
    assert len(results) == expected_len
