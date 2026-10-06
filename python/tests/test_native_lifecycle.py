"""Native teardown exercises installed ownership checks against a fake workspace."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.service.apps import App

from apx_agent import AgentConfig
from apx_agent._project_gen import generate_project
from apx_agent.cli import main


@pytest.mark.parametrize("entry", ["destroy", "delete", "canary", "space"])
@pytest.mark.parametrize("failure", [None, "owner", "cleanup", "app", "principal", "read"])
def test_native_teardown_retains_identity_until_cleanup_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, failure: str | None,
) -> None:
    import apx_agent.cli as cli

    name = "named-space-app" if entry == "space" else "agent-bricks-proof"
    if entry == "canary":
        name += "-canary-1"
    generate_project(AgentConfig(name="proof", target="durable_agent_server"), tmp_path)
    monkeypatch.chdir(tmp_path)
    ws = MagicMock()
    ws.apps.get.return_value = App(name=name, service_principal_client_id="app-sp",
                                   space="shared-space" if entry == "space" else None)
    if failure == "principal":
        ws.apps.get.return_value.service_principal_client_id = None
    if failure == "read":
        ws.apps.get.side_effect = PermissionDenied("synthetic-secret")
    store = {"name": f"runtime-stores/{name}",
             "owner": {"app": {"name": name, "service_principal_id": "other-sp" if failure == "owner" else "app-sp"}}}
    remaining = {"runtime": True, "app": True, "sessions": True, "memory": True}
    events = []

    def api(method, path, **kwargs):
        if entry == "canary" and path.endswith("/agent-bricks-proof"):
            assert method == "GET"
            raise NotFound("parent already deleted", error_code="NOT_FOUND")
        assert path == f"/api/2.0/agents/runtime-stores/{name}"
        events.append(method)
        if not remaining["runtime"]:
            raise NotFound("gone", error_code="NOT_FOUND")
        if method == "GET":
            return store
        assert method == "DELETE"
        if failure == "cleanup":
            raise PermissionDenied("synthetic-secret")
        remaining["runtime"] = False
        return {}

    def delete_app(app_name):
        assert app_name == name and not remaining["runtime"]
        events.append("app")
        if failure == "app":
            raise PermissionDenied("synthetic-secret")
        remaining["app"] = False

    ws.api_client.do.side_effect = api
    ws.apps.delete.side_effect = delete_app
    ws.registered_models.get.return_value = SimpleNamespace(tags=[])
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda **kw: ws)
    monkeypatch.setattr(cli, "_connect_workspace", lambda profile: (ws, SimpleNamespace(host="https://workspace.example.com")))
    monkeypatch.setattr(cli, "load_deploy_state", lambda *a: None)
    cleared = MagicMock()
    monkeypatch.setattr(cli, "delete_deploy_state", cleared)
    bundle = MagicMock(side_effect=AssertionError("native teardown must not call Bundle"))
    monkeypatch.setattr(cli, "_run_databricks_cmd", bundle)
    if entry == "destroy":
        args = ["destroy", "--profile", "chosen", "--yes", "--json-output"]
    else:
        args = ["agents", "delete", "--uc-name", "main.agents.proof", "--profile", "chosen", "--yes", "--json"]
        if entry == "canary":
            canary = ws.apps.get.return_value

            def get_app(app_name):
                if app_name == "agent-bricks-proof":
                    raise NotFound("parent already deleted")
                assert app_name == name
                if failure == "read":
                    raise PermissionDenied("synthetic-secret")
                return canary

            ws.apps.get.side_effect = get_app
            ws.apps.list.return_value = [App(name=name)]
            # Canary discovery uses the deployed model's App family tag.
            ws.registered_models.get.return_value = SimpleNamespace(tags=[
                SimpleNamespace(key="apx.apps.app_name", value="agent-bricks-proof"),
            ])
            args.extend(["--purge"])
        else:
            args.extend(["--app", name])
    result = CliRunner().invoke(main, args)
    assert result.exit_code == (1 if failure else 0), result.output
    assert "synthetic-secret" not in result.output
    if failure:
        assert remaining["app"]
        cleared.assert_not_called()
        ws.registered_models.delete.assert_not_called()
        ws.serving_endpoints.delete.assert_not_called()
        if failure != "app":
            ws.apps.delete.assert_not_called()
            assert remaining["runtime"]
    else:
        assert not remaining["runtime"] and not remaining["app"]
        assert events == ["GET", "DELETE", "app"]
    assert remaining["sessions"] and remaining["memory"]
    bundle.assert_not_called()
    # If App deletion failed after store cleanup, retry must accept NOT_FOUND.
    if failure == "app":
        failure = None
        retry = CliRunner().invoke(main, args)
        assert retry.exit_code == 0, retry.output
        assert not remaining["app"] and events[-2:] == ["GET", "app"]


def test_native_status_and_logs_without_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import apx_agent.cli as cli

    generate_project(AgentConfig(name="proof", target="durable_agent_server"), tmp_path)
    monkeypatch.chdir(tmp_path)
    ws = MagicMock()
    ws.apps.get.return_value = App(name="agent-bricks-proof", url="https://proof.databricksapps.com")
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda **kw: ws)
    monkeypatch.setattr(cli, "load_deploy_state", lambda *a: None)
    result = CliRunner().invoke(main, ["status", "--profile", "chosen", "--json-output"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["deployment"]["url"] == "https://proof.databricksapps.com"
    run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="native runtime log", stderr=""))
    monkeypatch.setattr("subprocess.run", run)
    result = CliRunner().invoke(main, ["agents", "logs", "--profile", "chosen"])
    assert result.exit_code == 0 and "native runtime log" in result.stdout
    assert run.call_args.args[0] == ["databricks", "apps", "logs", "agent-bricks-proof", "--profile", "chosen"]


@pytest.mark.parametrize("failure", [False, True])
def test_space_bundle_resolves_target_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool,
) -> None:
    import apx_agent.cli as cli

    generate_project(AgentConfig(name="proof", target="durable_agent_server", deploy={"space": "shared"}), tmp_path)
    monkeypatch.chdir(tmp_path)
    name = "proof-prod"
    ws = MagicMock()
    ws.apps.get.return_value = App(name=name, space="shared", service_principal_client_id="sp")
    events = []

    def api(method, path, **kwargs):
        assert path.endswith(f"/runtime-stores/{name}")
        events.append(method)
        if method == "GET":
            return {"name": f"runtime-stores/{name}", "owner": {"app": {"name": name, "service_principal_id": "sp"}}}
        if failure:
            raise PermissionDenied("denied")
        return {}

    def bundle(args, profile=None):
        assert profile == "chosen" and args[3] == "prod"
        events.append(args[1])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"resources": {"apps": {"proof": {"name": name}}}}))

    ws.api_client.do.side_effect = api
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda **kw: ws)
    monkeypatch.setattr(cli, "load_deploy_state", lambda *a: None)
    clear = MagicMock()
    monkeypatch.setattr(cli, "delete_deploy_state", clear)
    monkeypatch.setattr(cli, "_run_databricks_cmd", bundle)
    result = CliRunner().invoke(main, ["destroy", "--bundle-target", "prod", "--profile", "chosen", "--yes", "--json-output"])
    assert result.exit_code == int(failure), result.output
    assert events == ["validate", "GET", "DELETE"] + ([] if failure else ["destroy"])
    ws.apps.get.assert_called_once_with(name)
    ws.apps.delete.assert_not_called()
    if failure:
        clear.assert_not_called()
    else:
        clear.assert_called_once_with(ws, name, "prod")


def test_orphaned_store_is_not_deleted_without_app_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    import click
    from apx_agent._agentbricks_deploy import cleanup_app_runtime

    ws = MagicMock()
    ws.apps.get.side_effect = NotFound("gone", error_code="NOT_FOUND")
    ws.api_client.do.return_value = {"name": "runtime-stores/agent-bricks-proof"}
    with pytest.raises(click.ClickException, match="cleanup failed"):
        cleanup_app_runtime(ws, "agent-bricks-proof")
    assert [call.args[0] for call in ws.api_client.do.call_args_list] == ["GET"]
    ws.apps.delete.assert_not_called()
