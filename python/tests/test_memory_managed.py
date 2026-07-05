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
from apx_agent._memory_managed import ManagedMemoryStore, provision_managed_memory

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
        # id IS the path; path is /memories/{namespace}/{uuid} (store requires
        # the /memories/ prefix — verified live).
        assert call["body"]["path"] == mem.id
        assert mem.id.startswith("/memories/prefs/")
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
    def _search_api(self, rows: list[tuple[str, str, float]]) -> FakeApi:
        # Real shape (verified live): {results: [{memory_entry: {…}, score}]}.
        results = [
            {"memory_entry": {"path": p, "contents": c}, "score": s} for p, c, s in rows
        ]
        return FakeApi(lambda m, pa, q, b: {"results": results})

    def test_searches_and_maps_rows_to_scored_results(self) -> None:
        api = self._search_api(
            [("/memories/default/1", "first", 0.9), ("/memories/default/2", "second", 0.7)]
        )
        out = _store(api).recall(RecallOptions(principal_id="alice", query="q"))
        call = api.calls[0]
        assert call["method"] == "POST"
        assert call["path"] == f"{BASE}:search"
        # scope AND query are query params (verified live); no body.
        assert call["query"] == {"scope": "alice", "query": "q"}
        assert call["body"] is None
        assert [r.memory.content for r in out] == ["first", "second"]
        assert [r.score for r in out] == [0.9, 0.7]

    def test_namespace_is_a_path_prefix_post_filter(self) -> None:
        api = self._search_api(
            [("/memories/prefs/1", "keep", 0.9), ("/memories/other/2", "drop", 0.8)]
        )
        out = _store(api).recall(
            RecallOptions(principal_id="u", query="q", namespace="prefs")
        )
        assert [r.memory.content for r in out] == ["keep"]

    def test_tags_and_min_importance_are_ignored_not_emptying(self) -> None:
        # The managed model has no tags/importance; a tags filter must NOT
        # post-filter the results to empty.
        api = self._search_api([("/memories/default/1", "hit", 0.5)])
        out = _store(api).recall(
            RecallOptions(
                principal_id="u", query="q", tags=("nope",), min_importance=0.99
            )
        )
        assert [r.memory.content for r in out] == ["hit"]

    def test_respects_k(self) -> None:
        rows = [(f"/memories/default/{i}", str(i), 1.0) for i in range(5)]
        out = _store(self._search_api(rows)).recall(
            RecallOptions(principal_id="u", query="q", k=2)
        )
        assert len(out) == 2

    def test_missing_score_defaults_to_zero(self) -> None:
        api = FakeApi(
            lambda m, p, q, b: {"results": [{"memory_entry": {"path": "/memories/d/1", "contents": "x"}}]}
        )
        out = _store(api).recall(RecallOptions(principal_id="u", query="q"))
        assert len(out) == 1
        assert out[0].score == 0.0

    def test_empty_results_returns_empty(self) -> None:
        out = _store(FakeApi(lambda m, p, q, b: {})).recall(
            RecallOptions(principal_id="u", query="q")
        )
        assert out == []

    def test_paginates_through_next_page_token(self) -> None:
        # #487: a namespace match ranked onto page 2 must still be returned —
        # the single-page read could under-return after the post-filter.
        pages = {
            None: {
                "results": [{"memory_entry": {"path": "/memories/e/1"}, "score": 0.9}],
                "next_page_token": "t2",
            },
            "t2": {
                "results": [{"memory_entry": {"path": "/memories/e/2"}, "score": 0.8}]
            },
        }
        api = FakeApi(lambda m, p, q, b: pages[(q or {}).get("page_token")])
        out = _store(api).recall(
            RecallOptions(principal_id="u", query="q", namespace="e", k=10)
        )
        assert [r.memory.id for r in out] == ["/memories/e/1", "/memories/e/2"]


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    def test_lists_with_scope_and_namespace_prefix_and_limit(self) -> None:
        api = FakeApi(
            lambda m, p, q, b: {
                "entries": [
                    {"path": "/memories/prefs/1", "description": "a"},
                    {"path": "/memories/prefs/2", "description": "b"},
                    {"path": "/memories/other/3", "description": "c"},
                ]
            }
        )
        out = _store(api).list(MemoryFilter(principal_id="alice", namespace="prefs", limit=10))
        assert api.calls[0]["query"] == {"scope": "alice"}
        assert [m.id for m in out] == ["/memories/prefs/1", "/memories/prefs/2"]
        # list rows carry only description (no contents) — content falls back to it.
        assert out[0].content == "a"
        assert out[0].namespace == "prefs"

    def test_empty_store_returns_empty(self) -> None:
        # `entries` key omitted entirely when empty — must not blow up.
        out = _store(FakeApi(lambda m, p, q, b: {})).list(MemoryFilter(principal_id="u"))
        assert out == []

    def test_limit_caps_results(self) -> None:
        api = FakeApi(
            lambda m, p, q, b: {"entries": [{"path": f"/default/{i}"} for i in range(5)]}
        )
        assert len(_store(api).list(MemoryFilter(principal_id="u", limit=3))) == 3

    def test_paginates_through_next_page_token(self) -> None:
        # #487: a namespace match that lives on page 2 must still be returned —
        # the single-page read dropped it before.
        pages = {
            None: {"entries": [{"path": "/memories/prefs/1"}], "next_page_token": "t2"},
            "t2": {"entries": [{"path": "/memories/prefs/2"}]},  # last page, no token
        }
        api = FakeApi(lambda m, p, q, b: pages[(q or {}).get("page_token")])
        out = _store(api).list(
            MemoryFilter(principal_id="u", namespace="prefs", limit=10)
        )
        assert [m.id for m in out] == ["/memories/prefs/1", "/memories/prefs/2"]

    def test_ignored_page_token_degrades_to_single_page(self) -> None:
        # If the API ignores page_token (same page + same token forever), the
        # dedup + unchanged-token guard terminate with no dupes.
        api = FakeApi(
            lambda m, p, q, b: {
                "entries": [{"path": "/default/1"}, {"path": "/default/2"}],
                "next_page_token": "same",
            }
        )
        out = _store(api).list(MemoryFilter(principal_id="u", limit=100))
        assert [m.id for m in out] == ["/default/1", "/default/2"]


