"""Credential-free checks of the opt-in probes; these are NOT live evidence."""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from databricks.sdk.errors import NotFound, NotImplemented, PermissionDenied
from databricks_agentbricks.errors import AgentCliError
from databricks_agentkit import AgentKitClient

from tests import test_managed_state_live_reality_ctk as probe
from apx_agent._memory_managed import provision_managed_memory

SP = "11111111-2222-3333-4444-555555555555"


class GrantApi:
    """Minimal REST boundary: permission writes and their independent readback."""

    def __init__(self) -> None:
        self.stores: dict[str, dict] = {}
        self.grants: list[dict] = []
        self.calls: list[dict] = []
        self.omit_readback = False

    def do(self, method, path, *, query=None, body=None):
        self.calls.append({"method": method, "path": path, "body": body})
        root = "/api/2.0/agents/memory-stores"
        if path == root and method == "POST":
            name = body["display_name"]
            self.stores[name] = {"name": f"memory-stores/{name}", "display_name": name}
            return self.stores[name]
        name = path.removeprefix(root + "/").split("/")[0]
        if name not in self.stores:
            raise NotFound("missing", error_code="NOT_FOUND")
        if path.endswith("/permissions:grant"):
            self.grants.append(body)
            return {}
        if path.endswith("/permissions"):
            return {"memory_store_permissions": [] if self.omit_readback else self.grants}
        if method == "DELETE":
            del self.stores[name]
            return {}
        return self.stores[name]


@pytest.mark.parametrize("omit_readback", [False, True])
def test_grant_probe_requires_independent_readback_and_cleans_up(monkeypatch, omit_readback) -> None:
    api = GrantApi()
    api.omit_readback = omit_readback
    ws = SimpleNamespace(
        api_client=api,
        service_principals=SimpleNamespace(list=lambda **_: [SimpleNamespace(id="other")]),
        current_user=SimpleNamespace(me=lambda: SimpleNamespace(id="caller")),
    )
    monkeypatch.setenv("APX_MANAGED_STATE_GRANT_SP", SP)
    properties = {}

    def run():
        probe.test_live_grant_to_another_service_principal(
            (ws, AgentKitClient(workspace_client=ws)), properties.__setitem__,
        )

    if omit_readback:
        with pytest.raises(AssertionError):
            run()
        assert "grant_readback" not in properties
    else:
        run()
        assert properties["grant_readback"] == "SERVICE_PRINCIPAL WRITE"
    assert properties["cleanup"] == "NOT_FOUND"
    assert not api.stores
    assert api.grants == [{"principal": {"principal_type": "SERVICE_PRINCIPAL", "name": SP},
                           "permission": "WRITE"}]
    assert api.calls[0]["method"] == "POST"  # no pre-cleanup/adoption of another run's store


@pytest.mark.parametrize("scenario, failure, expected", [
    ("DENIED", PermissionDenied("denied", error_code="PERMISSION_DENIED"), "PERMISSION_DENIED"),
    ("UNSUPPORTED", NotImplemented("unsupported", error_code="NOT_IMPLEMENTED"), "NOT_IMPLEMENTED"),
])
def test_typed_failure_probe_uses_real_sdk_and_never_writes(monkeypatch, scenario, failure, expected) -> None:
    api = MagicMock()
    api.do.side_effect = failure
    ws = SimpleNamespace(api_client=api, config=SimpleNamespace(host="https://sandbox.invalid"))
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda **_: ws)
    monkeypatch.setenv(f"APX_MANAGED_STATE_{scenario}_PROFILE", "sandbox")
    monkeypatch.setenv(f"APX_MANAGED_STATE_{scenario}_STORE", "denied-store")
    properties = {}
    probe.test_live_typed_store_failure(scenario, {expected}, properties.__setitem__)
    assert properties["error_type"] == "AgentCliError"
    assert properties["error_code"] == expected
    # APX provisioning must preserve that typed error, not interpret it as NOT_FOUND.
    with pytest.raises(AgentCliError) as raised:
        provision_managed_memory(ws, "denied-store")
    assert raised.value.error_code == expected
    assert all(call.args[0] == "GET" for call in api.do.call_args_list)


