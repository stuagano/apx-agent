"""Tests for _memory.py — durable memory protocol and InMemoryMemoryStore.

Mirrors typescript/tests/memory.test.ts case-by-case so the two
implementations stay in lock-step.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import pytest

from apx_agent._memory import (
    EmbeddingFn,
    InMemoryMemoryStore,
    Memory,
    MemoryFilter,
    RecallOptions,
    apply_patch,
    cosine_similarity,
    materialize_memory,
    new_memory_id,
    tags_overlap,
)


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
    """Default memory input — user-1 / 'apple banana' — with overrides."""
    base: dict[str, object] = {"principal_id": "user-1", "content": "apple banana"}
    base.update(overrides)
    return base


# ===========================================================================
# cosine_similarity
# ===========================================================================


def test_cosine_identical_vectors_returns_1() -> None:
    assert cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_returns_0() -> None:
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)


def test_cosine_empty_or_mismatched_returns_0() -> None:
    assert cosine_similarity([], [1]) == 0.0
    assert cosine_similarity([1, 2], [1]) == 0.0


def test_cosine_all_zero_vector_returns_0() -> None:
    assert cosine_similarity([0, 0, 0], [1, 1, 1]) == 0.0


# ===========================================================================
# tags_overlap
# ===========================================================================


def test_tags_overlap_when_one_shared() -> None:
    assert tags_overlap(["a", "b"], ["b", "c"]) is True


def test_tags_overlap_rejects_when_none_shared() -> None:
    assert tags_overlap(["a"], ["b"]) is False


def test_tags_overlap_empty_or_none_filter_matches_all() -> None:
    assert tags_overlap(["a"], []) is True
    assert tags_overlap(["a"], None) is True


# ===========================================================================
# materialize_memory
# ===========================================================================


def test_materialize_mints_id_and_timestamps_when_omitted() -> None:
    m = materialize_memory({"principal_id": "u", "content": "hi"})
    assert m.id.startswith("mem_")
    assert isinstance(m.created_at, str)
    assert m.created_at == m.updated_at
    assert m.namespace == "default"
    assert m.importance == 0.5


def test_materialize_preserves_caller_supplied_id() -> None:
    m = materialize_memory({"principal_id": "u", "content": "hi", "id": "fixed"})
    assert m.id == "fixed"


def test_materialize_freezes_tags_and_metadata() -> None:
    m = materialize_memory(
        {"principal_id": "u", "content": "hi", "tags": ["a"], "metadata": {"k": 1}}
    )
    assert m.tags == ("a",)
    # tuples are immutable
    with pytest.raises(AttributeError):
        m.tags.append("b")  # type: ignore[attr-defined]
    # the Memory dataclass itself is frozen
    with pytest.raises(Exception):
        m.tags = ("b",)  # type: ignore[misc]


def test_materialize_attaches_embedding_when_provided() -> None:
    m = materialize_memory(
        {"principal_id": "u", "content": "hi"}, embedding=[1.0, 2.0, 3.0]
    )
    assert m.embedding == (1.0, 2.0, 3.0)


def test_new_memory_id_uses_prefix() -> None:
    assert new_memory_id("mem").startswith("mem_")
    assert new_memory_id("ex").startswith("ex_")


# ===========================================================================
# apply_patch
# ===========================================================================


def test_apply_patch_updates_only_provided_fields_and_bumps_updated_at() -> None:
    m = materialize_memory({"principal_id": "u", "content": "old", "importance": 0.3})
    time.sleep(0.005)
    patched = apply_patch(m, {"content": "new"})
    assert patched.content == "new"
    assert patched.importance == 0.3
    assert patched.updated_at > m.updated_at


def test_apply_patch_ignores_unknown_keys() -> None:
    m = materialize_memory({"principal_id": "u", "content": "hi"})
    patched = apply_patch(m, {"unrelated": 999})
    assert patched.content == m.content
    assert patched.importance == m.importance


# ===========================================================================
# InMemoryMemoryStore — CRUD
# ===========================================================================


def test_add_returns_materialized_memory_and_persists() -> None:
    store = InMemoryMemoryStore()
    m = store.add(make_input(content="hello"))
    assert m.content == "hello"
    assert store.get(m.id) == m


def test_add_batch_persists_multiple() -> None:
    store = InMemoryMemoryStore()
    ms = store.add_batch([make_input(content="a"), make_input(content="b")])
    assert len(ms) == 2
    assert len(store.list(MemoryFilter(principal_id="user-1"))) == 2


def test_add_batch_empty_returns_empty() -> None:
    store = InMemoryMemoryStore()
    assert store.add_batch([]) == []


def test_update_applies_patch_and_bumps_updated_at() -> None:
    store = InMemoryMemoryStore()
    m = store.add(make_input(content="old"))
    updated = store.update(m.id, {"content": "new", "importance": 0.9})
    assert updated is not None
    assert updated.content == "new"
    assert updated.importance == 0.9


def test_update_returns_none_for_missing_id() -> None:
    store = InMemoryMemoryStore()
    assert store.update("nope", {"content": "x"}) is None


def test_delete_returns_true_on_hit_false_on_miss() -> None:
    store = InMemoryMemoryStore()
    m = store.add(make_input())
    assert store.delete(m.id) is True
    assert store.delete(m.id) is False


def test_list_scopes_by_principal_id() -> None:
    store = InMemoryMemoryStore()
    store.add(make_input(principal_id="a", content="a-mem"))
    store.add(make_input(principal_id="b", content="b-mem"))
    a_results = store.list(MemoryFilter(principal_id="a"))
    assert len(a_results) == 1
    assert a_results[0].content == "a-mem"


def test_list_filters_by_namespace_tags_min_importance() -> None:
    store = InMemoryMemoryStore()
    store.add(
        make_input(content="m1", namespace="profile", tags=["x"], importance=0.2)
    )
    store.add(
        make_input(content="m2", namespace="profile", tags=["y"], importance=0.9)
    )
    store.add(
        make_input(content="m3", namespace="episodic", tags=["x"], importance=0.5)
    )
    assert (
        len(store.list(MemoryFilter(principal_id="user-1", namespace="profile"))) == 2
    )
    assert (
        len(store.list(MemoryFilter(principal_id="user-1", tags=("x",)))) == 2
    )
    assert (
        len(store.list(MemoryFilter(principal_id="user-1", min_importance=0.6))) == 1
    )


def test_list_respects_limit_and_sorts_by_updated_at_desc() -> None:
    store = InMemoryMemoryStore()
    store.add(make_input(content="first"))
    time.sleep(0.005)
    store.add(make_input(content="second"))
    rows = store.list(MemoryFilter(principal_id="user-1", limit=1))
    assert rows[0].content == "second"


# ===========================================================================
# InMemoryMemoryStore — recall
# ===========================================================================


def test_recall_uses_cosine_similarity_when_embedder_wired() -> None:
    store = InMemoryMemoryStore(embedding_fn=make_toy_embedder())
    store.add(make_input(content="apple apple apple"))
    store.add(make_input(content="zebra zebra zebra"))
    results = store.recall(
        RecallOptions(principal_id="user-1", query="apple", k=2)
    )
    assert results[0].memory.content == "apple apple apple"
    assert results[0].score > results[1].score


def test_recall_embedder_called_on_add() -> None:
    calls: list[list[str]] = []

    def fn(texts: Sequence[str]) -> list[list[float]]:
        calls.append(list(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]

    store = InMemoryMemoryStore(embedding_fn=fn)
    m = store.add(make_input(content="X"))
    assert calls[0] == ["X"]
    assert m.embedding == (1.0, 0.0, 0.0)


def test_recall_embedder_called_on_update_when_content_changes() -> None:
    calls: list[list[str]] = []

    def fn(texts: Sequence[str]) -> list[list[float]]:
        calls.append(list(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]

    store = InMemoryMemoryStore(embedding_fn=fn)
    m = store.add(make_input(content="X"))
    store.update(m.id, {"content": "Y"})
    assert len(calls) == 2
    assert calls[1] == ["Y"]


def test_recall_falls_back_to_recency_without_embedder() -> None:
    store = InMemoryMemoryStore()
    store.add(make_input(content="old"))
    time.sleep(0.005)
    store.add(make_input(content="new"))
    results = store.recall(
        RecallOptions(principal_id="user-1", query="anything", k=2)
    )
    assert results[0].memory.content == "new"
    assert results[0].score > results[1].score


def test_recall_respects_namespace_filter() -> None:
    store = InMemoryMemoryStore(embedding_fn=make_toy_embedder())
    store.add(make_input(content="apple", namespace="a"))
    store.add(make_input(content="apple", namespace="b"))
    results = store.recall(
        RecallOptions(principal_id="user-1", query="apple", namespace="a", k=5)
    )
    assert len(results) == 1
    assert results[0].memory.namespace == "a"


def test_recall_respects_tag_filter() -> None:
    store = InMemoryMemoryStore(embedding_fn=make_toy_embedder())
    store.add(make_input(content="apple", tags=["alpha"]))
    store.add(make_input(content="apple", tags=["beta"]))
    results = store.recall(
        RecallOptions(principal_id="user-1", query="apple", tags=("alpha",), k=5)
    )
    assert len(results) == 1
    assert "alpha" in results[0].memory.tags


def test_recall_returns_empty_when_no_candidates() -> None:
    store = InMemoryMemoryStore(embedding_fn=make_toy_embedder())
    assert (
        store.recall(RecallOptions(principal_id="nobody", query="x")) == []
    )


def test_recall_skips_rows_without_embeddings() -> None:
    """When an embedder is wired but a row lacks an embedding, skip it cleanly."""
    store = InMemoryMemoryStore(embedding_fn=make_toy_embedder())
    # Inject a row that lacks an embedding (e.g. legacy data)
    raw = materialize_memory(make_input(content="apple"))
    store._rows[raw.id] = Memory(  # type: ignore[attr-defined]
        id=raw.id,
        principal_id=raw.principal_id,
        namespace=raw.namespace,
        content=raw.content,
        tags=raw.tags,
        importance=raw.importance,
        embedding=None,
        metadata=raw.metadata,
        created_at=raw.created_at,
        updated_at=raw.updated_at,
    )
    results = store.recall(RecallOptions(principal_id="user-1", query="apple"))
    assert results == []