# ---------------------------------------------------------------------------
# get / update / delete — scope safety (fail closed, never trust the id)
# ---------------------------------------------------------------------------


class TestScopeSafety:
    def test_get_uses_trusted_scope_not_the_id(self) -> None:
        api = FakeApi(lambda m, p, q, b: {"contents": "v"})
        mem = _store(api, scope="alice").get("/memories/prefs/1")
        assert mem is not None
        assert api.calls[0]["query"] == {"scope": "alice", "path": "/memories/prefs/1"}
        assert mem.principal_id == "alice"
        assert mem.namespace == "prefs"

    def test_get_fails_closed_without_resolver(self) -> None:
        api = FakeApi(lambda m, p, q, b: {"contents": "v"})
        assert _store(api).get("/prefs/1") is None
        assert api.calls == []  # no REST call attempted

    def test_get_missing_entry_returns_none(self) -> None:
        api = FakeApi(lambda m, p, q, b: {})
        assert _store(api, scope="alice").get("/memories/prefs/1") is None

    def test_get_returns_none_when_api_raises_not_found(self) -> None:
        # Verified live: a missing entry surfaces as a not-found ERROR (the SDK's
        # NotFound class), not an empty body — get must still honor the
        # None-for-missing contract for a GENUINE miss.
        from databricks.sdk.errors import NotFound

        def boom(m: str, p: str, q: Any, b: Any) -> Any:
            raise NotFound("memory entry not found at path")

        assert _store(FakeApi(boom), scope="alice").get("/memories/prefs/1") is None

    def test_get_propagates_non_not_found_errors(self) -> None:
        # #486: a transient infra failure must NOT masquerade as "not found" —
        # only the REST not-found signal maps to None; anything else propagates
        # (update/delete/forget all call get() first, so a swallowed error here
        # would surface everywhere as a false "no such memory").
        def boom(m: str, p: str, q: Any, b: Any) -> Any:
            raise RuntimeError("warehouse unreachable")

        with pytest.raises(RuntimeError, match="warehouse unreachable"):
            _store(FakeApi(boom), scope="alice").get("/memories/prefs/1")

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


# ---------------------------------------------------------------------------
# store_exists (boot-time probe)
# ---------------------------------------------------------------------------


class TestStoreExists:
    def test_true_when_get_returns_dict(self) -> None:
        assert _store(FakeApi(lambda m, p, q, b: {"name": "x"})).store_exists() is True

    def test_false_when_get_raises(self) -> None:
        def boom(m: str, p: str, q: Any, b: Any) -> Any:
            raise RuntimeError("404 not found")

        assert _store(FakeApi(boom)).store_exists() is False

    def test_false_when_get_returns_none(self) -> None:
        assert _store(FakeApi(lambda m, p, q, b: None)).store_exists() is False


# ---------------------------------------------------------------------------
# provision_managed_memory (deploy/admin step)
# ---------------------------------------------------------------------------


class TestProvision:
    def test_creates_store_when_absent(self) -> None:
        # GET (existence probe) raises → absent → POST create.
        def handler(m: str, p: str, q: Any, b: Any) -> Any:
            if m == "GET":
                raise RuntimeError("404")
            return {}

        api = FakeApi(handler)
        status = provision_managed_memory(api, "main.agents.mem")
        assert "Created" in status
        post = next(c for c in api.calls if c["method"] == "POST")
        assert post["path"] == "/api/2.1/unity-catalog/memory-stores"
        assert post["body"] == {
            "name": "mem",
            "catalog_name": "main",
            "schema_name": "agents",
            "description": "apx-agent managed agent memory",
        }
        # Grant guidance is surfaced (admin must grant the serving principal).
        assert "MEMORY STORE" in status

    def test_idempotent_when_present(self) -> None:
        api = FakeApi(lambda m, p, q, b: {"name": "mem"})  # GET → exists
        status = provision_managed_memory(api, "main.agents.mem")
        assert "already exists" in status
        assert not any(c["method"] == "POST" for c in api.calls)

    def test_rejects_non_three_part_name(self) -> None:
        with pytest.raises(ValueError, match="3-part"):
            provision_managed_memory(FakeApi(), "main.mem")


class TestIsNotFound:
    def test_real_sdk_notfound_is_detected(self) -> None:
        from databricks.sdk.errors import NotFound
        from apx_agent._memory_managed import _is_not_found

        assert _is_not_found(NotFound("missing")) is True

    def test_duck_typed_notfound_is_detected(self) -> None:
        """A caller-supplied transport (test fake, alt SDK) that raises its own
        NotFound-shaped exception is still recognized without an isinstance
        dependency on the real SDK class."""
        from apx_agent._memory_managed import _is_not_found

        class NotFound(Exception):
            pass

        assert _is_not_found(NotFound("missing")) is True

    def test_generic_exception_is_not_classified_as_not_found(self) -> None:
        from apx_agent._memory_managed import _is_not_found

        assert _is_not_found(RuntimeError("boom")) is False
        assert _is_not_found(TimeoutError("boom")) is False
