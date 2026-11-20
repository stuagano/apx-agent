"""Opt-in #855/#856 probes; skipped cases are UNVERIFIED, not live proof.

APX_MANAGED_STATE_LIVE=1 authorizes the original disposable CRUD stores
``apx-862-855-probe`` / ``apx-862-855-sessions`` and their teardown. With
APX_MANAGED_STATE_APP_NAME it also grants WRITE on that memory store to the
App's existing service principal, reads the grant back, and revokes it.
APX_MANAGED_STATE_GRANT_SP still authorizes a separate fresh store and is not
set by this probe. Failure profiles are read-only. The in-process App probe
runs only inside an already authorized App; the external observation never
deploys or restarts one. See the contract-map runbook.
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


def _record_versions(record_property) -> None:
    from importlib.metadata import version

    record_property("agentbricks_version", version("databricks-agentbricks"))
    record_property("sdk_version", version("databricks-sdk"))


def _state_name(value) -> str:
    raw = value.value if value is not None and hasattr(value, "value") else value
    return "absent" if raw is None else str(raw)


def _workspace_text(ws, path: str) -> str:
    import base64

    content = ws.workspace.export(path).content
    raw = content if isinstance(content, bytes) else base64.b64decode(content)
    return raw.decode("utf-8")


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


def test_live_grant_on_authorized_store(live_workspace, record_property) -> None:
    """WRITE-grant the authorized App's existing SP on the disposable memory store.

    Does not create a service principal or a store outside MEMORY. The grant is
    revoked and the store deleted before the test returns.
    """
    from uuid import UUID

    from databricks_agentbricks.errors import AgentCliError

    ws, client = live_workspace
    app_name = _required_env("APX_MANAGED_STATE_APP_NAME")
    principal = ws.apps.get(name=app_name).service_principal_client_id
    parsed = str(UUID(principal))
    targets = list(ws.service_principals.list(filter=f'applicationId eq "{parsed}"'))
    assert len(targets) == 1, "Authorized App service principal did not resolve"
    assert targets[0].id != ws.current_user.me().id, "Grant must target another principal"

    def delete_store() -> None:
        try:
            client.memory_stores.get(MEMORY).delete()
        except Exception:
            return

    delete_store()
    store = client.memory_stores.create(MEMORY, description="apx-862 disposable #855 probe")
    record_property("memory_store", MEMORY)
    record_property("grant_principal", "authorized-app-service-principal")
    _record_versions(record_property)
    try:
        result = provision_managed_memory(ws, MEMORY, grant_service_principals=[parsed])
        assert f"Granted WRITE to service principal {parsed}." in result
        grants = list(store.list_permissions())
        assert any(
            grant.principal.principal_type == "SERVICE_PRINCIPAL"
            and grant.principal.name == parsed and grant.permission == "WRITE"
            for grant in grants
        )
        record_property("grant_readback", "SERVICE_PRINCIPAL WRITE")
        store.revoke_permission(parsed, principal_type="SERVICE_PRINCIPAL")
        assert all(grant.principal.name != parsed for grant in store.list_permissions())
        record_property("revoke", "absent")
    finally:
        delete_store()
        with pytest.raises(AgentCliError) as raised:
            client.memory_stores.get(MEMORY)
        assert raised.value.error_code == "NOT_FOUND"
        record_property("cleanup", "NOT_FOUND")


@pytest.mark.parametrize("scenario, codes", [
    ("DENIED", {"PERMISSION_DENIED"}),
    ("UNSUPPORTED", {"FEATURE_DISABLED", "NOT_IMPLEMENTED", "UNIMPLEMENTED"}),
])
def test_live_typed_store_failure(scenario, codes, record_property) -> None:
    """Read-only. Metadata GET may be visible; grant listing is the denial boundary.

    Unsupported service must fail the metadata GET itself. A missing store is
    never accepted as either failure.
    """
    from databricks.sdk import WorkspaceClient
    from databricks_agentbricks.errors import AgentCliError
    from databricks_agentkit import AgentKitClient

    profile = _required_env(f"APX_MANAGED_STATE_{scenario}_PROFILE")
    name = _required_env(f"APX_MANAGED_STATE_{scenario}_STORE")
    ws = WorkspaceClient(profile=profile)
    client = AgentKitClient(workspace_client=ws)
    record_property("workspace", ws.config.host)
    record_property("memory_store", name)
    _record_versions(record_property)
    # Do not call provision here: an unexpectedly readable/missing store must
    # fail this test, never create a resource or change permissions.
    operation = "memory_stores.get"
    try:
        store = client.memory_stores.get(name)
    except AgentCliError as exc:
        observed = exc
    else:
        if scenario != "DENIED":
            raise AssertionError("store metadata GET succeeded; not an unsupported-service failure")
        operation = "list_permissions"
        try:
            list(store.list_permissions())
        except AgentCliError as exc:
            observed = exc
        else:
            raise AssertionError("permission list succeeded; not a permission denial")
    assert observed.error_code in codes
    record_property("operation", operation)
    record_property("error_type", type(observed).__name__)
    record_property("error_code", observed.error_code)


def test_live_app_declared_resources_observed(live_workspace, record_property) -> None:
    """Read an already deployed App. Do not deploy, restart, or enter its process."""
    import tomllib

    app_name = _required_env("APX_MANAGED_STATE_APP_NAME")
    if os.environ.get("DATABRICKS_APP_NAME"):
        pytest.skip(
            "APX-MANAGED-STATE UNVERIFIED: this observation stays outside the App process."
        )
    ws, _client = live_workspace
    app = ws.apps.get(name=app_name)
    assert app.name == app_name
    record_property("app", app_name)
    record_property("workspace", ws.config.host)
    record_property("app_state", _state_name(app.app_status.state))
    record_property("compute_state", _state_name(app.compute_status.state))
    deployment = app.active_deployment
    source = None
    if deployment is None:
        record_property("deployment_state", "absent")
    else:
        record_property("deployment_state", _state_name(deployment.status.state))
        if deployment.deployment_id:
            record_property("deployment_id", deployment.deployment_id)
        source = deployment.source_code_path
    resource_names = [resource.name for resource in (app.resources or []) if resource.name]
    record_property("resources", ",".join(resource_names) if resource_names else "none")
    memory_name = None
    session_name = None
    if source:
        manifest = tomllib.loads(_workspace_text(ws, f"{source}/agent.toml"))
        memory = manifest.get("memory_store")
        session = manifest.get("session_store")
        memory_name = memory.get("name") if isinstance(memory, dict) else None
        session_name = session.get("name") if isinstance(session, dict) else None
    record_property("memory_store", memory_name or "not-declared")
    record_property("session_store", session_name or "not-declared")
    record_property("process_startup", "not-entered")
    record_property(
        "managed_binding",
        "declared-in-source" if memory_name and session_name else "UNVERIFIED",
    )
    _record_versions(record_property)


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
    if os.environ.get("DATABRICKS_APP_NAME") != expected_app:
        pytest.skip(
            "APX-MANAGED-STATE UNVERIFIED: run inside the authorized App process. "
            "This check does not deploy or restart it."
        )
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
