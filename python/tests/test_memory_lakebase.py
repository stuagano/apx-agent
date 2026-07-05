"""Tests for _memory_lakebase.py — Lakebase pgvector-backed MemoryStore.

SQLite cannot run pgvector / TEXT[] / JSONB / vector() DDL, so we use a
recording fake Engine that captures the SQL strings and bind parameters
produced by the store and returns canned rows. Tests assert on the SQL
shape, bind values, vector serialization, and ordering of operations.

Covers:
  * Constructor validation (embedding_fn / embedding_dim required).
  * Auto-create DDL emits the right statements once.
  * Add / add_batch round-trip params correctly and embed content.
  * Update fetches the existing row, applies the patch, re-embeds on
    content change.
  * Delete returns True/False based on rowcount.
  * Get / list / recall emit the correct SQL, params, and filter guards.
  * Vector literal serialization is ``[v1,v2,...]`` shape.
  * ``ensure_extension=False`` suppresses ``CREATE EXTENSION``.
  * ``auto_create=False`` suppresses DDL entirely.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NamedTuple

import pytest

pytest.importorskip("sqlalchemy")

from apx_agent._memory_lakebase import (  # noqa: E402
    LakebaseMemoryStore,
    _epoch_to_iso,
    _iso_to_epoch,
    _row_to_memory,
    _vector_literal,
)
from apx_agent._memory import MemoryFilter, RecallOptions  # noqa: E402


# ---------------------------------------------------------------------------
# Recording fake Engine
# ---------------------------------------------------------------------------


class _FakeResult:
    """Stand-in for SQLAlchemy's CursorResult."""

    def __init__(self, rows: list[dict[str, Any]], rowcount: int = -1) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def mappings(self) -> "_FakeResult":
        return self

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _FakeConn:
    """Stand-in for SQLAlchemy Connection — records execute() calls."""

    def __init__(self, engine: "RecordingEngine") -> None:
        self._engine = engine

    def execute(self, sql_obj: Any, params: dict[str, Any] | None = None):
        sql_text = str(getattr(sql_obj, "text", sql_obj))
        self._engine.calls.append((sql_text, params or {}))
        return self._engine.next_result(sql_text)

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class RecordingEngine:
    """Fake SQLAlchemy Engine that records SQL + params and returns canned rows.

    ``rows_to_return`` is a list of canned result-sets — one per
    ``execute()`` call. Each entry is a list of dicts representing rows, or
    a tuple ``(rows, rowcount)`` when the test cares about rowcount.
    """

    def __init__(
        self,
        rows_to_return: list[list[dict[str, Any]] | tuple[list[dict[str, Any]], int]]
        | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._rows = list(rows_to_return or [])

    def next_result(self, _sql: str) -> _FakeResult:
        if not self._rows:
            return _FakeResult([])
        nxt = self._rows.pop(0)
        if isinstance(nxt, tuple):
            return _FakeResult(nxt[0], rowcount=nxt[1])
        return _FakeResult(nxt)

    def begin(self) -> _FakeConn:
        return _FakeConn(self)

    def connect(self) -> _FakeConn:
        return _FakeConn(self)


# ---------------------------------------------------------------------------
# Embedder helper
# ---------------------------------------------------------------------------


class _EmbedderPair(NamedTuple):
    calls: list[list[str]]
    fn: Any


def make_recording_embedder() -> _EmbedderPair:
    """Returns an _EmbedderPair(calls_log, embedder_fn). Embedder produces fixed [1,0,0]."""
    calls: list[list[str]] = []

    def fn(texts: Sequence[str]) -> list[list[float]]:
        calls.append(list(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]

    return _EmbedderPair(calls=calls, fn=fn)


def _store(
    engine: RecordingEngine,
    *,
    embedding_fn: Any | None = None,
    table_name: str = "apx_memories",
    auto_create: bool = True,
    ensure_extension: bool = True,
    embedding_dim: int = 3,
) -> LakebaseMemoryStore:
    if embedding_fn is None:
        _, embedding_fn = make_recording_embedder()
    return LakebaseMemoryStore(
        engine=engine,
        embedding_fn=embedding_fn,
        embedding_dim=embedding_dim,
        table_name=table_name,
        auto_create=auto_create,
        ensure_extension=ensure_extension,
    )


# ===========================================================================
# Constructor validation
# ===========================================================================


def test_constructor_requires_embedding_fn() -> None:
    with pytest.raises(ValueError, match="embedding_fn"):
        LakebaseMemoryStore(
            engine=RecordingEngine(),
            embedding_fn=None,  # type: ignore[arg-type]
            embedding_dim=3,
        )


def test_constructor_requires_positive_embedding_dim() -> None:
    _, fn = make_recording_embedder()
    with pytest.raises(ValueError, match="embedding_dim"):
        LakebaseMemoryStore(engine=RecordingEngine(), embedding_fn=fn, embedding_dim=0)
    with pytest.raises(ValueError, match="embedding_dim"):
        LakebaseMemoryStore(engine=RecordingEngine(), embedding_fn=fn, embedding_dim=-5)


# ===========================================================================
# DDL / ensure_schema
# ===========================================================================


def test_ensure_schema_emits_extension_and_table_and_indexes() -> None:
    engine = RecordingEngine()
    store = _store(engine)
    store.add({"principal_id": "u", "content": "x"})
    ddl_calls = [c for c in engine.calls if "CREATE" in c[0]]
    sqls = " ".join(c[0] for c in ddl_calls)
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sqls
    assert "CREATE TABLE IF NOT EXISTS apx_memories" in sqls
    assert "vector(3)" in sqls
    assert "TEXT[]" in sqls
    assert "JSONB" in sqls
    assert "apx_memories_principal_idx" in sqls
    assert "apx_memories_embedding_idx" in sqls
    assert "ivfflat" in sqls


def test_ensure_schema_runs_only_once() -> None:
    engine = RecordingEngine()
    store = _store(engine)
    store.add({"principal_id": "u", "content": "x"})
    initial_ddl = sum(1 for c in engine.calls if "CREATE" in c[0])
    store.add({"principal_id": "u", "content": "y"})
    later_ddl = sum(1 for c in engine.calls if "CREATE" in c[0])
    assert initial_ddl == later_ddl  # No new CREATE statements emitted


def test_ensure_extension_false_skips_extension_ddl() -> None:
    engine = RecordingEngine()
    store = _store(engine, ensure_extension=False)
    store.add({"principal_id": "u", "content": "x"})
    sqls = " ".join(c[0] for c in engine.calls)
    assert "CREATE EXTENSION" not in sqls
    assert "CREATE TABLE" in sqls


def test_auto_create_false_skips_all_ddl() -> None:
    engine = RecordingEngine()
    store = _store(engine, auto_create=False)
    store.add({"principal_id": "u", "content": "x"})
    sqls = " ".join(c[0] for c in engine.calls)
    assert "CREATE EXTENSION" not in sqls
    assert "CREATE TABLE" not in sqls


def test_custom_table_name_is_used_in_ddl_and_dml() -> None:
    engine = RecordingEngine()
    store = _store(engine, table_name="custom_mem")
    store.add({"principal_id": "u", "content": "x"})
    sqls = " ".join(c[0] for c in engine.calls)
    assert "CREATE TABLE IF NOT EXISTS custom_mem" in sqls
    assert "INSERT INTO custom_mem" in sqls


# ===========================================================================
# Vector literal serialization
# ===========================================================================


def test_vector_literal_format() -> None:
    assert _vector_literal([1.0, 2.5, -3.0]) == "[1.0,2.5,-3.0]"
    assert _vector_literal(None) is None
    assert _vector_literal([]) == "[]"


def test_iso_epoch_round_trip() -> None:
    iso = "2026-05-19T12:34:56.789Z"
    epoch = _iso_to_epoch(iso)
    assert epoch > 0
    iso2 = _epoch_to_iso(epoch)
    assert iso2.startswith("2026-05-19T12:34:56")


def test_iso_to_epoch_handles_garbage() -> None:
    assert _iso_to_epoch("") == 0.0
    assert _iso_to_epoch("not-a-date") == 0.0


# ===========================================================================
# CRUD: add / add_batch
# ===========================================================================


def test_add_embeds_content_and_inserts() -> None:
    engine = RecordingEngine()
    calls, fn = make_recording_embedder()
    store = _store(engine, embedding_fn=fn)
    m = store.add({"principal_id": "u", "content": "hello"})
    # Embedder saw the content
    assert calls[0] == ["hello"]
    assert m.embedding == (1.0, 0.0, 0.0)
    # An INSERT INTO ran with the expected params
    inserts = [c for c in engine.calls if "INSERT INTO apx_memories" in c[0]]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert "CAST(:embedding AS vector)" in sql
    assert "CAST(:metadata AS jsonb)" in sql
    assert params["principal_id"] == "u"
    assert params["content"] == "hello"
    assert params["embedding"] == "[1.0,0.0,0.0]"
    assert params["tags"] == []
    assert params["namespace"] == "default"
    assert params["importance"] == 0.5


def test_add_batch_embeds_in_one_call() -> None:
    engine = RecordingEngine()
    calls, fn = make_recording_embedder()
    store = _store(engine, embedding_fn=fn)
    ms = store.add_batch(
        [
            {"principal_id": "u", "content": "a"},
            {"principal_id": "u", "content": "b"},
        ]
    )
    assert len(ms) == 2
    # One embedder call with both contents
    assert len(calls) == 1
    assert calls[0] == ["a", "b"]
    inserts = [c for c in engine.calls if "INSERT INTO apx_memories" in c[0]]
    assert len(inserts) == 2


def test_add_batch_empty_short_circuits() -> None:
    engine = RecordingEngine()
    calls, fn = make_recording_embedder()
    store = _store(engine, embedding_fn=fn)
    assert store.add_batch([]) == []
    # Embedder never called and no INSERT ran
    assert calls == []
    assert not any("INSERT INTO" in c[0] for c in engine.calls)


# ===========================================================================
# CRUD: get / update / delete
# ===========================================================================


def _canned_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "mem_1",
        "principal_id": "u",
        "namespace": "default",
        "content": "hi",
        "tags": ["t1"],
        "importance": 0.5,
        "embedding": "[1.0,0.0,0.0]",
        "metadata": "{}",
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_000_001.0,
    }
    base.update(overrides)
    return base


def test_get_returns_row_when_found() -> None:
    engine = RecordingEngine(rows_to_return=[[_canned_row()]])
    store = _store(engine, auto_create=False)
    m = store.get("mem_1")
    assert m is not None
    assert m.id == "mem_1"
    assert m.tags == ("t1",)
    assert m.embedding == (1.0, 0.0, 0.0)
    sel = [c for c in engine.calls if "SELECT" in c[0]]
    assert sel[0][1] == {"id": "mem_1"}


def test_get_returns_none_when_missing() -> None:
    engine = RecordingEngine(rows_to_return=[[]])
    store = _store(engine, auto_create=False)
    assert store.get("nope") is None


def test_update_returns_none_when_existing_missing() -> None:
    engine = RecordingEngine(rows_to_return=[[]])
    store = _store(engine, auto_create=False)
    assert store.update("nope", {"content": "x"}) is None


def test_update_re_embeds_when_content_changes() -> None:
    # First execute is the SELECT in get(); second is the UPDATE (rowcount 1 = hit).
    engine = RecordingEngine(rows_to_return=[[_canned_row(content="old")], ([], 1)])
    calls, fn = make_recording_embedder()
    store = _store(engine, embedding_fn=fn, auto_create=False)
    updated = store.update("mem_1", {"content": "new"})
    assert updated is not None
    assert updated.content == "new"
    assert calls == [["new"]]
    # A conditional UPDATE, not an upsert INSERT — so it can't resurrect (#485).
    updates = [c for c in engine.calls if c[0].strip().startswith("UPDATE")]
    assert len(updates) == 1
    assert updates[0][1]["content"] == "new"
    assert not any("INSERT INTO" in c[0] for c in engine.calls)


def test_update_no_reembed_when_content_unchanged() -> None:
    engine = RecordingEngine(rows_to_return=[[_canned_row()], ([], 1)])
    calls, fn = make_recording_embedder()
    store = _store(engine, embedding_fn=fn, auto_create=False)
    store.update("mem_1", {"importance": 0.9})
    # Embedder never called for an unchanged-content patch
    assert calls == []


def test_update_returns_none_when_row_deleted_concurrently() -> None:
    # #485: get() sees the row, but the UPDATE affects 0 rows (a concurrent
    # forget deleted it) → return None instead of resurrecting via upsert.
    engine = RecordingEngine(rows_to_return=[[_canned_row()], ([], 0)])
    store = _store(engine, auto_create=False)
    assert store.update("mem_1", {"importance": 0.9}) is None
    assert not any("INSERT INTO" in c[0] for c in engine.calls)


def test_delete_returns_true_on_rowcount_positive() -> None:
    engine = RecordingEngine(rows_to_return=[([], 1)])
    store = _store(engine, auto_create=False)
    assert store.delete("mem_1") is True


def test_delete_returns_false_on_rowcount_zero() -> None:
    engine = RecordingEngine(rows_to_return=[([], 0)])
    store = _store(engine, auto_create=False)
    assert store.delete("nope") is False


class _RaisingEngine:
    """Engine whose every execute raises — simulates an infra failure."""

    class _Conn:
        def execute(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("warehouse unreachable")

        def __enter__(self) -> "_RaisingEngine._Conn":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    def connect(self) -> "_RaisingEngine._Conn":
        return self._Conn()

    def begin(self) -> "_RaisingEngine._Conn":
        return self._Conn()


def test_get_propagates_infra_error_not_none() -> None:
    # #486: a transient failure must NOT masquerade as "not found" (None) —
    # callers like update/delete/forget would misread it as a genuine miss.
    store = _store(_RaisingEngine(), auto_create=False)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="warehouse unreachable"):
        store.get("mem_1")


def test_delete_propagates_infra_error_not_false() -> None:
    # #486: False must mean "wasn't there", never "the delete failed".
    store = _store(_RaisingEngine(), auto_create=False)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="warehouse unreachable"):
        store.delete("mem_1")


