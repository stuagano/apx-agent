"""Exercise the real AgentKit SDK against a controlled memory REST transport."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from runpy import run_path
from types import SimpleNamespace
from typing import Any

import pytest
from databricks.sdk.errors import DatabricksError, NotFound, PermissionDenied
from databricks_agentbricks.errors import AgentCliError

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
        self.failure_target: tuple[str, str] | None = None

    def do(self, method: str, path: str, *, query: Any = None, body: Any = None) -> Any:
        self.calls.append(dict(method=method, path=path, query=query, body=body))
        assert path.startswith("/api/2.0/agents/memory-stores")
        if self.failure and (self.failure_target is None or self.failure_target == (method, path)):
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


@pytest.mark.parametrize("code", [
    "PERMISSION_DENIED", "FEATURE_DISABLED", "NOT_IMPLEMENTED", "UNIMPLEMENTED", "UNAVAILABLE",
])
@pytest.mark.parametrize("operation,method,suffix", [
    ("get", "GET", "/entries/entry-1"),
    ("update", "PATCH", "/entries/entry-1"),
    ("delete", "DELETE", "/entries/entry-1"),
    ("add", "POST", "/entries"),
    ("add_batch", "POST", "/entries"),
    ("list", "GET", "/entries"),
    ("recall", "POST", "/entries:search"),
])
def test_service_failure_codes_survive_each_adapter_operation(code, operation, method, suffix) -> None:
    """Fail the operation itself, not just the initial store metadata lookup."""
    api = MemoryApi()
    store = ManagedMemoryStore(
        ws=SimpleNamespace(api_client=api), store_name="agent-memory", scope_resolver=lambda: "alice",
    )
    store.add({"principal_id": "alice", "content": "unchanged"})
    before = {key: dict(value) for key, value in api.entries.items()}
    api.failure = DatabricksError("service failure", error_code=code)
    api.failure_target = (method, BASE + suffix)
    operations = {
        "get": lambda: store.get("entry-1"),
        "update": lambda: store.update("entry-1", {"content": "must not write"}),
        "delete": lambda: store.delete("entry-1"),
        "add": lambda: store.add({"principal_id": "alice", "content": "must not write"}),
        "add_batch": lambda: store.add_batch([{"principal_id": "alice", "content": "must not write"}]),
        "list": lambda: store.list(MemoryFilter(principal_id="alice")),
        "recall": lambda: store.recall(RecallOptions(principal_id="alice", query="unchanged")),
    }
    with pytest.raises(AgentCliError) as raised:
        operations[operation]()
    assert raised.value.error_code == code
    assert raised.value.__cause__ is api.failure
    assert api.entries == before
    assert api.calls[-1]["method"] == method
    assert api.calls[-1]["path"] == BASE + suffix


@pytest.mark.parametrize("code", [
    "PERMISSION_DENIED", "FEATURE_DISABLED", "NOT_IMPLEMENTED", "UNIMPLEMENTED", "UNAVAILABLE",
])
def test_resource_failures_are_not_absence_or_permission_to_create(code, caplog) -> None:
    api = MemoryApi()
    api.failure = DatabricksError("service failure", error_code=code)
    ws = SimpleNamespace(api_client=api)
    store = ManagedMemoryStore(ws=ws, store_name="agent-memory", scope_resolver=lambda: "alice")
    # Boot readiness intentionally degrades, but records why. Runtime and
    # explicit provisioning must instead propagate the structured SDK error.
    assert not store.store_exists()
    assert code in caplog.text
    with pytest.raises(AgentCliError) as raised:
        store.get("entry-1")
    assert raised.value.error_code == code
    with pytest.raises(AgentCliError) as raised:
        provision_managed_memory(ws, "agent-memory")
    assert raised.value.error_code == code
    assert all(call["method"] == "GET" for call in api.calls)


@pytest.mark.parametrize("scenario,code,verified", [
    ("denied", "PERMISSION_DENIED", True),
    ("unsupported", "FEATURE_DISABLED", True),
    ("unsupported", "NOT_IMPLEMENTED", True),
    ("unsupported", "UNIMPLEMENTED", True),
    ("denied", "UNAUTHENTICATED", False),
    ("denied", "FEATURE_DISABLED", False),
    ("unsupported", "PERMISSION_DENIED", False),
    ("unsupported", "UNAVAILABLE", False),
    ("unsupported", "NOT_FOUND", False),
    ("denied", "NOT_FOUND", False),
    ("denied", None, False),
])
def test_failure_probe_requires_the_requested_real_error(scenario, code, verified) -> None:
    probe = run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "managed_memory_failure_probe.py"))["probe"]
    api = MemoryApi()
    if code is not None:
        api.failure = DatabricksError("service failure", error_code=code)
    result = probe(SimpleNamespace(api_client=api), store_name="agent-memory", scenario=scenario)
    assert result["verified"] is verified
    assert result["error_code"] == (None if code == "NOT_FOUND" else code)
    assert all(call["method"] == "GET" for call in api.calls)
    assert "service failure" not in str(result)


@pytest.mark.parametrize("scenario,code,exit_code", [
    ("denied", "PERMISSION_DENIED", 0),
    ("unsupported", "FEATURE_DISABLED", 0),
    ("denied", "NOT_FOUND", 1),
    ("unsupported", None, 1),
])
def test_failure_probe_cli_records_evidence_and_fails_unsuitable_targets(
    scenario, code, exit_code, monkeypatch, capsys,
) -> None:
    main = run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "managed_memory_failure_probe.py"))["main"]
    api = MemoryApi()
    if code is not None:
        api.failure = DatabricksError("sensitive-response", error_code=code)
    ws = SimpleNamespace(api_client=api, config=SimpleNamespace(host="https://example.invalid"))
    monkeypatch.setitem(main.__globals__, "WorkspaceClient", lambda **kwargs: ws)
    monkeypatch.setattr(sys, "argv", ["probe", "--scenario", scenario, "--profile", "restricted", "--store", "agent-memory"])
    assert main() == exit_code
    output = capsys.readouterr().out
    evidence = json.loads(output)
    assert evidence["verified"] is (exit_code == 0)
    assert evidence["profile"] == "restricted"
    assert evidence["host"] == "https://example.invalid"
    assert evidence["timestamp"] and evidence["agentbricks_version"] and evidence["sdk_version"]
    assert "sensitive-response" not in output
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


def test_provision_grants_each_service_principal_through_agentkit() -> None:
    """#1129: a shared store grants other agents' principals without hand-written SDK calls."""
    from unittest.mock import MagicMock, call, patch

    client = MagicMock()
    store = client.memory_stores.get.return_value
    store.name = "memory-stores/abc123"
    with patch("databricks_agentkit.AgentKitClient", return_value=client):
        out = provision_managed_memory(MagicMock(), "agent-memory", grant_service_principals=["sp-a", "sp-b"])
    assert store.grant_permission.call_args_list == [
        call("sp-a", permission="WRITE", principal_type="SERVICE_PRINCIPAL"),
        call("sp-b", permission="WRITE", principal_type="SERVICE_PRINCIPAL"),
    ]
    client.memory_stores.create.assert_not_called()
    assert "Granted WRITE to service principal sp-b." in out
    assert "agentbricks memory stores permissions list memory-stores/abc123" in out


def test_provision_without_grants_grants_nothing() -> None:
    from unittest.mock import MagicMock, patch

    client = MagicMock()
    with patch("databricks_agentkit.AgentKitClient", return_value=client):
        provision_managed_memory(MagicMock(), "agent-memory")
    client.memory_stores.get.return_value.grant_permission.assert_not_called()
