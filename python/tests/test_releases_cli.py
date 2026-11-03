"""Public native Apps release commands; no workspace calls leave the process."""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from apx_agent.cli import main


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch) -> Mock:
    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", factory)
    return factory


@pytest.mark.parametrize("as_json", [False, True])
def test_native_history_shows_exact_active_identity_and_snapshot(
    monkeypatch: pytest.MonkeyPatch, workspace: Mock, as_json: bool,
) -> None:
    metadata = {
        "app_name": "native-app", "active_deployment_id": "id-exact-active",
        "deployments": [{
            "deployment_id": "id-exact-active", "active": True,
            "state": "SUCCEEDED", "mode": "SNAPSHOT",
            "snapshot_source_code_path": "/Workspace/snapshots/exact",
            "create_time": "2026-11-03T00:00:00Z", "update_time": None,
        }],
    }
    reader = Mock(return_value=metadata)
    monkeypatch.setattr("apx_agent._app_releases.list_app_releases", reader)
    args = ["releases", "history", "--app-name", "native-app", "--profile", "release"]
    if as_json:
        args.append("--json")
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    workspace.assert_called_once_with(profile="release")
    reader.assert_called_once_with(workspace.return_value, "native-app")
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["active_deployment_id"] == "id-exact-active"
        assert payload["deployments"] == metadata["deployments"]
    else:
        assert "Active deployment: id-exact-active" in result.stdout
        assert "SUCCEEDED" in result.stdout
        assert "/Workspace/snapshots/exact" in result.stdout


def test_native_rollback_restores_exact_requested_deployment_without_prompt(
    monkeypatch: pytest.MonkeyPatch, workspace: Mock,
) -> None:
    restored = {
        "app_name": "native-app", "restored_from_deployment_id": "old-id-exact",
        "deployment_id": "new-id-exact", "active": True, "state": "SUCCEEDED",
        "snapshot_source_code_path": "/Workspace/snapshots/old",
    }
    restore = Mock(return_value=restored)
    monkeypatch.setattr("apx_agent._app_releases.restore_app_release", restore)
    result = CliRunner().invoke(main, [
        "releases", "rollback", "--app-name", "native-app",
        "--deployment-id", "old-id-exact", "--profile", "release", "--json",
    ])
    assert result.exit_code == 0, result.output
    restore.assert_called_once_with(workspace.return_value, "native-app", "old-id-exact")
    workspace.assert_called_once_with(profile="release")
    assert json.loads(result.stdout) == {"ok": True, **restored}


@pytest.mark.parametrize("command", ["history", "rollback"])
@pytest.mark.parametrize("as_json", [False, True])
def test_native_release_errors_are_nonsecret(
    monkeypatch: pytest.MonkeyPatch, workspace: Mock, command: str, as_json: bool,
) -> None:
    helper = "list_app_releases" if command == "history" else "restore_app_release"
    monkeypatch.setattr(
        f"apx_agent._app_releases.{helper}",
        Mock(side_effect=RuntimeError("SECRET_ENV_VALUE status message")),
    )
    args = ["releases", command, "--app-name", "native-app", "--profile", "release"]
    if command == "rollback":
        args.extend(["--deployment-id", "old-id"])
    if as_json:
        args.append("--json")
    result = CliRunner().invoke(main, args)
    assert result.exit_code != 0
    assert "SECRET_ENV_VALUE" not in result.output
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert "error" in payload
    else:
        assert "Error:" in result.output


@pytest.mark.parametrize("as_json", [False, True])
def test_native_release_auth_errors_are_nonsecret(
    workspace: Mock, as_json: bool,
) -> None:
    workspace.side_effect = RuntimeError("SECRET_AUTH_VALUE")
    args = ["releases", "history", "--app-name", "native-app"]
    if as_json:
        args.append("--json")
    result = CliRunner().invoke(main, args)
    assert result.exit_code != 0
    assert "SECRET_AUTH_VALUE" not in result.output
    if as_json:
        assert json.loads(result.stdout)["ok"] is False


def test_native_rollback_requires_exact_deployment_before_any_workspace_call(workspace: Mock) -> None:
    result = CliRunner().invoke(main, ["releases", "rollback", "--app-name", "native-app"])
    assert result.exit_code != 0
    assert "--deployment-id" in result.output
    workspace.assert_not_called()


def test_native_history_reports_empty_history_and_active_id(
    monkeypatch: pytest.MonkeyPatch, workspace: Mock,
) -> None:
    monkeypatch.setattr("apx_agent._app_releases.list_app_releases", Mock(return_value={
        "app_name": "native-app", "active_deployment_id": "active-not-in-history", "deployments": [],
    }))
    result = CliRunner().invoke(main, ["releases", "history", "--app-name", "native-app"])
    assert result.exit_code == 0, result.output
    assert "Active deployment: active-not-in-history" in result.stdout
    assert "No native App deployments recorded" in result.stdout
