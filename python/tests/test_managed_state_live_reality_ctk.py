"""Opt-in #855/#856 probes; skipped cases are UNVERIFIED, not live proof.

APX_MANAGED_STATE_LIVE=1 authorizes the original disposable CRUD stores
``apx-862-855-probe`` / ``apx-862-855-sessions`` and their teardown. Adding
APX_MANAGED_STATE_GRANT_SP authorizes a fresh disposable grant store and WRITE
to that other sandbox SP. Failure profiles are read-only; the App probe runs
inside an existing operator-authorized App only. See the contract-map runbook.
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


def test_live_grant_to_another_service_principal(live_workspace, record_property) -> None:
    """Explicitly authorized WRITE grant, independently read back, on a fresh store only."""
    from uuid import UUID, uuid4

    ws, client = live_workspace
    principal = str(UUID(_required_env("APX_MANAGED_STATE_GRANT_SP")))
    targets = list(ws.service_principals.list(filter=f'applicationId eq "{principal}"'))
    assert len(targets) == 1, "Supply one existing sandbox service-principal application ID"
    assert targets[0].id != ws.current_user.me().id, "Grant must target another principal"
    name = f"apx-l79-grant-{uuid4().hex}"
    # Create directly first: never adopt or delete an already-existing store.
    store = client.memory_stores.create(name, description="apx-l79 disposable grant probe")
    record_property("memory_store", store.name)
    record_property("grant_principal", principal)
    try:
        result = provision_managed_memory(ws, name, grant_service_principals=[principal])
        assert f"Granted WRITE to service principal {principal}." in result
        grants = list(store.list_permissions())
        assert any(
            grant.principal.principal_type == "SERVICE_PRINCIPAL"
            and grant.principal.name == principal and grant.permission == "WRITE"
            for grant in grants
        ), grants
        record_property("grant_readback", "SERVICE_PRINCIPAL WRITE")
    finally:
        store.delete()
        from databricks_agentbricks.errors import AgentCliError

        with pytest.raises(AgentCliError) as raised:
            client.memory_stores.get(name)
        assert raised.value.error_code == "NOT_FOUND"
        record_property("cleanup", "NOT_FOUND")


@pytest.mark.parametrize("scenario, codes", [
    ("DENIED", {"PERMISSION_DENIED"}),
    ("UNSUPPORTED", {"FEATURE_DISABLED", "NOT_IMPLEMENTED", "UNIMPLEMENTED"}),
])
def test_live_typed_store_failure(scenario, codes, record_property) -> None:
    """Read-only: operator supplies a denied store / workspace without the service."""
    from databricks.sdk import WorkspaceClient
    from databricks_agentbricks.errors import AgentCliError
    from databricks_agentkit import AgentKitClient

    profile = _required_env(f"APX_MANAGED_STATE_{scenario}_PROFILE")
    name = _required_env(f"APX_MANAGED_STATE_{scenario}_STORE")
    ws = WorkspaceClient(profile=profile)
    client = AgentKitClient(workspace_client=ws)
    record_property("workspace", ws.config.host)
    record_property("memory_store", name)
    # Do not call provision here: an unexpectedly readable/missing store must
    # fail this test, never create a resource or change permissions.
    with pytest.raises(AgentCliError) as raised:
        client.memory_stores.get(name)
    assert raised.value.error_code in codes
    record_property("error_type", type(raised.value).__name__)
    record_property("error_code", raised.value.error_code)


def test_live_app_process_starts_with_declared_stores(record_property) -> None:
    """Run inside an operator-owned disposable App project, never deploy from pytest.

    Requires a non-recovering project: starting a recovery loop could execute
    unfinished invocations. /readyz alone does not prove either store binding.
    """
    import tomllib
    from pathlib import Path

    from databricks_agentkit import AgentKitClient
    from databricks_agentkit.langgraph import checkpointer
    from databricks_agentkit.langgraph.session_store import DatabricksSessionStoreSaver
    from fastapi.testclient import TestClient

    from apx_agent._inspection import _load_agent_config
    from apx_agent._serve import create_app

    expected_app = _required_env("APX_MANAGED_STATE_APP_NAME")
    assert os.environ.get("DATABRICKS_APP_NAME") == expected_app, "Run inside the authorized App process"
    assert os.environ.get("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL") != "true"
    config = _load_agent_config(pyproject_path=Path.cwd() / "pyproject.toml")
    assert config is not None and config.recovery is False, "Recovery execution is outside this probe"
    assert config.memory is not None and config.memory.retention is None, "Retention deletes are outside this probe"
    manifest = tomllib.loads(Path("agent.toml").read_text())
    memory_name = manifest["memory_store"]["name"]
    session_name = manifest["session_store"]["name"]
    assert os.environ.get("AGENT_SESSION_STORE") == session_name
    bound_sessions: list[str] = []

    def build_saver(store: str):
        saver = checkpointer(store)  # real AgentKit saver, not an in-memory substitute
        assert isinstance(saver, DatabricksSessionStoreSaver), "Cached local saver is not a managed binding"
        assert saver._client._store_name == session_name, "Cached saver points at a different store"
        bound_sessions.append(store)
        return saver

    app = create_app(build_saver=build_saver)
    assert app._recovery_hook is None, "Effective runtime must not execute recovery"
    assert bound_sessions == [session_name]
    with TestClient(app) as http:
        response = http.get("/readyz")
        assert response.status_code == 200, response.text
        context = app.state.agent_context
        store = context.agent._apx_memory_store
        assert isinstance(store, ManagedMemoryStore)
        assert store._store == memory_name and store.store_exists()
        assert not vars(context.agent).get("_apx_memory_degraded", False)
        assert context.config.recovery is False
        session = AgentKitClient(workspace_client=app.state.workspace_client).session_stores.get(session_name)
        assert session.name.removeprefix("session-stores/") == session_name
    record_property("app", expected_app)
    record_property("workspace", app.state.workspace_client.config.host)
    record_property("memory_store", memory_name)
    record_property("session_store", bound_sessions[0])
    record_property("startup", "lifespan entered; readyz 200; managed memory and saver bound")


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
