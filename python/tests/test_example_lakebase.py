"""Tests for _example_lakebase.py — Lakebase pgvector-backed ExampleStore.

Uses the same recording-fake Engine pattern as test_memory_lakebase. SQLite
can't execute the real pgvector / TEXT[] / JSONB / vector() DDL, so we
assert on the SQL strings, bind params, and ordering of operations instead.

Covers:
  * Constructor validation.
  * Auto-create DDL emits the right statements once.
  * Add / add_batch embed the ``input`` field (not the output).
  * Update fetches, applies the patch, re-embeds on input change.
  * Delete returns True/False based on rowcount.
  * Get / list / find_similar emit the correct SQL, params, filter guards.
  * Custom table name and ensure_extension flag respected.
  * Row deserialization handles JSONB string, list, and dict metadata.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

pytest.importorskip("sqlalchemy")

from apx_agent._example_lakebase import (  # noqa: E402
    LakebaseExampleStore,
    _row_to_example,
)
from apx_agent._example import ExampleFilter, FindSimilarOptions  # noqa: E402


# ---------------------------------------------------------------------------
# Recording fake Engine (copied to keep the test file self-contained)
# ---------------------------------------------------------------------------


class _FakeResult:
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
# Embedder + store helpers
# ---------------------------------------------------------------------------


def make_recording_embedder() -> tuple[list[list[str]], Any]:
    calls: list[list[str]] = []

    def fn(texts: Sequence[str]) -> list[list[float]]:
        calls.append(list(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]

    return calls, fn


def _store(
    engine: RecordingEngine,
    *,
    embedding_fn: Any | None = None,
    table_name: str = "apx_examples",
    auto_create: bool = True,
    ensure_extension: bool = True,
    embedding_dim: int = 3,
) -> LakebaseExampleStore:
    if embedding_fn is None:
        _, embedding_fn = make_recording_embedder()
    return LakebaseExampleStore(
        engine=engine,
        embedding_fn=embedding_fn,
        embedding_dim=embedding_dim,
        table_name=table_name,
        auto_create=auto_create,
        ensure_extension=ensure_extension,
    )


def _canned_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "ex_1",
        "agent_id": "triage",
        "intent": "default",
        "input": "why is my bill so high",
        "output": "let me check your usage",
        "score": 0.7,
        "tags": ["t1"],
        "embedding": "[1.0,0.0,0.0]",
        "metadata": "{}",
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_000_001.0,
    }
    base.update(overrides)
    return base


# ===========================================================================
# Constructor validation
# ===========================================================================


def test_constructor_requires_embedding_fn() -> None:
    with pytest.raises(ValueError, match="embedding_fn"):
        LakebaseExampleStore(
            engine=RecordingEngine(),
            embedding_fn=None,  # type: ignore[arg-type]
            embedding_dim=3,
        )


def test_constructor_requires_positive_embedding_dim() -> None:
    _, fn = make_recording_embedder()
    with pytest.raises(ValueError, match="embedding_dim"):
        LakebaseExampleStore(
            engine=RecordingEngine(), embedding_fn=fn, embedding_dim=0
        )


# ===========================================================================
# DDL
# ===========================================================================


def test_ensure_schema_emits_extension_table_and_indexes() -> None:
    engine = RecordingEngine()
    store = _store(engine)
    store.add({"agent_id": "a", "input": "q", "output": "o"})
    sqls = " ".join(c[0] for c in engine.calls if "CREATE" in c[0])
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sqls
    assert "CREATE TABLE IF NOT EXISTS apx_examples" in sqls
    assert "vector(3)" in sqls
    assert "TEXT[]" in sqls
    assert "JSONB" in sqls
    assert "apx_examples_agent_idx" in sqls
    assert "apx_examples_embedding_idx" in sqls
    assert "ivfflat" in sqls


def test_ensure_schema_runs_only_once() -> None:
    engine = RecordingEngine()
    store = _store(engine)
    store.add({"agent_id": "a", "input": "q1", "output": "o"})
    initial_ddl = sum(1 for c in engine.calls if "CREATE" in c[0])
    store.add({"agent_id": "a", "input": "q2", "output": "o"})
    later_ddl = sum(1 for c in engine.calls if "CREATE" in c[0])
    assert initial_ddl == later_ddl


def test_ensure_extension_false_skips_extension_ddl() -> None:
    engine = RecordingEngine()
    store = _store(engine, ensure_extension=False)
    store.add({"agent_id": "a", "input": "q", "output": "o"})
    sqls = " ".join(c[0] for c in engine.calls)
    assert "CREATE EXTENSION" not in sqls
    assert "CREATE TABLE" in sqls


def test_auto_create_false_skips_all_ddl() -> None:
    engine = RecordingEngine()
    store = _store(engine, auto_create=False)
    store.add({"agent_id": "a", "input": "q", "output": "o"})
    sqls = " ".join(c[0] for c in engine.calls)
    assert "CREATE" not in sqls


def test_custom_table_name_is_used() -> None:
    engine = RecordingEngine()
    store = _store(engine, table_name="few_shots")
    store.add({"agent_id": "a", "input": "q", "output": "o"})
    sqls = " ".join(c[0] for c in engine.calls)
    assert "CREATE TABLE IF NOT EXISTS few_shots" in sqls
    assert "INSERT INTO few_shots" in sqls


# ===========================================================================
# Add / add_batch
# ===========================================================================


def test_add_embeds_input_not_output() -> None:
    engine = RecordingEngine()
    calls, fn = make_recording_embedder()
    store = _store(engine, embedding_fn=fn)
    ex = store.add({"agent_id": "a", "input": "IN", "output": "OUT"})
    assert calls[0] == ["IN"]
    assert ex.embedding == (1.0, 0.0, 0.0)
    inserts = [c for c in engine.calls if "INSERT INTO apx_examples" in c[0]]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert "CAST(:embedding AS vector)" in sql
    assert "CAST(:metadata AS jsonb)" in sql
    assert params["agent_id"] == "a"
    assert params["input"] == "IN"
    assert params["output"] == "OUT"
    assert params["embedding"] == "[1.0,0.0,0.0]"
    assert params["intent"] == "default"


def test_add_batch_embeds_inputs_only_in_one_call() -> None:
    engine = RecordingEngine()
    calls, fn = make_recording_embedder()
    store = _store(engine, embedding_fn=fn)
    exs = store.add_batch(
        [
            {"agent_id": "a", "input": "q1", "output": "o1"},
            {"agent_id": "a", "input": "q2", "output": "o2"},
        ]
    )
    assert len(exs) == 2
    assert calls == [["q1", "q2"]]


def test_add_batch_empty_short_circuits() -> None:
    engine = RecordingEngine()
    calls, fn = make_recording_embedder()
    store = _store(engine, embedding_fn=fn)
    assert store.add_batch([]) == []
    assert calls == []


# ===========================================================================
# Get / update / delete
# ===========================================================================


def test_get_returns_row_when_found() -> None:
    engine = RecordingEngine(rows_to_return=[[_canned_row()]])
    store = _store(engine, auto_create=False)
    ex = store.get("ex_1")
    assert ex is not None
    assert ex.id == "ex_1"
    assert ex.score == 0.7
    assert ex.tags == ("t1",)


def test_get_returns_none_when_missing() -> None:
    engine = RecordingEngine(rows_to_return=[[]])
    store = _store(engine, auto_create=False)
    assert store.get("nope") is None


def test_update_returns_none_when_existing_missing() -> None:
    engine = RecordingEngine(rows_to_return=[[]])
    store = _store(engine, auto_create=False)
    assert store.update("nope", {"output": "x"}) is None


def test_update_re_embeds_when_input_changes() -> None:
    engine = RecordingEngine(rows_to_return=[[_canned_row(input="old")]])
    calls, fn = make_recording_embedder()
    store = _store(engine, embedding_fn=fn, auto_create=False)
    updated = store.update("ex_1", {"input": "new"})
    assert updated is not None
    assert updated.input == "new"
    assert calls == [["new"]]
    inserts = [c for c in engine.calls if "INSERT INTO" in c[0]]
    assert len(inserts) == 1
    assert inserts[0][1]["input"] == "new"


def test_update_no_reembed_when_input_unchanged() -> None:
    engine = RecordingEngine(rows_to_return=[[_canned_row()]])
    calls, fn = make_recording_embedder()
    store = _store(engine, embedding_fn=fn, auto_create=False)
    store.update("ex_1", {"output": "new"})
    assert calls == []


def test_delete_returns_true_on_rowcount_positive() -> None:
    engine = RecordingEngine(rows_to_return=[([], 1)])
    store = _store(engine, auto_create=False)
    assert store.delete("ex_1") is True


def test_delete_returns_false_on_rowcount_zero() -> None:
    engine = RecordingEngine(rows_to_return=[([], 0)])
    store = _store(engine, auto_create=False)
    assert store.delete("nope") is False


# ===========================================================================
# list / find_similar — SQL guards
# ===========================================================================


def test_list_emits_agent_filter_and_order() -> None:
    engine = RecordingEngine(rows_to_return=[[_canned_row()]])
    store = _store(engine, auto_create=False)
    rows = store.list(ExampleFilter(agent_id="triage"))
    assert len(rows) == 1
    sql, params = engine.calls[-1]
    assert "WHERE agent_id = :agent_id" in sql
    assert "ORDER BY updated_at DESC" in sql
    assert "LIMIT :limit" in sql
    assert params == {"agent_id": "triage", "limit": 100}


def test_list_emits_intent_tags_min_score_guards() -> None:
    engine = RecordingEngine(rows_to_return=[[]])
    store = _store(engine, auto_create=False)
    store.list(
        ExampleFilter(
            agent_id="triage", intent="classify", tags=("a", "b"), min_score=0.5
        )
    )
    sql, params = engine.calls[-1]
    assert "intent = :intent" in sql
    assert "tags && CAST(:tags AS TEXT[])" in sql
    assert "score IS NOT NULL AND score >= :min_score" in sql
    assert params["intent"] == "classify"
    assert params["tags"] == ["a", "b"]
    assert params["min_score"] == 0.5


def test_find_similar_orders_by_distance_returns_similarity_score() -> None:
    engine = RecordingEngine(
        rows_to_return=[[_canned_row(id="r1") | {"sim": 0.88}]],
    )
    calls, fn = make_recording_embedder()
    store = _store(engine, embedding_fn=fn, auto_create=False)
    results = store.find_similar(
        FindSimilarOptions(agent_id="triage", query="apple", k=3)
    )
    assert len(results) == 1
    assert results[0].score == pytest.approx(0.88)
    assert results[0].example.id == "r1"
    assert calls == [["apple"]]
    sql, params = engine.calls[-1]
    assert "ORDER BY embedding <=> CAST(:query_vec AS vector) ASC" in sql
    assert "embedding IS NOT NULL" in sql
    assert params["k"] == 3
    assert params["query_vec"] == "[1.0,0.0,0.0]"


def test_find_similar_applies_intent_and_tag_filters() -> None:
    engine = RecordingEngine(rows_to_return=[[]])
    _, fn = make_recording_embedder()
    store = _store(engine, embedding_fn=fn, auto_create=False)
    store.find_similar(
        FindSimilarOptions(
            agent_id="triage",
            query="q",
            intent="classify",
            tags=("a",),
            min_score=0.4,
        )
    )
    sql, params = engine.calls[-1]
    assert "intent = :intent" in sql
    assert "tags && CAST(:tags AS TEXT[])" in sql
    assert "score IS NOT NULL AND score >= :min_score" in sql
    assert params["intent"] == "classify"
    assert params["tags"] == ["a"]


# ===========================================================================
# _row_to_example
# ===========================================================================


def test_row_to_example_handles_string_metadata() -> None:
    ex = _row_to_example(_canned_row(metadata='{"k": 1}'))
    assert ex.metadata == {"k": 1}


def test_row_to_example_handles_malformed_metadata() -> None:
    ex = _row_to_example(_canned_row(metadata="not-json"))
    assert ex.metadata == {}


def test_row_to_example_handles_dict_metadata() -> None:
    ex = _row_to_example(_canned_row(metadata={"a": 2}))
    assert ex.metadata == {"a": 2}


def test_row_to_example_parses_pgvector_string() -> None:
    ex = _row_to_example(_canned_row(embedding="[0.1,0.2,0.3]"))
    assert ex.embedding == (0.1, 0.2, 0.3)


def test_row_to_example_handles_list_embedding() -> None:
    ex = _row_to_example(_canned_row(embedding=[0.1, 0.2]))
    assert ex.embedding == (0.1, 0.2)


def test_row_to_example_handles_null_embedding() -> None:
    ex = _row_to_example(_canned_row(embedding=None))
    assert ex.embedding is None


def test_row_to_example_score_optional() -> None:
    ex = _row_to_example(_canned_row(score=None))
    assert ex.score is None
