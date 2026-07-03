"""Tests for ``apx_agent._example_delta`` — Delta-backed ``ExampleStore``.

Mirrors ``test_memory_delta.py`` but exercises the Example-specific
schema: ``agent_id`` scoping, ``intent`` partitioning, ``input`` as the
embedded field (not ``output``), and a nullable ``score`` column.

Test count: 22+. Covers:
  * Construction / table-name validation / VS-without-index rejection.
  * Auto-create DDL: idempotent + columns + `score FLOAT`.
  * `auto_create=False` skips DDL.
  * CRUD via MERGE; `input` is the field embedded at write/update.
  * SQL escaping for `input`, `output`, `intent`, and agent_id.
  * Score nullability (NULL when omitted; CAST when present).
  * list() WHERE clause: agent_id required, intent/tags/min_score optional.
  * find_similar() 4-tier fallback (vector_search + embedder, vector_search
    only, embedder only, recency).
  * Vector Search filter pass-through (agent_id, intent).
  * Score-column fallback chain — _score → similarity_score → 0
    (NOT ``score`` for examples, because ``score`` is a genuine row column).
  * Row decoding back to Example.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import pytest

from apx_agent._example import ExampleFilter, FindSimilarOptions
from apx_agent._example_delta import DeltaExampleStore, _row_to_example


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _toy_embedder(texts: Sequence[str]) -> list[list[float]]:
    """8-d vector based on first char of each text."""
    return [[float(ord(t[0]) - 96) if t else 0.0] * 8 for t in texts]


class RecordingSqlExecutor:
    def __init__(
        self,
        patterns: list[tuple[str, list[dict[str, Any]] | type[Exception]]] | None = None,
    ) -> None:
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
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[dict[str, Any]] = []

    def similarity_search(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        return {"rows": list(self.rows)}


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_constructor_defaults() -> None:
    sql = RecordingSqlExecutor()
    store = DeltaExampleStore(run_sql=sql)
    assert store.table_name == "apx_examples"
    assert sql.calls == []


def test_constructor_rejects_bad_table_name() -> None:
    with pytest.raises(ValueError):
        DeltaExampleStore(run_sql=lambda s: [], table_name="x; DROP")


def test_constructor_requires_index_with_vector_search() -> None:
    with pytest.raises(ValueError):
        DeltaExampleStore(
            run_sql=lambda s: [], vector_search=FakeVectorSearch(),
        )


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------


def test_auto_create_ddl_is_idempotent_and_has_score_float() -> None:
    sql = RecordingSqlExecutor()
    store = DeltaExampleStore(run_sql=sql)
    store.add({"agent_id": "a1", "input": "in", "output": "out"})
    store.add({"agent_id": "a1", "input": "in2", "output": "out2"})
    ddl_calls = [c for c in sql.calls if "CREATE TABLE" in c]
    assert len(ddl_calls) == 1
    ddl = ddl_calls[0]
    for col in (
        "id STRING", "agent_id STRING", "intent STRING",
        "input STRING", "output STRING", "score FLOAT",
        "tags ARRAY<STRING>", "embedding ARRAY<FLOAT>",
        "metadata STRING", "created_at DOUBLE", "updated_at DOUBLE",
    ):
        assert col in ddl


def test_auto_create_false_skips_ddl() -> None:
    sql = RecordingSqlExecutor()
    store = DeltaExampleStore(run_sql=sql, auto_create=False)
    store.add({"agent_id": "a1", "input": "in", "output": "out"})
    assert not any("CREATE TABLE" in c for c in sql.calls)


# ---------------------------------------------------------------------------
# add / add_batch
# ---------------------------------------------------------------------------


def test_add_embeds_input_not_output() -> None:
    seen: list[Sequence[str]] = []

    def embedder(texts: Sequence[str]) -> list[list[float]]:
        seen.append(list(texts))
        return _toy_embedder(texts)

    sql = RecordingSqlExecutor()
    store = DeltaExampleStore(run_sql=sql, embedding_fn=embedder)
    store.add({"agent_id": "a1", "input": "QUERY", "output": "ANSWER"})
    # Embedder called once, with the input — NOT the output.
    assert seen == [["QUERY"]]


def test_add_merge_includes_all_fields() -> None:
    sql = RecordingSqlExecutor()
    store = DeltaExampleStore(run_sql=sql, embedding_fn=_toy_embedder)
    store.add({
        "id": "e1", "agent_id": "a1", "intent": "classify",
        "input": "in", "output": "out", "score": 0.85,
        "tags": ["t1", "t2"], "metadata": {"k": "v"},
    })
    merge = [c for c in sql.calls if c.startswith("MERGE INTO")][0]
    assert "'e1'" in merge
    assert "'a1'" in merge
    assert "'classify'" in merge
    assert "'in' AS input" in merge
    assert "'out' AS output" in merge
    assert "CAST(0.85 AS FLOAT) AS score" in merge
    assert "array('t1', 't2') AS tags" in merge


def test_add_with_no_score_emits_null_cast() -> None:
    sql = RecordingSqlExecutor()
    store = DeltaExampleStore(run_sql=sql)
    store.add({"agent_id": "a1", "input": "in", "output": "out"})
    merge = [c for c in sql.calls if c.startswith("MERGE INTO")][0]
    assert "CAST(NULL AS FLOAT) AS score" in merge


def test_add_escapes_sql_injection() -> None:
    sql = RecordingSqlExecutor()
    store = DeltaExampleStore(run_sql=sql)
    store.add({
        "id": "e1",
        "agent_id": "a'; DROP TABLE x; --",
        "input": "i'", "output": "o'",
    })
    merge = [c for c in sql.calls if c.startswith("MERGE INTO")][0]
    assert "'a''; DROP TABLE x; --'" in merge
    assert "'i''' AS input" in merge
    assert "'o''' AS output" in merge


def test_add_batch_single_embedding_call_over_inputs() -> None:
    calls: list[Sequence[str]] = []

    def embedder(texts: Sequence[str]) -> list[list[float]]:
        calls.append(list(texts))
        return _toy_embedder(texts)

    sql = RecordingSqlExecutor()
    store = DeltaExampleStore(run_sql=sql, embedding_fn=embedder)
    store.add_batch([
        {"agent_id": "a1", "input": "a", "output": "x"},
        {"agent_id": "a1", "input": "b", "output": "y"},
        {"agent_id": "a1", "input": "c", "output": "z"},
    ])
    assert len(calls) == 1
    assert calls[0] == ["a", "b", "c"]


def test_add_batch_empty_is_noop() -> None:
    sql = RecordingSqlExecutor()
    store = DeltaExampleStore(run_sql=sql, embedding_fn=_toy_embedder)
    assert store.add_batch([]) == []
    assert sql.calls == []


# ---------------------------------------------------------------------------
# get / update / delete
# ---------------------------------------------------------------------------


def _canned_row(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "e1",
        "agent_id": "a1",
        "intent": "classify",
        "input": "stored in",
        "output": "stored out",
        "score": 0.7,
        "tags": ["t1"],
        "embedding": [1.0] * 8,
        "metadata": '{"k":"v"}',
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_000_100.0,
    }
    base.update(overrides)
    return base


def test_get_decodes_row() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT.*LIMIT 1$", [_canned_row()])])
    store = DeltaExampleStore(run_sql=sql)
    ex = store.get("e1")
    assert ex is not None
    assert ex.id == "e1"
    assert ex.intent == "classify"
    assert ex.score == pytest.approx(0.7)
    assert ex.tags == ("t1",)
    assert ex.metadata == {"k": "v"}


def test_get_missing_returns_none() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT", [])])
    store = DeltaExampleStore(run_sql=sql)
    assert store.get("nope") is None


def test_get_handles_runtime_error() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT", RuntimeError)])
    store = DeltaExampleStore(run_sql=sql)
    assert store.get("e1") is None


def test_update_reembeds_on_input_change() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT", [_canned_row()])])
    seen: list[Sequence[str]] = []

    def embedder(texts: Sequence[str]) -> list[list[float]]:
        seen.append(list(texts))
        return [[9.0] * 8 for _ in texts]

    store = DeltaExampleStore(run_sql=sql, embedding_fn=embedder)
    out = store.update("e1", {"input": "new in"})
    assert out is not None
    assert out.input == "new in"
    assert out.embedding == tuple([9.0] * 8)
    assert seen == [["new in"]]


def test_update_output_only_does_not_reembed() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT", [_canned_row()])])
    seen: list[Sequence[str]] = []

    def embedder(texts: Sequence[str]) -> list[list[float]]:
        seen.append(list(texts))
        return _toy_embedder(texts)

    store = DeltaExampleStore(run_sql=sql, embedding_fn=embedder)
    out = store.update("e1", {"output": "new out", "score": 0.92})
    assert out is not None
    assert out.output == "new out"
    assert out.score == pytest.approx(0.92)
    # Input unchanged — no embedding call.
    assert seen == []


def test_update_misses() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT", [])])
    store = DeltaExampleStore(run_sql=sql)
    assert store.update("e1", {"output": "x"}) is None


def test_delete_emits_correct_sql() -> None:
    # delete() existence-checks first (SELECT), then DELETEs only on a hit.
    # Seed a matching row so the existence check finds it and returns True.
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT", [_canned_row()])])
    store = DeltaExampleStore(run_sql=sql)
    assert store.delete("e1") is True
    del_sql = [c for c in sql.calls if c.startswith("DELETE FROM")][0]
    assert "WHERE id = 'e1'" in del_sql


def test_delete_returns_false_on_miss() -> None:
    # No matching row → existence check misses → no DELETE emitted, False.
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT", [])])
    store = DeltaExampleStore(run_sql=sql)
    assert store.delete("missing") is False
    assert not any(c.startswith("DELETE FROM") for c in sql.calls)


def test_delete_returns_false_on_error() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^DELETE", RuntimeError)])
    store = DeltaExampleStore(run_sql=sql)
    assert store.delete("e1") is False


# ---------------------------------------------------------------------------
# list filters
# ---------------------------------------------------------------------------


def test_list_default_filter() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT.*ORDER BY", [_canned_row()])])
    store = DeltaExampleStore(run_sql=sql)
    rows = store.list(ExampleFilter(agent_id="a1"))
    assert len(rows) == 1
    select = [c for c in sql.calls if c.startswith("SELECT")][0]
    assert "agent_id = 'a1'" in select
    assert "ORDER BY updated_at DESC" in select


def test_list_full_filter() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT.*ORDER BY", [])])
    store = DeltaExampleStore(run_sql=sql)
    store.list(ExampleFilter(
        agent_id="a1", intent="classify",
        tags=("t1", "t2"), min_score=0.5, limit=7,
    ))
    select = [c for c in sql.calls if c.startswith("SELECT")][0]
    assert "agent_id = 'a1'" in select
    assert "intent = 'classify'" in select
    assert "score >= 0.5" in select
    assert "size(array_intersect(tags, array('t1', 't2'))) > 0" in select
    assert "LIMIT 7" in select


def test_list_propagates_infra_error() -> None:
    # #380: an infra failure must propagate, not masquerade as "no examples".
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT", RuntimeError)])
    store = DeltaExampleStore(run_sql=sql)
    with pytest.raises(RuntimeError):
        store.list(ExampleFilter(agent_id="a1"))


# ---------------------------------------------------------------------------
# find_similar fallback tree
# ---------------------------------------------------------------------------


def test_find_similar_branch1_vector_search_plus_embedder() -> None:
    vs = FakeVectorSearch(rows=[
        {**_canned_row(id="ex_vs1"), "_score": 0.9},
        {**_canned_row(id="ex_vs2"), "_score": 0.3},
    ])
    store = DeltaExampleStore(
        run_sql=RecordingSqlExecutor(), embedding_fn=_toy_embedder,
        vector_search=vs, index_name="ix",
    )
    out = store.find_similar(FindSimilarOptions(
        agent_id="a1", query="alpha", intent="classify", k=3,
    ))
    assert len(out) == 2
    assert out[0].example.id == "ex_vs1"
    assert out[0].score == pytest.approx(0.9)
    call = vs.calls[0]
    assert "query_vector" in call
    assert "query_text" not in call
    assert call["filters"]["agent_id"] == "a1"
    assert call["filters"]["intent"] == "classify"
    assert call["num_results"] == 3
    assert call["index_name"] == "ix"


def test_find_similar_branch2_vector_search_only() -> None:
    vs = FakeVectorSearch(rows=[{**_canned_row(), "_score": 0.5}])
    store = DeltaExampleStore(
        run_sql=RecordingSqlExecutor(),
        vector_search=vs, index_name="ix",
    )
    store.find_similar(FindSimilarOptions(agent_id="a1", query="banana"))
    call = vs.calls[0]
    assert call.get("query_text") == "banana"
    assert "query_vector" not in call


def test_find_similar_branch3_embedder_only_cosine() -> None:
    rows = [
        _canned_row(id="ex_a", input="alpha", embedding=[1.0] * 8),
        _canned_row(id="ex_b", input="beta", embedding=[2.0] * 8),
    ]
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT.*ORDER BY", rows)])
    store = DeltaExampleStore(run_sql=sql, embedding_fn=_toy_embedder)
    out = store.find_similar(FindSimilarOptions(agent_id="a1", query="apple", k=2))
    assert len(out) == 2
    # query "apple" → vector matches embedding shape of both candidates (all
    # equal-direction). Cosine of equal-direction vectors is 1 for both.
    assert out[0].score == pytest.approx(1.0)
    assert out[1].score == pytest.approx(1.0)


def test_find_similar_branch4_recency_fallback() -> None:
    rows = [
        _canned_row(id="ex_new", updated_at=1_700_001_000.0),
        _canned_row(id="ex_old", updated_at=1_700_000_000.0),
    ]
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT.*ORDER BY", rows)])
    store = DeltaExampleStore(run_sql=sql)  # no embedder, no VS
    out = store.find_similar(FindSimilarOptions(agent_id="a1", query="x", k=5))
    assert len(out) == 2
    assert out[0].example.id == "ex_new"
    assert out[0].score == 1.0


def test_find_similar_branch4_no_candidates() -> None:
    sql = RecordingSqlExecutor(patterns=[(r"^SELECT.*ORDER BY", [])])
    store = DeltaExampleStore(run_sql=sql)
    out = store.find_similar(FindSimilarOptions(agent_id="a1", query="x"))
    assert out == []


def test_find_similar_score_fallback_excludes_score_column() -> None:
    """Examples have a real ``score`` column; the VS rank lives in
    ``_score`` or ``similarity_score`` — falling back to ``row.score``
    would conflate "this is a high-quality example" with "this example
    matched the query".
    """
    vs = FakeVectorSearch(rows=[
        {**_canned_row(id="ex_a", score=0.8), "_score": 0.9},
        {**_canned_row(id="ex_b", score=0.8), "similarity_score": 0.42},
        {**_canned_row(id="ex_c", score=0.8)},  # no _score, no similarity_score
    ])
    store = DeltaExampleStore(
        run_sql=RecordingSqlExecutor(),
        vector_search=vs, index_name="ix",
    )
    out = store.find_similar(FindSimilarOptions(agent_id="a1", query="q"))
    scores = {r.example.id: r.score for r in out}
    assert scores["ex_a"] == pytest.approx(0.9)
    assert scores["ex_b"] == pytest.approx(0.42)
    # row.score (0.8) is intentionally NOT used as a fallback — it's the
    # row's own quality score, not a similarity rank.
    assert scores["ex_c"] == pytest.approx(0.0)


def test_find_similar_vector_search_row_decodes_to_example() -> None:
    vs = FakeVectorSearch(rows=[
        {
            "id": "ex_vs", "agent_id": "a1", "intent": "summarize",
            "input": "Q", "output": "A", "score": 0.85,
            "tags": ["good"], "metadata": '{"src":"prod"}',
            "created_at": 1_700_000_000.0, "updated_at": 1_700_001_000.0,
            "_score": 0.77,
        }
    ])
    store = DeltaExampleStore(
        run_sql=RecordingSqlExecutor(),
        vector_search=vs, index_name="ix",
    )
    out = store.find_similar(FindSimilarOptions(agent_id="a1", query="q"))
    assert len(out) == 1
    r = out[0]
    assert r.example.id == "ex_vs"
    assert r.example.agent_id == "a1"
    assert r.example.intent == "summarize"
    assert r.example.score == pytest.approx(0.85)
    assert r.example.tags == ("good",)
    assert r.score == pytest.approx(0.77)


def test_find_similar_filters_passthrough_no_intent() -> None:
    vs = FakeVectorSearch(rows=[])
    store = DeltaExampleStore(
        run_sql=RecordingSqlExecutor(),
        vector_search=vs, index_name="ix",
    )
    store.find_similar(FindSimilarOptions(agent_id="a1", query="x"))
    call = vs.calls[0]
    assert call["filters"] == {"agent_id": "a1"}


def test_row_to_example_handles_none_score() -> None:
    row = _canned_row(score=None)
    ex = _row_to_example(row)
    assert ex.score is None


def test_protocol_compliance() -> None:
    from apx_agent._example import ExampleStore

    store = DeltaExampleStore(run_sql=lambda s: [])
    assert isinstance(store, ExampleStore)