# ===========================================================================
# list / recall — SQL guards
# ===========================================================================


def test_list_emits_principal_filter_and_order() -> None:
    engine = RecordingEngine(rows_to_return=[[_canned_row()]])
    store = _store(engine, auto_create=False)
    rows = store.list(MemoryFilter(principal_id="u"))
    assert len(rows) == 1
    sql, params = engine.calls[-1]
    assert "WHERE principal_id = :principal_id" in sql
    assert "ORDER BY updated_at DESC" in sql
    assert "LIMIT :limit" in sql
    assert params == {"principal_id": "u", "limit": 100}


def test_list_emits_namespace_tags_min_importance_guards() -> None:
    engine = RecordingEngine(rows_to_return=[[]])
    store = _store(engine, auto_create=False)
    store.list(
        MemoryFilter(
            principal_id="u",
            namespace="profile",
            tags=("a", "b"),
            min_importance=0.4,
        )
    )
    sql, params = engine.calls[-1]
    assert "namespace = :namespace" in sql
    assert "tags && CAST(:tags AS TEXT[])" in sql
    assert "importance >= :min_importance" in sql
    assert params["namespace"] == "profile"
    assert params["tags"] == ["a", "b"]
    assert params["min_importance"] == 0.4


def test_recall_orders_by_distance_and_returns_similarity_score() -> None:
    engine = RecordingEngine(
        rows_to_return=[[_canned_row(id="r1") | {"score": 0.92}]],
    )
    calls, fn = make_recording_embedder()
    store = _store(engine, embedding_fn=fn, auto_create=False)
    results = store.recall(RecallOptions(principal_id="u", query="apple", k=5))
    assert len(results) == 1
    assert results[0].score == pytest.approx(0.92)
    assert results[0].memory.id == "r1"
    # Query was embedded
    assert calls == [["apple"]]
    sql, params = engine.calls[-1]
    assert "embedding <=> CAST(:query_vec AS vector)" in sql
    assert "ORDER BY embedding <=> CAST(:query_vec AS vector) ASC" in sql
    assert "embedding IS NOT NULL" in sql
    assert params["k"] == 5
    assert params["query_vec"] == "[1.0,0.0,0.0]"


