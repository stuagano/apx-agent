"""Tests for ManagedMemoryStore — Databricks Managed Agent Memory backend.

Verifies the UC memory-store REST mapping behind the MemoryStore protocol:

  1. add → POST /entries with scope query + {path, contents, description};
     id IS the entry path; unpersisted tags/importance come back as defaults.
  2. recall → POST /entries:search; rows → RecallResult with score; namespace
     path-prefix post-filter; tags/min_importance are IGNORED (not emptying).
  3. list → GET /entries; namespace prefix filter + limit.
  4. get / update / delete → resolve scope from the trusted resolver, keyed by
     path == memory_id; FAIL CLOSED (no API call, None/False) without a scope —
     scope is never parsed from the model-supplied id.
  5. store_name validation; MemoryStore protocol conformance.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from apx_agent._memory import MemoryFilter, MemoryStore, RecallOptions
from apx_agent._memory_managed import ManagedMemoryStore

STORE = "main.agents.mem"
BASE = f"/api/2.1/unity-catalog/memory-stores/{STORE}/entries"


class FakeApi:
    """Records calls; returns whatever ``handler`` yields (``{}`` by default)."""

    def __init__(
        self,
        handler: Callable[[str, str, dict | None, dict | None], Any] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._handler = handler

    def do(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append({"method": method, "path": path, "query": query, "body": body})
        return self._handler(method, path, query, body) if self._handler else {}


def _store(api: FakeApi, *, scope: str | None = None) -> ManagedMemoryStore:
    return ManagedMemoryStore(
        api=api,
        store_name=STORE,
        scope_resolver=(lambda: scope) if scope is not None else None,
    )


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


class TestAdd:
    def test_posts_entry_with_scope_and_derived_description(self) -> None:
        api = FakeApi()
        mem = _store(api).add(
            {"principal_id": "alice", "content": "Likes oat milk", "namespace": "prefs"}
        )
        assert len(api.calls) == 1
        call = api.calls[0]
        assert call["method"] == "POST"
        assert call["path"] == BASE
        assert call["query"] == {"scope": "alice"}
        assert call["body"]["contents"] == "Likes oat milk"
        assert call["body"]["description"] == "Likes oat milk"
        # id IS the path; path is /{namespace}/{uuid}
        assert call["body"]["path"] == mem.id
        assert mem.id.startswith("/prefs/")
        assert mem.principal_id == "alice"
        assert mem.namespace == "prefs"
        assert mem.content == "Likes oat milk"

    def test_does_not_persist_or_claim_tags_importance(self) -> None:
        api = FakeApi()
        mem = _store(api).add(
            {
                "principal_id": "alice",
                "content": "x",
                "tags": ["a", "b"],
                "importance": 0.9,
                "metadata": {"k": "v"},
            }
        )
        # Not in the request body (no field for them) ...
        assert set(api.calls[0]["body"]) == {"path", "contents", "description"}
        # ... and not claimed on the returned row (round-trips consistent).
        assert mem.tags == ()
        assert mem.importance == 0.5
        assert mem.metadata == {}

    def test_description_is_first_nonempty_line(self) -> None:
        api = FakeApi()
        _store(api).add({"principal_id": "u", "content": "\n\n  hello world  \nmore"})
        assert api.calls[0]["body"]["description"] == "hello world"


# ---------------------------------------------------------------------------
# recall (entries:search)
# ---------------------------------------------------------------------------


class TestRecall:
    def _search_api(self, rows: list[dict]) -> FakeApi:
        return FakeApi(lambda m, p, q, b: {"entries": rows})

    def test_searches_and_maps_rows_to_scored_results(self) -> None:
        api = self._search_api(
            [
                {"path": "/default/1", "contents": "first", "score": 0.9},
                {"path": "/default/2", "contents": "second", "score": 0.7},
            ]
        )
        out = _store(api).recall(RecallOptions(principal_id="alice", query="q"))
        call = api.calls[0]
        assert call["method"] == "POST"
        assert call["path"] == f"{BASE}:search"
        assert call["query"] == {"scope": "alice"}
        assert call["body"] == {"query": "q"}
        assert [r.memory.content for r in out] == ["first", "second"]
        assert [r.score for r in out] == [0.9, 0.7]

    def test_namespace_is_a_path_prefix_post_filter(self) -> None:
        api = self._search_api(
            [
                {"path": "/prefs/1", "contents": "keep", "score": 0.9},
                {"path": "/other/2", "contents": "drop", "score": 0.8},
            ]
        )
        out = _store(api).recall(
            RecallOptions(principal_id="u", query="q", namespace="prefs")
        )
        assert [r.memory.content for r in out] == ["keep"]

    def test_tags_and_min_importance_are_ignored_not_emptying(self) -> None:
        # The managed model has no tags/importance; a tags filter must NOT
        # post-filter the results to empty.
        api = self._search_api([{"path": "/default/1", "contents": "hit", "score": 0.5}])
        out = _store(api).recall(
            RecallOptions(
                principal_id="u", query="q", tags=("nope",), min_importance=0.99
            )
        )
        assert [r.memory.content for r in out] == ["hit"]

    def test_respects_k(self) -> None:
        rows = [{"path": f"/default/{i}", "contents": str(i), "score": 1.0} for i in range(5)]
        out = _store(self._search_api(rows)).recall(
            RecallOptions(principal_id="u", query="q", k=2)
        )
        assert len(out) == 2

    def test_falls_back_to_results_key_and_default_score(self) -> None:
        api = FakeApi(lambda m, p, q, b: {"results": [{"path": "/d/1", "contents": "x"}]})
        out = _store(api).recall(RecallOptions(principal_id="u", query="q"))
        assert len(out) == 1
        assert out[0].score == 0.0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    def test_lists_with_scope_and_namespace_prefix_and_limit(self) -> None:
        api = FakeApi(
            lambda m, p, q, b: {
                "entries": [
                    {"path": "/prefs/1", "description": "a"},
                    {"path": "/prefs/2", "description": "b"},
                    {"path": "/other/3", "description": "c"},
                ]
            }
        )
        out = _store(api).list(MemoryFilter(principal_id="alice", namespace="prefs", limit=10))
        assert api.calls[0]["query"] == {"scope": "alice"}
        assert [m.id for m in out] == ["/prefs/1", "/prefs/2"]
        # content falls back to description when contents absent
        assert out[0].content == "a"

    def test_empty_store_returns_empty(self) -> None:
        # `entries` key omitted entirely when empty — must not blow up.
        out = _store(FakeApi(lambda m, p, q, b: {})).list(MemoryFilter(principal_id="u"))
        assert out == []

    def test_limit_caps_results(self) -> None:
        api = FakeApi(
            lambda m, p, q, b: {"entries": [{"path": f"/default/{i}"} for i in range(5)]}
        )
        assert len(_store(api).list(MemoryFilter(principal_id="u", limit=3))) == 3


# ---------------------------------------------------------------------------
# get / update / delete — scope safety (fail closed, never trust the id)
# ---------------------------------------------------------------------------


class TestScopeSafety:
    def test_get_uses_trusted_scope_not_the_id(self) -> None:
        api = FakeApi(lambda m, p, q, b: {"contents": "v"})
        mem = _store(api, scope="alice").get("/prefs/1")
        assert mem is not None
        assert api.calls[0]["query"] == {"scope": "alice", "path": "/prefs/1"}
        assert mem.principal_id == "alice"
        assert mem.namespace == "prefs"

    def test_get_fails_closed_without_resolver(self) -> None:
        api = FakeApi(lambda m, p, q, b: {"contents": "v"})
        assert _store(api).get("/prefs/1") is None
        assert api.calls == []  # no REST call attempted

    def test_get_missing_entry_returns_none(self) -> None:
        api = FakeApi(lambda m, p, q, b: {})
        assert _store(api, scope="alice").get("/prefs/1") is None

    def test_delete_uses_trusted_scope(self) -> None:
        api = FakeApi()
        assert _store(api, scope="alice").delete("/prefs/1") is True
        call = api.calls[0]
        assert call["method"] == "DELETE"
        assert call["query"] == {"scope": "alice", "path": "/prefs/1"}

    def test_delete_fails_closed_without_resolver(self) -> None:
        api = FakeApi()
        assert _store(api).delete("/prefs/1") is False
        assert api.calls == []

    def test_update_replaces_contents_with_trusted_scope(self) -> None:
        api = FakeApi(lambda m, p, q, b: {"contents": "old"})
        mem = _store(api, scope="alice").update("/prefs/1", {"content": "new"})
        assert mem is not None and mem.content == "new"
        patch = next(c for c in api.calls if c["method"] == "PATCH")
        assert patch["body"]["scope"] == "alice"
        assert patch["body"]["path"] == "/prefs/1"
        assert patch["body"]["replace_all"] == "new"
        assert patch["body"]["description"] == "new"

    def test_update_fails_closed_without_resolver(self) -> None:
        api = FakeApi()
        assert _store(api).update("/prefs/1", {"content": "new"}) is None
        assert api.calls == []

    def test_update_no_content_change_is_a_noop_read(self) -> None:
        api = FakeApi(lambda m, p, q, b: {"contents": "old"})
        mem = _store(api, scope="alice").update("/prefs/1", {"importance": 0.9})
        assert mem is not None and mem.content == "old"
        assert not any(c["method"] == "PATCH" for c in api.calls)


# ---------------------------------------------------------------------------
# construction + protocol
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_rejects_unsafe_store_name(self) -> None:
        with pytest.raises(ValueError):
            ManagedMemoryStore(api=FakeApi(), store_name="bad name; DROP")

    def test_conforms_to_memory_store_protocol(self) -> None:
        assert isinstance(_store(FakeApi()), MemoryStore)