def test_denied_probe_uses_permission_list_when_metadata_is_visible(monkeypatch) -> None:
    """A visible store is not access. The grant list is the read-only denial."""
    api = MagicMock()

    def do(method, path, **_kwargs):
        assert method == "GET", "Denial probe must not write"
        if path.endswith("/permissions"):
            raise PermissionDenied("denied", error_code="PERMISSION_DENIED")
        if path.endswith("/denied-store"):
            return {"name": "memory-stores/denied-store", "display_name": "denied-store"}
        raise AssertionError(path)

    api.do.side_effect = do
    ws = SimpleNamespace(api_client=api, config=SimpleNamespace(host="https://sandbox.invalid"))
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda **_: ws)
    monkeypatch.setenv("APX_MANAGED_STATE_DENIED_PROFILE", "sandbox")
    monkeypatch.setenv("APX_MANAGED_STATE_DENIED_STORE", "denied-store")
    properties = {}
    probe.test_live_typed_store_failure("DENIED", {"PERMISSION_DENIED"}, properties.__setitem__)
    assert properties["operation"] == "list_permissions"
    assert properties["error_code"] == "PERMISSION_DENIED"
    assert all(call.args[0] == "GET" for call in api.do.call_args_list)


def test_visible_permission_list_is_not_a_denial(monkeypatch) -> None:
    api = MagicMock()

    def do(method, path, **_kwargs):
        assert method == "GET"
        if path.endswith("/permissions"):
            return {"memory_store_permissions": []}
        return {"name": "memory-stores/denied-store", "display_name": "denied-store"}

    api.do.side_effect = do
    ws = SimpleNamespace(api_client=api, config=SimpleNamespace(host="https://sandbox.invalid"))
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda **_: ws)
    monkeypatch.setenv("APX_MANAGED_STATE_DENIED_PROFILE", "sandbox")
    monkeypatch.setenv("APX_MANAGED_STATE_DENIED_STORE", "denied-store")
    with pytest.raises(AssertionError, match="permission list succeeded"):
        probe.test_live_typed_store_failure("DENIED", {"PERMISSION_DENIED"}, lambda *_: None)


def test_failure_probe_does_not_accept_missing_as_denied(monkeypatch) -> None:
    api = MagicMock()
    api.do.side_effect = NotFound("not a permission failure", error_code="NOT_FOUND")
    ws = SimpleNamespace(api_client=api, config=SimpleNamespace(host="https://sandbox.invalid"))
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda **_: ws)
    monkeypatch.setenv("APX_MANAGED_STATE_DENIED_PROFILE", "sandbox")
    monkeypatch.setenv("APX_MANAGED_STATE_DENIED_STORE", "missing-store")
    with pytest.raises(AssertionError):
        probe.test_live_typed_store_failure("DENIED", {"PERMISSION_DENIED"}, lambda *_: None)
    assert all(call.args[0] == "GET" for call in api.do.call_args_list)


@pytest.mark.parametrize("saver_binding", ["declared", "wrong-store", "inmemory"])
def test_app_probe_enters_real_startup_and_checks_both_bindings(tmp_path, monkeypatch, saver_binding) -> None:
    from databricks_agentkit.langgraph.session_store import DatabricksSessionStoreSaver
    from databricks_agentkit.runtime.store import InMemoryRuntimeStore

    from apx_agent import AgentConfig
    from apx_agent._project_gen import generate_project

    config = AgentConfig(name="proof", target="durable_agent_server", model="test",
                         memory={"type": "managed", "store_name": "proof-memory"},
                         session={"type": "managed", "store_name": "proof-sessions"})
    generate_project(config, tmp_path)
    api = MagicMock()

    def read(method, path, **_):
        assert method == "GET", "Startup binding probe must not write"
        if path.endswith("/proof-memory"):
            return {"name": "memory-stores/proof-memory", "display_name": "proof-memory"}
        if path.endswith("/proof-sessions"):
            return {"name": "session-stores/proof-sessions", "display_name": "proof-sessions"}
        raise AssertionError(path)

    api.do.side_effect = read
    ws = SimpleNamespace(api_client=api, config=SimpleNamespace(host="https://sandbox.invalid"))
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda **_: ws)
    def saver(store):
        from langgraph.checkpoint.memory import InMemorySaver

        if saver_binding == "inmemory":
            return InMemorySaver()
        name = store if saver_binding == "declared" else "other-sessions"
        return DatabricksSessionStoreSaver(name, workspace_client=ws)

    monkeypatch.setattr("databricks_agentkit.langgraph.checkpointer", saver)
    # Only invocation persistence is local. The real App compiler, lifespan,
    # managed-memory adapter and managed-session saver run unchanged.
    monkeypatch.setattr("databricks_agentkit.runtime.runtime.runtime_store_from_environment", InMemoryRuntimeStore)
    monkeypatch.setenv("APX_MANAGED_STATE_APP_NAME", "authorized-probe")
    monkeypatch.setenv("DATABRICKS_APP_NAME", "authorized-probe")
    monkeypatch.setenv("AGENT_SESSION_STORE", "proof-sessions")
    monkeypatch.setenv("APX_PYPROJECT", str(tmp_path / "pyproject.toml"))
    monkeypatch.delenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "agent", raising=False)
    properties = {}
    try:
        if saver_binding != "declared":
            with pytest.raises(AssertionError, match="Cached"):
                probe.test_live_app_process_starts_with_declared_stores(properties.__setitem__)
            assert not properties
            return
        probe.test_live_app_process_starts_with_declared_stores(properties.__setitem__)
    finally:
        sys.modules.pop("agent", None)
    assert properties["memory_store"] == "proof-memory"
    assert properties["session_store"] == "proof-sessions"
    assert properties["startup"].startswith("lifespan entered")
    assert {call.args[1] for call in api.do.call_args_list} == {
        "/api/2.0/agents/memory-stores/proof-memory", "/api/2.0/agents/session-stores/proof-sessions"}