def test_recall_applies_namespace_and_tag_filters() -> None:
    engine = RecordingEngine(rows_to_return=[[]])
    _, fn = make_recording_embedder()
    store = _store(engine, embedding_fn=fn, auto_create=False)
    store.recall(
        RecallOptions(
            principal_id="u",
            query="q",
            namespace="profile",
            tags=("a",),
            min_importance=0.5,
        )
    )
    sql, params = engine.calls[-1]
    assert "namespace = :namespace" in sql
    assert "tags && CAST(:tags AS TEXT[])" in sql
    assert "importance >= :min_importance" in sql
    assert params["namespace"] == "profile"
    assert params["tags"] == ["a"]


# ===========================================================================
# _row_to_memory
# ===========================================================================


def test_row_to_memory_handles_string_metadata() -> None:
    row = _canned_row(metadata='{"k": 1}')
    m = _row_to_memory(row)
    assert m.metadata == {"k": 1}


def test_row_to_memory_handles_malformed_metadata() -> None:
    row = _canned_row(metadata="not-json")
    m = _row_to_memory(row)
    assert m.metadata == {}


def test_row_to_memory_handles_dict_metadata() -> None:
    row = _canned_row(metadata={"a": 2})
    m = _row_to_memory(row)
    assert m.metadata == {"a": 2}


def test_row_to_memory_parses_pgvector_string() -> None:
    row = _canned_row(embedding="[0.1,0.2,0.3]")
    m = _row_to_memory(row)
    assert m.embedding == (0.1, 0.2, 0.3)


def test_row_to_memory_handles_list_embedding() -> None:
    row = _canned_row(embedding=[0.1, 0.2])
    m = _row_to_memory(row)
    assert m.embedding == (0.1, 0.2)


def test_row_to_memory_handles_null_embedding() -> None:
    row = _canned_row(embedding=None)
    m = _row_to_memory(row)
    assert m.embedding is None
