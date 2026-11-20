"""Opt-in live #855/#856 probes. A skip is UNVERIFIED, not a pass.

APX_MANAGED_STATE_LIVE=1 authorizes disposable stores ``apx-862-855-probe``
and ``apx-862-855-sessions`` and their teardown. The adapter failure cases are
read-only against the profile and store named by
APX_MANAGED_STATE_DENIED_* or APX_MANAGED_STATE_UNSUPPORTED_*. They do not
create stores, entries, or grants.
"""

from __future__ import annotations

import os

import pytest

from apx_agent import MemoryFilter, RecallOptions
from apx_agent._memory_managed import ManagedMemoryStore, provision_managed_memory

MEMORY = "apx-862-855-probe"
SESSION = "apx-862-855-sessions"


@pytest.fixture(scope="module")
def live_workspace():
    live = os.environ.get("APX_MANAGED_STATE_LIVE")
    if live is None or live.strip() not in {"1", "true", "yes"}:
        pytest.skip(
            "APX-MANAGED-STATE UNVERIFIED: set APX_MANAGED_STATE_LIVE=1 to run "
            "the disposable store probe on the selected Databricks profile."
        )
    from databricks.sdk import WorkspaceClient
    from databricks_agentkit import AgentKitClient

    ws = WorkspaceClient()
    return ws, AgentKitClient(workspace_client=ws)


def test_live_managed_store_provision_crud_and_isolation(live_workspace) -> None:
    ws, client = live_workspace

    def cleanup() -> None:
        try:
            store = client.memory_stores.get(MEMORY)
            for entry in list(store.list(actor_id="apx-855-actor-a", read_mask="*")):
                store.get(entry.id, read_mask="*").delete()
            store.delete()
        except Exception:
            pass
        try:
            client.session_stores.get(SESSION).delete()
        except Exception:
            pass

    cleanup()
    try:
        first = provision_managed_memory(ws, MEMORY, description="apx-862 disposable #855 probe")
        assert first.startswith("Created memory store")
        second = provision_managed_memory(ws, MEMORY, description="apx-862 disposable #855 probe")
        assert "already exists" in second
        assert client.memory_stores.get(MEMORY) is not None

        try:
            client.session_stores.get(SESSION)
        except Exception:
            client.session_stores.create(SESSION, description="apx-862 disposable #855 session probe")
        assert client.session_stores.get(SESSION) is not None

        actor_a = ManagedMemoryStore(ws=ws, store_name=MEMORY, scope_resolver=lambda: "apx-855-actor-a")
        actor_b = ManagedMemoryStore(ws=ws, store_name=MEMORY, scope_resolver=lambda: "apx-855-actor-b")
        added = actor_a.add({
            "principal_id": "apx-855-actor-a",
            "content": "actor A unique recall phrase 855",
            "namespace": "probe",
        })
        assert actor_a.get(added.id) is not None
        assert actor_b.get(added.id) is None
        listed_a = actor_a.list(MemoryFilter(principal_id="apx-855-actor-a", namespace="probe", limit=10))
        listed_b = actor_b.list(MemoryFilter(principal_id="apx-855-actor-b", namespace="probe", limit=10))
        assert len(listed_a) == 1
        assert listed_b == []
        recalled_a = actor_a.recall(RecallOptions(
            principal_id="apx-855-actor-a", query="unique recall phrase", k=5, namespace="probe",
        ))
        recalled_b = actor_b.recall(RecallOptions(
            principal_id="apx-855-actor-b", query="unique recall phrase", k=5, namespace="probe",
        ))
        assert recalled_a
        assert recalled_b == []
        updated = actor_a.update(added.id, {"content": "actor A updated 855"})
        assert updated is not None and "updated" in updated.content
        assert actor_a.delete(added.id)
        assert actor_a.get(added.id) is None

        from databricks_agentbricks.errors import AgentCliError

        # AgentKit normalizes every API failure to AgentCliError (wrap_api_error).
        with pytest.raises(AgentCliError) as raised:
            client.memory_stores.get("apx-862-855-missing-store")
        assert raised.value.error_code == "NOT_FOUND"
    finally:
        cleanup()


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        pytest.skip(f"APX-MANAGED-STATE UNVERIFIED: operator must supply {name}.")
    return value.strip()


def _live_enabled() -> None:
    live = os.environ.get("APX_MANAGED_STATE_LIVE")
    if live is None or live.strip() not in {"1", "true", "yes"}:
        pytest.skip(
            "APX-MANAGED-STATE UNVERIFIED: set APX_MANAGED_STATE_LIVE=1 to run "
            "the disposable store probe on the selected Databricks profile."
        )


@pytest.mark.parametrize("scenario, codes", [
    ("denied", {"PERMISSION_DENIED"}),
    ("unsupported", {"FEATURE_DISABLED", "NOT_IMPLEMENTED", "UNIMPLEMENTED"}),
])
def test_live_memory_adapter_failure(scenario, codes, record_property) -> None:
    """ManagedMemoryStore.get must surface the service error. Absence is not denial."""
    from pathlib import Path
    from runpy import run_path

    from databricks.sdk import WorkspaceClient

    _live_enabled()
    profile = _required_env(f"APX_MANAGED_STATE_{scenario.upper()}_PROFILE")
    store_name = _required_env(f"APX_MANAGED_STATE_{scenario.upper()}_STORE")
    script = Path(__file__).resolve().parents[2] / "scripts" / "managed_memory_failure_probe.py"
    probe = run_path(str(script))["probe"]
    ws = WorkspaceClient(profile=profile)
    result = probe(ws, store_name=store_name, scenario=scenario)
    record_property("workspace", ws.config.host)
    record_property("memory_store", store_name)
    record_property("operation", result["operation"])
    record_property("exception_type", result["exception_type"])
    record_property("error_code", result["error_code"])
    record_property("verified", result["verified"])
    assert result["verified"] is True
    assert result["exception_type"] == "AgentCliError"
    assert result["error_code"] in codes
    assert result.get("entry_returned") is not True
