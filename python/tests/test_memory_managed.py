"""Exercise the real AgentKit SDK against a controlled memory REST transport."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from databricks.sdk.errors import NotFound, PermissionDenied

from apx_agent._memory import MemoryFilter, MemoryStore, RecallOptions
from apx_agent._memory_managed import ManagedMemoryStore, provision_managed_memory
from apx_agent._models import MemoryBackendConfig

BASE = "/api/2.0/agents/memory-stores/agent-memory"


class MemoryApi:
    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.exists = True
        self.failure: Exception | None = None

    def do(self, method: str, path: str, *, query: Any = None, body: Any = None) -> Any:
        self.calls.append(dict(method=method, path=path, query=query, body=body))
        assert path.startswith("/api/2.0/agents/memory-stores")
        if self.failure:
            raise self.failure
        if path == BASE:
            if not self.exists:
                raise NotFound("missing")
            return {"name": "memory-stores/agent-memory", "display_name": "agent-memory"}
        if path == "/api/2.0/agents/memory-stores":
            assert method == "POST"
            self.exists = True
            return {"name": "memory-stores/agent-memory", "display_name": body["display_name"]}
        if path == BASE + "/entries" and method == "POST":
            entry_id = f"entry-{len(self.entries) + 1}"
            entry = {**body, "name": f"memory-stores/agent-memory/entries/{entry_id}",
                     "create_time": "2026-10-05T00:00:00Z"}
            self.entries[entry_id] = entry
            return entry
        if path in (BASE + "/entries", BASE + "/entries:search"):
            args = body if method == "POST" else query
            entries = [e for e in self.entries.values() if e["actor_id"] == args["actor_id"]
                       and e["path"].startswith(args.get("path_prefix") or "/")]
            if method == "POST":
                return {"results": [{"managed_memory_entry": e, "score": 0.75}
                                    for e in entries[:args["page_size"]]]}
            # Two pages exercise the SDK's native pagination.
            if not args.get("page_token") and len(entries) > 1:
                return {"managed_memory_entries": entries[:1], "next_page_token": "page-2"}
            return {"managed_memory_entries": entries[1:] if args.get("page_token") else entries}
        entry_id = path.rsplit("/", 1)[-1]
        if entry_id not in self.entries:
            raise NotFound("missing")
        if method == "DELETE":
            del self.entries[entry_id]
            return {}
        if method == "PATCH":
            self.entries[entry_id].update(body)
        return self.entries[entry_id]


def test_sdk_memory_roundtrip_and_actor_isolation() -> None:
    api = MemoryApi()
    ws = SimpleNamespace(api_client=api)
    actor = "alice"
    store = ManagedMemoryStore(ws=ws, store_name="agent-memory", scope_resolver=lambda: actor)
    assert isinstance(store, MemoryStore)
    assert store.store_exists()
    memory = store.add({"principal_id": "alice", "content": "Prefers tea", "namespace": "prefs",
                        "tags": ["tea"], "importance": 0.9})
    assert memory.id == "entry-1"
    assert memory.content == "Prefers tea"
    assert memory.tags == () and memory.importance == 0.5
    assert store.get(memory.id).content == memory.content
    store.add({"principal_id": "alice", "content": "Prefers quiet"})
    store.add({"principal_id": "bob", "content": "Private"})
    assert len(store.list(MemoryFilter(principal_id="alice"))) == 2
    assert len(store.list(MemoryFilter(principal_id="alice", limit=1))) == 1
    hits = store.recall(RecallOptions(principal_id="alice", query="tea", namespace="prefs", k=200))
    assert [hit.memory.content for hit in hits] == ["Prefers tea"]
    assert hits[0].score == 0.75
    assert api.calls[-1]["body"]["page_size"] == 100
    assert api.calls[-1]["body"]["path_prefix"] == "/memories/prefs/"
    assert store.update(memory.id, {"content": "Prefers coffee"}).content == "Prefers coffee"
    actor = "bob"
    assert store.get(memory.id) is None
    assert store.update(memory.id, {"content": "overwrite"}) is None
    assert not store.delete(memory.id)
    assert api.entries[memory.id]["content"] == "Prefers coffee"
    actor = "alice"
    assert store.delete(memory.id)
    assert store.get(memory.id) is None
    assert not store.delete(memory.id)


def test_missing_identity_and_invalid_ids_cannot_access_entries() -> None:
    api = MemoryApi()
    ws = SimpleNamespace(api_client=api)
    store = ManagedMemoryStore(ws=ws, store_name="agent-memory")
    assert store.get("entry-1") is None
    assert store.update("entry-1", {"content": "x"}) is None
    assert not store.delete("entry-1")
    assert not api.calls
    for actor in (None, "", "  "):
        with pytest.raises(ValueError, match="trusted principal_id"):
            store.add({"principal_id": actor, "content": "must not write"})
    assert not api.calls
    scoped = ManagedMemoryStore(ws=ws, store_name="agent-memory", scope_resolver=lambda: "alice")
    for entry_id in ("../other", "/memories/prefs/old", "entry?actor_id=bob"):
        with pytest.raises(ValueError, match="entry ID"):
            scoped.delete(entry_id)
    assert not api.calls


@pytest.mark.parametrize("failure", [PermissionDenied("denied"), ConnectionError("offline")])
def test_backend_failures_propagate_and_never_provision(failure: Exception) -> None:
    api = MemoryApi()
    api.failure = failure
    ws = SimpleNamespace(api_client=api)
    store = ManagedMemoryStore(ws=ws, store_name="agent-memory", scope_resolver=lambda: "alice")
    assert not store.store_exists()
    with pytest.raises(Exception, match="denied|offline"):
        store.get("entry-1")
    with pytest.raises(Exception, match="denied|offline"):
        provision_managed_memory(ws, "agent-memory")
    assert all(call["method"] == "GET" for call in api.calls)


def test_provision_creates_only_missing_store() -> None:
    api = MemoryApi()
    api.exists = False
    ws = SimpleNamespace(api_client=api)
    assert provision_managed_memory(ws, "agent-memory").startswith("Created")
    assert provision_managed_memory(ws, "agent-memory").startswith("Memory store")
    writes = [call for call in api.calls if call["method"] == "POST"]
    assert len(writes) == 1
    assert writes[0]["body"] == {"display_name": "agent-memory", "description": "apx-agent managed agent memory"}


@pytest.mark.parametrize("name", ["main.agents.mem", "a", "Bad-Name", "bad/name", "a" * 57])
def test_invalid_and_legacy_store_names_fail_before_network(name: str) -> None:
    api = MemoryApi()
    with pytest.raises(ValueError, match="explicit data migration"):
        ManagedMemoryStore(ws=SimpleNamespace(api_client=api), store_name=name)
    with pytest.raises(ValueError, match="explicit data migration"):
        MemoryBackendConfig(type="managed", store_name=name)
    assert not api.calls
