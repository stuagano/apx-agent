"""Tests for ``apx_agent._memory_delta`` — Delta-backed ``MemoryStore``.

The store is fully decoupled from the Databricks SDK at runtime by the
injected ``run_sql: Callable[[str], list[dict]]``. We exercise the SQL
shape, MERGE INTO upsert behavior, DDL idempotency, the four-way recall
fallback tree, and the SQL-escaping defense for untrusted input by
recording every SQL string the store emits and returning canned rows.

Test count: 30+. Covers:
  * Construction / validation.
  * Auto-create DDL: idempotent, skipped when ``auto_create=False``.
  * CRUD (add, add_batch, get, update, delete) and MERGE shape.
  * SQL injection escaping in IDs, content, namespaces, and tags.
  * list() WHERE-clause assembly with all filter combos.
  * recall() 4-tier fallback:
      (1) vector_search + embedder → query_vector path.
      (2) vector_search only       → query_text path.
      (3) embedder only            → SQL list + cosine client-side.
      (4) neither                  → recency fallback.
  * VectorSearch filter pass-through (principal_id, namespace).
  * Row-back-to-Memory mapping from Vector Search rows.
  * Score-column fallback chain (_score → score → similarity_score → 0).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import pytest

from apx_agent._memory import MemoryFilter, RecallOptions
from apx_agent._memory_delta import (
    DeltaMemoryStore,
    VectorSearchLike,
    _epoch_to_iso,
    _iso_to_epoch,
    _quote_string,
    _sql_array_float,
    _sql_array_str,
    _validate_table_name,
)


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


def _toy_embedder(texts: Sequence[str]) -> list[list[float]]:
    """Deterministic embedder for tests: 8-d vector based on first char."""
    return [[float(ord(t[0]) - 96) if t else 0.0] * 8 for t in texts]


class RecordingSqlExecutor:
    """Captures every SQL string and dispatches canned rows by pattern."""

    def __init__(
        self,
        patterns: list[tuple[str, list[dict[str, Any]] | type[Exception]]] | None = None,
    ) -> None:
        # Each entry: (regex, rows-or-exception-class). First match wins.
        self.patterns = patterns or []
        self.calls: list[str] = []

    def __call__(self, sql: str) -> list[dict[str, Any]]:
        self.calls.append(sql)
        for pat, ret in self.patterns:
            if re.search(pat, sql, re.IGNORECASE):
                if isinstance(ret, type) and issubclass(ret, Exception):
                    raise ret("simulated SQL failure")
                return list(ret)
        return []


class FakeVectorSearch:
    """Records similarity_search calls; returns canned rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[dict[str, Any]] = []

    def similarity_search(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        return {"rows": list(self.rows)}


# ---------------------------------------------------------------------------
# Helpers / unit tests on SQL escaping
# ---------------------------------------------------------------------------


def test_quote_string_doubles_single_quotes() -> None:
    assert _quote_string("foo") == "'foo'"
    assert _quote_string("o'malley") == "'o''malley'"
    assert _quote_string("' OR 1=1 --") == "''' OR 1=1 --'"


def test_sql_array_str_handles_empty_and_quoted() -> None:
    assert _sql_array_str(None) == "array()"
    assert _sql_array_str([]) == "array()"
    assert _sql_array_str(["a", "b"]) == "array('a', 'b')"
    assert _sql_array_str(["it's"]) == "array('it''s')"


def test_sql_array_float_handles_none_empty_and_nonfinite() -> None:
    assert _sql_array_float(None) == "CAST(NULL AS ARRAY<FLOAT>)"
    assert _sql_array_float([]) == "array()"
    assert _sql_array_float([1.0, 2.5]) == "array(1.0, 2.5)"
    # NaN / inf coerced to 0
    nan = float("nan")
    inf = float("inf")
    assert _sql_array_float([nan, inf, -inf]) == "array(0, 0, 0)"


def test_iso_epoch_roundtrip() -> None:
    iso = "2026-05-19T12:34:56.789Z"
    epoch = _iso_to_epoch(iso)
    back = _epoch_to_iso(epoch)
    # Round-trip preserves seconds-level precision; we just need ordering.
    assert back.startswith("2026-05-19T12:34:56")


def test_iso_to_epoch_garbage_returns_zero() -> None:
    assert _iso_to_epoch("") == 0.0
    assert _iso_to_epoch("not-a-date") == 0.0


def test_validate_table_name_accepts_three_part() -> None:
    _validate_table_name("catalog.schema.memories")
    _validate_table_name("apx_memories")


def test_validate_table_name_rejects_injection() -> None:
    with pytest.raises(ValueError):
        _validate_table_name("memories; DROP TABLE x")
    with pytest.raises(ValueError):
        _validate_table_name("memories'")
    with pytest.raises(ValueError):
        _validate_table_name("")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_constructor_defaults() -> None:
    sql = RecordingSqlExecutor()
    store = DeltaMemoryStore(run_sql=sql)
    assert store.table_name == "apx_memories"
    # Auto-create deferred until first use.
    assert sql.calls == []


def test_constructor_validates_table_name() -> None:
    with pytest.raises(ValueError):
        DeltaMemoryStore(run_sql=lambda s: [], table_name="bad;name")


def test_constructor_requires_index_with_vector_search() -> None:
    with pytest.raises(ValueError):
        DeltaMemoryStore(
            run_sql=lambda s: [],
            vector_search=FakeVectorSearch(),
        )


# ---------------------------------------------------------------------------
# DDL behavior
# ---------------------------------------------------------------------------


def test_auto_create_emits_ddl_once() -> None:
    sql = RecordingSqlExecutor()
    store = DeltaMemoryStore(run_sql=sql, table_name="apx_memories")
    store.add({"principal_id": "u1", "content": "hi"})
    store.add({"principal_id": "u1", "content": "hi again"})
    ddl_calls = [c for c in sql.calls if "CREATE TABLE" in c]
    assert len(ddl_calls) == 1
    assert "USING DELTA" in ddl_calls[0]
    # All canonical columns present.
    for col in (
        "id STRING", "principal_id STRING", "namespace STRING",
        "content STRING", "tags ARRAY<STRING>", "importance FLOAT",
        "embedding ARRAY<FLOAT>", "metadata STRING",
        "created_at DOUBLE", "updated_at DOUBLE",
    ):
        assert col in ddl_calls[0]


def test_auto_create_false_skips_ddl() -> None:
    sql = RecordingSqlExecutor()
    store = DeltaMemoryStore(
        run_sql=sql, auto_create=False, table_name="apx_memories",
    )
    store.add({"principal_id": "u1", "content": "hi"})
    assert not any("CREATE TABLE" in c for c in sql.calls)


def test_auto_create_swallows_ddl_errors(caplog: pytest.LogCaptureFixture) -> None:
    sql = RecordingSqlExecutor(patterns=[("CREATE TABLE", RuntimeError)])
    store = DeltaMemoryStore(run_sql=sql)
    # Should not raise — DDL failure is logged + swallowed.
    store._ensure_table()
    assert any("CREATE TABLE" in c for c in sql.calls)


def test_auto_create_retries_after_create_table_failure() -> None:
    """CREATE TABLE failure must not mark _created=True — next call retries."""
    sql = RecordingSqlExecutor(patterns=[("CREATE TABLE", RuntimeError)])
    store = DeltaMemoryStore(run_sql=sql)
    store._ensure_table()
    assert not store._created, "_created must stay False after CREATE TABLE failure"
    # A second call should still attempt CREATE TABLE
    store._ensure_table()
    create_calls = [c for c in sql.calls if "CREATE TABLE" in c]
    assert len(create_calls) == 2, "Expected two CREATE TABLE attempts"


# ---------------------------------------------------------------------------
# add / add_batch / MERGE
# ---------------------------------------------------------------------------


def test_add_emits_merge_with_embedded_content() -> None:
    sql = RecordingSqlExecutor()
    store = DeltaMemoryStore(run_sql=sql, embedding_fn=_toy_embedder)
    mem = store.add({
        "id": "m1", "principal_id": "u1", "namespace": "profile",
        "content": "hello world", "tags": ["greet"],
    })
    assert mem.id == "m1"
    assert mem.embedding is not None and len(mem.embedding) == 8
    merge_calls = [c for c in sql.calls if c.startswith("MERGE INTO")]
    assert len(merge_calls) == 1
    merge = merge_calls[0]
    assert "ON target.id = src.id" in merge
    assert "WHEN MATCHED THEN UPDATE SET" in merge
    assert "WHEN NOT MATCHED THEN INSERT" in merge
    assert "'m1'" in merge
    assert "'u1'" in merge
    assert "'profile'" in merge
    assert "array('greet')" in merge


def test_add_without_embedder_emits_null_embedding() -> None:
    sql = RecordingSqlExecutor()
    store = DeltaMemoryStore(run_sql=sql)
    store.add({"principal_id": "u1", "content": "hi"})
    merge = [c for c in sql.calls if c.startswith("MERGE INTO")][0]
    assert "CAST(NULL AS ARRAY<FLOAT>) AS embedding" in merge


def test_add_escapes_sql_injection_in_content() -> None:
    sql = RecordingSqlExecutor()
    store = DeltaMemoryStore(run_sql=sql)
    store.add({
        "id": "m1", "principal_id": "u'1",
        "content": "'; DROP TABLE memories; --",
    })
    merge = [c for c in sql.calls if c.startswith("MERGE INTO")][0]
    # The literal must be quoted with doubled single quotes.
    assert "'u''1'" in merge
    assert "'''; DROP TABLE memories; --'" in merge
    # No unescaped attack landed.
    assert "DROP TABLE memories;" in merge  # part of the literal
    # But the SQL outside the literal must not contain a DROP TABLE.
    # Check: the only occurrence is inside single quotes.
    occurrences = [m.start() for m in re.finditer(r"DROP TABLE memories", merge)]
    assert len(occurrences) == 1
    # The character before it must be a single quote (literal contents).
    assert merge[occurrences[0] - 2:occurrences[0]] == "; "  # inside literal
    # And the literal is the surrounding pair of quotes.


def test_add_batch_makes_one_embedding_call() -> None:
    calls: list[Sequence[str]] = []

    def embedder(texts: Sequence[str]) -> list[list[float]]:
        calls.append(list(texts))
        return _toy_embedder(texts)

    sql = RecordingSqlExecutor()
    store = DeltaMemoryStore(run_sql=sql, embedding_fn=embedder)
    out = store.add_batch([
        {"principal_id": "u1", "content": "a"},
        {"principal_id": "u1", "content": "b"},
        {"principal_id": "u1", "content": "c"},
    ])
    assert len(out) == 3
    assert len(calls) == 1
    assert calls[0] == ["a", "b", "c"]
    merges = [c for c in sql.calls if c.startswith("MERGE INTO")]
    assert len(merges) == 3


def test_add_batch_empty_is_noop() -> None:
    sql = RecordingSqlExecutor()
    store = DeltaMemoryStore(run_sql=sql, embedding_fn=_toy_embedder)
    assert store.add_batch([]) == []
    # No DDL, no MERGE — empty input is a true no-op.
    assert sql.calls == []


def test_merge_array_literals_have_correct_types() -> None:
    """ARRAY<STRING> and ARRAY<FLOAT> render with array(...) Spark syntax."""
    sql = RecordingSqlExecutor()
    store = DeltaMemoryStore(run_sql=sql, embedding_fn=_toy_embedder)
    store.add({
        "id": "m1", "principal_id": "u1",
        "content": "x", "tags": ["a", "b"],
    })
    merge = [c for c in sql.calls if c.startswith("MERGE INTO")][0]
    assert "array('a', 'b') AS tags" in merge
    assert "array(" in merge and " AS embedding" in merge


# ---------------------------------------------------------------------------
# get / update / delete
# ---------------------------------------------------------------------------


def _canned_row(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "m1",
        "principal_id": "u1",
        "namespace": "profile",
        "content": "stored content",
        "tags": ["a"],
        "importance": 0.7,
        "embedding": [1.0] * 8,
        "metadata": '{"k":"v"}',
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_000_100.0,
    }
    base.update(overrides)
    return base


def test_get_emits_select_and_decodes_row() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT.*FROM .* LIMIT 1$", [_canned_row()])])
    store = DeltaMemoryStore(run_sql=sql)
    mem = store.get("m1")
    assert mem is not None
    assert mem.id == "m1"
    assert mem.namespace == "profile"
    assert mem.tags == ("a",)
    assert mem.metadata == {"k": "v"}
    assert mem.embedding is not None and len(mem.embedding) == 8
    select = [c for c in sql.calls if c.startswith("SELECT")][0]
    assert "WHERE id = 'm1'" in select
    assert "LIMIT 1" in select


def test_get_missing_returns_none() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT", [])])
    store = DeltaMemoryStore(run_sql=sql)
    assert store.get("nope") is None


def test_get_handles_runtime_error_gracefully() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT", RuntimeError)])
    store = DeltaMemoryStore(run_sql=sql)
    assert store.get("m1") is None


def test_update_misses_when_row_absent() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT", [])])
    store = DeltaMemoryStore(run_sql=sql)
    assert store.update("m1", {"content": "x"}) is None


def test_update_reembeds_on_content_change() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT", [_canned_row()])])
    embed_calls: list[Sequence[str]] = []

    def embedder(texts: Sequence[str]) -> list[list[float]]:
        embed_calls.append(list(texts))
        return [[7.0] * 8 for _ in texts]

    store = DeltaMemoryStore(run_sql=sql, embedding_fn=embedder)
    out = store.update("m1", {"content": "new content"})
    assert out is not None
    assert out.content == "new content"
    assert out.embedding == tuple([7.0] * 8)
    # Embedder called once for the new content.
    assert embed_calls == [["new content"]]


def test_update_without_content_does_not_reembed() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT", [_canned_row()])])
    calls: list[Sequence[str]] = []

    def embedder(texts: Sequence[str]) -> list[list[float]]:
        calls.append(list(texts))
        return [[7.0] * 8 for _ in texts]

    store = DeltaMemoryStore(run_sql=sql, embedding_fn=embedder)
    out = store.update("m1", {"importance": 0.9, "tags": ["t1"]})
    assert out is not None
    assert out.importance == 0.9
    assert out.tags == ("t1",)
    # Embedder NOT called — content unchanged.
    assert calls == []


def test_delete_emits_delete_sql() -> None:
    # delete() existence-checks first (SELECT), then DELETEs only on a hit.
    # Seed a matching row so the existence check finds it and returns True.
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT", [_canned_row()])])
    store = DeltaMemoryStore(run_sql=sql)
    assert store.delete("m1") is True
    delete_sql = [c for c in sql.calls if c.startswith("DELETE FROM")][0]
    assert "WHERE id = 'm1'" in delete_sql


def test_delete_returns_false_on_miss() -> None:
    # No matching row → existence check misses → no DELETE emitted, False.
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT", [])])
    store = DeltaMemoryStore(run_sql=sql)
    assert store.delete("missing") is False
    assert not any(c.startswith("DELETE FROM") for c in sql.calls)


def test_delete_returns_false_on_error() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^DELETE", RuntimeError)])
    store = DeltaMemoryStore(run_sql=sql)
    assert store.delete("m1") is False


# ---------------------------------------------------------------------------
# list() filters
# ---------------------------------------------------------------------------


def test_list_emits_principal_filter_and_order() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT.*ORDER BY", [_canned_row()])])
    store = DeltaMemoryStore(run_sql=sql)
    rows = store.list(MemoryFilter(principal_id="u1"))
    assert len(rows) == 1
    select = [c for c in sql.calls if c.startswith("SELECT")][0]
    assert "principal_id = 'u1'" in select
    assert "ORDER BY updated_at DESC" in select
    assert "LIMIT 100" in select


def test_list_emits_all_filter_clauses() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT.*ORDER BY", [])])
    store = DeltaMemoryStore(run_sql=sql)
    store.list(MemoryFilter(
        principal_id="u1", namespace="profile",
        tags=("a", "b"), min_importance=0.3, limit=42,
    ))
    select = [c for c in sql.calls if c.startswith("SELECT")][0]
    assert "principal_id = 'u1'" in select
    assert "namespace = 'profile'" in select
    assert "importance >= 0.3" in select
    assert "size(array_intersect(tags, array('a', 'b'))) > 0" in select
    assert "LIMIT 42" in select


def test_list_raises_on_sql_failure() -> None:
    # H21: a swallowed error here would read as "no memories found" to the
    # recall tool. list() lets infra failures propagate; [] is reserved for a
    # genuinely empty result set.
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT", RuntimeError)])
    store = DeltaMemoryStore(run_sql=sql)
    with pytest.raises(RuntimeError):
        store.list(MemoryFilter(principal_id="u1"))


# ---------------------------------------------------------------------------
# recall — 4-tier fallback
# ---------------------------------------------------------------------------


def test_recall_branch1_vector_search_plus_embedder_passes_query_vector() -> None:
    vs_rows = [
        {**_canned_row(id="m_vs1"), "_score": 0.91},
        {**_canned_row(id="m_vs2"), "_score": 0.42},
    ]
    vs = FakeVectorSearch(rows=vs_rows)
    sql = RecordingSqlExecutor()
    store = DeltaMemoryStore(
        run_sql=sql, embedding_fn=_toy_embedder,
        vector_search=vs, index_name="ix",
    )
    results = store.recall(RecallOptions(
        principal_id="u1", query="apple", namespace="profile", k=3,
    ))
    assert len(results) == 2
    assert results[0].memory.id == "m_vs1"
    assert results[0].score == pytest.approx(0.91)
    assert len(vs.calls) == 1
    call = vs.calls[0]
    assert "query_vector" in call
    assert call["query_vector"] is not None
    assert "query_text" not in call
    # Filter pass-through.
    assert call["filters"]["principal_id"] == "u1"
    assert call["filters"]["namespace"] == "profile"
    assert call["num_results"] == 3
    assert call["index_name"] == "ix"


def test_recall_branch2_vector_search_only_passes_query_text() -> None:
    vs = FakeVectorSearch(rows=[{**_canned_row(), "_score": 0.5}])
    sql = RecordingSqlExecutor()
    store = DeltaMemoryStore(
        run_sql=sql, vector_search=vs, index_name="ix",
    )
    store.recall(RecallOptions(principal_id="u1", query="banana"))
    call = vs.calls[0]
    assert call.get("query_text") == "banana"
    assert "query_vector" not in call


def test_recall_branch3_embedder_only_cosine_clientside() -> None:
    # Two candidates with different first chars → different embeddings.
    rows = [
        _canned_row(id="m_a", content="alpha", embedding=[1.0] * 8),
        _canned_row(id="m_b", content="beta", embedding=[2.0] * 8),
    ]
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT.*ORDER BY", rows)])
    store = DeltaMemoryStore(run_sql=sql, embedding_fn=_toy_embedder)
    out = store.recall(RecallOptions(principal_id="u1", query="alpha", k=2))
    assert len(out) == 2
    # alpha matches m_a; cosine of equal-direction vectors is 1.
    assert out[0].memory.id == "m_a"
    assert out[0].score == pytest.approx(1.0)


def test_recall_branch4_recency_fallback() -> None:
    rows = [
        _canned_row(id="m_new", updated_at=1_700_001_000.0),
        _canned_row(id="m_old", updated_at=1_700_000_000.0),
    ]
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT.*ORDER BY", rows)])
    # No embedder, no vector search → recency.
    store = DeltaMemoryStore(run_sql=sql)
    out = store.recall(RecallOptions(principal_id="u1", query="anything", k=5))
    # list() returned 2 in ORDER BY DESC; recency gives normalized score.
    assert len(out) == 2
    assert out[0].memory.id == "m_new"
    assert out[0].score == 1.0
    assert out[1].score == pytest.approx(0.5)


def test_recall_branch4_empty_candidates() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT.*ORDER BY", [])])
    store = DeltaMemoryStore(run_sql=sql)
    out = store.recall(RecallOptions(principal_id="u1", query="hi"))
    assert out == []


def test_recall_score_column_fallback_chain() -> None:
    """Score column resolves _score → score → similarity_score → 0."""
    base = _canned_row()
    vs = FakeVectorSearch(rows=[
        {**base, "_score": 0.9, "score": 0.1, "similarity_score": 0.0},
        {**{**base, "id": "m_b"}, "score": 0.42},
        {**{**base, "id": "m_c"}, "similarity_score": 0.31},
        {**{**base, "id": "m_d"}},
    ])
    store = DeltaMemoryStore(
        run_sql=RecordingSqlExecutor(), vector_search=vs, index_name="ix",
    )
    out = store.recall(RecallOptions(principal_id="u1", query="q"))
    scores = {r.memory.id: r.score for r in out}
    assert scores["m1"] == pytest.approx(0.9)  # _score wins
    assert scores["m_b"] == pytest.approx(0.42)  # score
    assert scores["m_c"] == pytest.approx(0.31)  # similarity_score
    assert scores["m_d"] == pytest.approx(0.0)  # default


def test_recall_vector_search_row_decodes_to_memory() -> None:
    """Rows from Vector Search are mapped through _row_to_memory."""
    vs = FakeVectorSearch(rows=[
        {
            "id": "m_vs", "principal_id": "u1", "namespace": "profile",
            "content": "from VS", "tags": ["t1", "t2"], "importance": 0.6,
            "metadata": '{"a":1}',
            "created_at": 1_700_000_000.0, "updated_at": 1_700_001_000.0,
            "_score": 0.77,
        }
    ])
    store = DeltaMemoryStore(
        run_sql=RecordingSqlExecutor(),
        vector_search=vs, index_name="ix",
    )
    out = store.recall(RecallOptions(principal_id="u1", query="q"))
    assert len(out) == 1
    r = out[0]
    assert r.memory.id == "m_vs"
    assert r.memory.principal_id == "u1"
    assert r.memory.tags == ("t1", "t2")
    assert r.memory.importance == pytest.approx(0.6)
    assert r.memory.metadata == {"a": 1}
    assert r.score == pytest.approx(0.77)


def test_recall_filter_passthrough_only_principal_when_no_namespace() -> None:
    vs = FakeVectorSearch(rows=[])
    store = DeltaMemoryStore(
        run_sql=RecordingSqlExecutor(),
        vector_search=vs, index_name="ix",
    )
    store.recall(RecallOptions(principal_id="u1", query="q"))
    call = vs.calls[0]
    assert call["filters"] == {"principal_id": "u1"}


def test_recall_clientside_skips_rows_with_no_embedding() -> None:
    rows = [
        _canned_row(id="with", embedding=[1.0] * 8),
        _canned_row(id="without", embedding=None),
    ]
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT.*ORDER BY", rows)])
    store = DeltaMemoryStore(run_sql=sql, embedding_fn=_toy_embedder)
    out = store.recall(RecallOptions(principal_id="u1", query="alpha"))
    ids = [r.memory.id for r in out]
    assert "with" in ids
    assert "without" not in ids


def test_protocol_compliance() -> None:
    """DeltaMemoryStore satisfies the runtime-checkable MemoryStore."""
    from apx_agent._memory import MemoryStore

    store = DeltaMemoryStore(run_sql=lambda s: [])
    assert isinstance(store, MemoryStore)


class TestEpochToIsoStringInput:
    """Databricks statement-execution returns numeric columns as STRINGS, so a
    recalled ``updated_at``/``created_at`` arrives as e.g. ``'1.78e9'``.
    ``_epoch_to_iso`` must coerce before comparing — regression for recall
    crashing with "'<=' not supported between instances of 'str' and 'int'"."""

    def test_accepts_plain_string_seconds(self):
        from apx_agent._memory_delta import _epoch_to_iso
        out = _epoch_to_iso("1780597237.801")
        assert out.startswith("2026-") and out.endswith("Z")

    def test_accepts_scientific_string(self):
        from apx_agent._memory_delta import _epoch_to_iso
        out = _epoch_to_iso("1.780597237801E9")
        assert "T" in out and out.endswith("Z")

    def test_garbage_string_falls_back_no_crash(self):
        from apx_agent._memory_delta import _epoch_to_iso
        assert _epoch_to_iso("not a number").endswith("Z")

    def test_row_to_memory_with_string_timestamps(self):
        from apx_agent._memory_delta import _row_to_memory
        row = {
            "id": "m1", "principal_id": "u@x", "namespace": "default",
            "content": "hi", "tags": [], "importance": "0.9",
            "created_at": "1.78e9", "updated_at": "1.780597237801E9",
        }
        m = _row_to_memory(row)  # must not raise
        assert m.content == "hi" and m.importance == 0.9
        assert m.updated_at.endswith("Z")