class AuthorizedGrantApi(GrantApi):
    def do(self, method, path, *, query=None, body=None):
        if path.endswith("/permissions:revoke"):
            self.calls.append({"method": method, "path": path, "body": body})
            principal_name = body["principal"]["name"]
            self.grants = [grant for grant in self.grants if grant["principal"]["name"] != principal_name]
            return {}
        return super().do(method, path, query=query, body=body)


def test_authorized_store_grant_readback_revokes_and_hides_principal(monkeypatch) -> None:
    api = AuthorizedGrantApi()
    app = SimpleNamespace(
        service_principal_client_id=SP,
        name="authorized-probe",
    )
    ws = SimpleNamespace(
        api_client=api,
        apps=SimpleNamespace(get=lambda **_: app),
        service_principals=SimpleNamespace(list=lambda **_: [SimpleNamespace(id="app-sp")]),
        current_user=SimpleNamespace(me=lambda: SimpleNamespace(id="caller")),
    )
    monkeypatch.setenv("APX_MANAGED_STATE_APP_NAME", "authorized-probe")
    monkeypatch.delenv("APX_MANAGED_STATE_GRANT_SP", raising=False)
    properties = {}
    probe.test_live_grant_on_authorized_store((ws, AgentKitClient(workspace_client=ws)), properties.__setitem__)
    assert properties["memory_store"] == probe.MEMORY
    assert properties["grant_principal"] == "authorized-app-service-principal"
    assert properties["grant_readback"] == "SERVICE_PRINCIPAL WRITE"
    assert properties["revoke"] == "absent"
    assert properties["cleanup"] == "NOT_FOUND"
    assert SP not in properties.values()
    assert not api.stores
    assert not api.grants
    assert any(call["path"].endswith("/permissions:grant") for call in api.calls)
    assert any(call["path"].endswith("/permissions:revoke") for call in api.calls)
    assert all("apx-l79-grant-" not in call["path"] for call in api.calls)


def test_app_observation_records_declared_resources_without_mutation(monkeypatch) -> None:
    import base64
    from types import SimpleNamespace as NS

    toml = 'schema_version = 1\n[agent]\nframework = "openai"\n'
    deployment = NS(
        status=NS(state="SUCCEEDED"),
        deployment_id="dep-1",
        source_code_path="/Workspace/probe",
    )
    app = NS(
        name="authorized-probe",
        app_status=NS(state="RUNNING"),
        compute_status=NS(state="ACTIVE"),
        active_deployment=deployment,
        resources=[NS(name="postgres-runtime-store")],
    )
    ws = MagicMock()
    ws.config.host = "https://sandbox.invalid"
    ws.apps.get.return_value = app
    ws.workspace.export.return_value = NS(content=base64.b64encode(toml.encode()).decode())
    monkeypatch.setenv("APX_MANAGED_STATE_APP_NAME", "authorized-probe")
    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
    properties = {}
    probe.test_live_app_declared_resources_observed((ws, None), properties.__setitem__)
    assert properties["app_state"] == "RUNNING"
    assert properties["compute_state"] == "ACTIVE"
    assert properties["resources"] == "postgres-runtime-store"
    assert properties["memory_store"] == "not-declared"
    assert properties["session_store"] == "not-declared"
    assert properties["managed_binding"] == "UNVERIFIED"
    assert properties["process_startup"] == "not-entered"
    ws.apps.start.assert_not_called()
    ws.apps.stop.assert_not_called()
    ws.apps.deploy.assert_not_called()
