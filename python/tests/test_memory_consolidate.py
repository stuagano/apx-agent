"""Tests for ``_memory_consolidate`` — mirror of the TS suite."""

from __future__ import annotations

import time
from collections.abc import Sequence

from apx_agent._memory import InMemoryMemoryStore, Memory, MemoryFilter
from apx_agent._memory_consolidate import consolidate_memories


def _seed_many(
    store: InMemoryMemoryStore,
    principal_id: str,
    count: int,
    *,
    namespace: str | None = None,
    importance: float | None = None,
) -> list[Memory]:
    out: list[Memory] = []
    for i in range(count):
        payload: dict[str, object] = {
            "principal_id": principal_id,
            "content": f"memo-{i}",
        }
        if namespace is not None:
            payload["namespace"] = namespace
        if importance is not None:
            payload["importance"] = importance
        out.append(store.add(payload))
    return out


class TestConsolidate:
    def test_writes_and_deletes_originals(self) -> None:
        store = InMemoryMemoryStore()
        _seed_many(store, "user-1", 5)
        result = consolidate_memories(
            store=store,
            principal_id="user-1",
            summarize_fn=lambda _mems: "summary",
            min_memories_for_consolidation=5,
        )
        assert result.candidates_found == 5
        assert result.consolidated_memory is not None
        assert result.consolidated_memory.content == "summary"
        assert len(result.deleted_ids) == 5
        remaining = store.list(MemoryFilter(principal_id="user-1"))
        assert len(remaining) == 1
        assert remaining[0].namespace == "consolidated"

    def test_short_circuit_below_min(self) -> None:
        store = InMemoryMemoryStore()
        _seed_many(store, "user-1", 3)

        def summarize(_mems: Sequence[Memory]) -> str:  # pragma: no cover - not called
            raise AssertionError("summarize should not be called")

        result = consolidate_memories(
            store=store,
            principal_id="user-1",
            summarize_fn=summarize,
            min_memories_for_consolidation=5,
        )
        assert result.candidates_found == 3
        assert result.consolidated_memory is None
        assert result.deleted_ids == ()
        assert len(store.list(MemoryFilter(principal_id="user-1"))) == 3

    def test_keep_originals_true(self) -> None:
        store = InMemoryMemoryStore()
        _seed_many(store, "user-1", 5)
        result = consolidate_memories(
            store=store,
            principal_id="user-1",
            summarize_fn=lambda _m: "summary",
            keep_originals=True,
        )
        assert result.deleted_ids == ()
        assert len(store.list(MemoryFilter(principal_id="user-1"))) == 6

    def test_min_importance_filters(self) -> None:
        store = InMemoryMemoryStore()
        _seed_many(store, "user-1", 3, importance=0.2)
        _seed_many(store, "user-1", 5, importance=0.9)
        result = consolidate_memories(
            store=store,
            principal_id="user-1",
            summarize_fn=lambda _m: "summary",
            min_importance=0.5,
            min_memories_for_consolidation=5,
        )
        assert result.candidates_found == 5

    def test_namespace_scopes(self) -> None:
        store = InMemoryMemoryStore()
        _seed_many(store, "user-1", 5, namespace="episodic")
        _seed_many(store, "user-1", 5, namespace="profile")
        result = consolidate_memories(
            store=store,
            principal_id="user-1",
            namespace="episodic",
            summarize_fn=lambda _m: "summary",
        )
        assert result.candidates_found == 5
        assert result.consolidated_memory is not None
        assert result.consolidated_memory.namespace == "consolidated"

    def test_dry_run_no_writes(self) -> None:
        store = InMemoryMemoryStore()
        originals = _seed_many(store, "user-1", 5)
        result = consolidate_memories(
            store=store,
            principal_id="user-1",
            summarize_fn=lambda _m: "summary",
            dry_run=True,
        )
        assert result.consolidated_memory is not None
        assert result.deleted_ids == ()
        remaining = store.list(MemoryFilter(principal_id="user-1"))
        assert len(remaining) == 5
        assert {m.id for m in remaining} == {m.id for m in originals}

    def test_summarize_fn_receives_candidates(self) -> None:
        store = InMemoryMemoryStore()
        seeded = _seed_many(store, "user-1", 5)
        captured: list[Memory] = []

        def summarize(mems: Sequence[Memory]) -> str:
            captured.extend(mems)
            return "summary"

        consolidate_memories(
            store=store,
            principal_id="user-1",
            summarize_fn=summarize,
        )
        assert {m.id for m in captured} == {m.id for m in seeded}

    def test_metadata_carries_source_ids(self) -> None:
        store = InMemoryMemoryStore()
        seeded = _seed_many(store, "user-1", 5)
        result = consolidate_memories(
            store=store,
            principal_id="user-1",
            summarize_fn=lambda _m: "summary",
            keep_originals=True,
        )
        m = result.consolidated_memory
        assert m is not None
        assert m.metadata["count"] == 5
        assert set(m.metadata["consolidated_from"]) == {x.id for x in seeded}

    def test_consolidated_tags_namespace_importance_respected(self) -> None:
        store = InMemoryMemoryStore()
        _seed_many(store, "user-1", 5)
        result = consolidate_memories(
            store=store,
            principal_id="user-1",
            summarize_fn=lambda _m: "summary",
            consolidated_namespace="rollup",
            consolidated_tags=("rollup", "auto"),
            consolidated_importance=0.4,
        )
        m = result.consolidated_memory
        assert m is not None
        assert m.namespace == "rollup"
        assert m.tags == ("rollup", "auto")
        assert m.importance == 0.4

    def test_max_age_seconds_drops_fresh(self) -> None:
        store = InMemoryMemoryStore()
        _seed_many(store, "user-1", 5)
        result = consolidate_memories(
            store=store,
            principal_id="user-1",
            summarize_fn=lambda _m: "summary",
            max_age_seconds=3600,
            min_memories_for_consolidation=1,
        )
        assert result.candidates_found == 0
        assert result.consolidated_memory is None

    def test_max_age_zero_admits_all(self) -> None:
        store = InMemoryMemoryStore()
        _seed_many(store, "user-1", 5)
        # Sleep briefly so updated_at < now.
        time.sleep(0.01)
        result = consolidate_memories(
            store=store,
            principal_id="user-1",
            summarize_fn=lambda _m: "summary",
            max_age_seconds=0.0,
        )
        assert result.candidates_found == 5

    def test_principal_scoping(self) -> None:
        store = InMemoryMemoryStore()
        _seed_many(store, "user-1", 5)
        _seed_many(store, "user-2", 5)
        result = consolidate_memories(
            store=store,
            principal_id="user-1",
            summarize_fn=lambda _m: "summary",
        )
        assert result.candidates_found == 5
        assert len(store.list(MemoryFilter(principal_id="user-2"))) == 5

    def test_does_not_touch_other_principals(self) -> None:
        store = InMemoryMemoryStore()
        _seed_many(store, "user-1", 5)
        _seed_many(store, "user-2", 3)
        consolidate_memories(
            store=store,
            principal_id="user-1",
            summarize_fn=lambda _m: "summary",
        )
        assert len(store.list(MemoryFilter(principal_id="user-2"))) == 3

    def test_dry_run_id_has_mem_prefix(self) -> None:
        store = InMemoryMemoryStore()
        _seed_many(store, "user-1", 5)
        result = consolidate_memories(
            store=store,
            principal_id="user-1",
            summarize_fn=lambda _m: "summary",
            dry_run=True,
        )
        assert result.consolidated_memory is not None
        assert result.consolidated_memory.id.startswith("mem_")
