"""Native release contracts; SDK boundaries are mocked, never a cloud deploy."""
from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import Mock

import pytest
from databricks.sdk.service.apps import (
    App, AppDeployment, AppDeploymentArtifacts, AppDeploymentMode,
    AppDeploymentState, AppDeploymentStatus, AppResource, EnvVar,
)

from apx_agent._app_releases import list_app_releases, restore_app_release


def deployment(deployment_id: str = "selected") -> AppDeployment:
    return AppDeployment(
        deployment_id=deployment_id,
        source_code_path="/Workspace/mutable-original",
        deployment_artifacts=AppDeploymentArtifacts(source_code_path=f"/Workspace/snapshots/{deployment_id}"),
        mode=AppDeploymentMode.SNAPSHOT,
        status=AppDeploymentStatus(state=AppDeploymentState.SUCCEEDED, message="hidden-secret"),
        command=["python", "entry.py", "hidden-command-secret"],
        env_vars=[EnvVar(name="TOKEN", value="hidden-env-secret")],
    )


@pytest.fixture
def workspace() -> Mock:
    w = Mock()
    selected = deployment()
    rolled = deployment("new")
    rolled.source_code_path = selected.deployment_artifacts.source_code_path
    w.apps.get_deployment.side_effect = [selected, rolled]
    w.apps.get.side_effect = [App(name="prod"), App(name="prod", active_deployment=rolled)]
    w.apps.deploy.return_value.response = AppDeployment(deployment_id="new")
    w.apps.wait_get_deployment_app_succeeded.return_value = rolled
    return w


def test_history_uses_active_not_latest_and_omits_secrets() -> None:
    w = Mock()
    active = deployment("older")
    w.apps.get.return_value = App(name="prod", active_deployment=active)
    w.apps.list_deployments.return_value = iter([deployment("latest"), active])
    result = list_app_releases(w, "prod")
    assert result["active_deployment_id"] == "older"
    assert [d["active"] for d in result["deployments"]] == [False, True]
    serialized = json.dumps(result)
    assert "hidden" not in serialized
    assert "env_vars" not in serialized
    assert "command" not in serialized
    assert result["deployments"][1]["snapshot_source_code_path"] == "/Workspace/snapshots/older"
    w.apps.get.assert_called_once_with("prod")
    w.apps.list_deployments.assert_called_once_with("prod")


def test_history_without_active_never_infers_latest() -> None:
    w = Mock()
    w.apps.get.return_value = App(name="prod")
    w.apps.list_deployments.return_value = [deployment()]
    result = list_app_releases(w, "prod")
    assert result["active_deployment_id"] is None
    assert result["deployments"][0]["active"] is False


def test_restore_exact_snapshot_preserves_overrides_and_reads_back(workspace: Mock) -> None:
    result = restore_app_release(workspace, "prod", "selected", timeout=timedelta(seconds=9))
    request = workspace.apps.deploy.call_args.kwargs["app_deployment"]
    assert request.source_code_path == "/Workspace/snapshots/selected"
    assert request.mode == AppDeploymentMode.SNAPSHOT
    assert request.command == ["python", "entry.py", "hidden-command-secret"]
    assert request.env_vars == [EnvVar(name="TOKEN", value="hidden-env-secret")]
    assert request.deployment_id is None
    assert request.deployment_artifacts is None
    assert request.status is None
    assert workspace.apps.deploy.call_args.kwargs["app_name"] == "prod"
    assert workspace.apps.get_deployment.call_args_list[0].kwargs == {
        "app_name": "prod", "deployment_id": "selected",
    }
    assert workspace.apps.get_deployment.call_args_list[1].kwargs == {
        "app_name": "prod", "deployment_id": "new",
    }
    workspace.apps.wait_get_deployment_app_succeeded.assert_called_once_with(
        app_name="prod", deployment_id="new", timeout=timedelta(seconds=9),
    )
    assert result["deployment_id"] == "new"
    assert result["active"] is True
    assert result["snapshot_source_code_path"] == "/Workspace/snapshots/new"
    assert "hidden" not in json.dumps(result)


@pytest.mark.parametrize("change", ["missing_artifacts", "missing_path", "failed", "auto_sync", "wrong_id"])
def test_invalid_source_fails_before_mutation(workspace: Mock, change: str) -> None:
    selected = deployment()
    if change == "missing_artifacts":
        selected.deployment_artifacts = None
    elif change == "missing_path":
        selected.deployment_artifacts.source_code_path = None
    elif change == "failed":
        selected.status.state = AppDeploymentState.FAILED
    elif change == "auto_sync":
        selected.mode = AppDeploymentMode.AUTO_SYNC
    else:
        selected.deployment_id = "not-selected"
    workspace.apps.get_deployment.side_effect = [selected]
    with pytest.raises(ValueError):
        restore_app_release(workspace, "prod", "selected")
    workspace.apps.deploy.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("user_api_scopes", ["sql"]),
    ("effective_user_api_scopes", ["sql"]),
    ("forward_user_access_token", True),
    ("resources", [AppResource(name="warehouse")]),
    ("space", "different"),
])
def test_incompatible_promotion_fails_before_mutation(workspace: Mock, field: str, value) -> None:
    source = App(name="stage")
    destination = App(name="prod")
    setattr(destination, field, value)
    workspace.apps.get.side_effect = [source, destination]
    with pytest.raises(ValueError, match="governance"):
        restore_app_release(workspace, "stage", "selected", destination_app="prod")
    workspace.apps.deploy.assert_not_called()


def test_even_matching_cross_app_config_cannot_prove_baked_identity_safe(workspace: Mock) -> None:
    workspace.apps.get.side_effect = [App(name="stage", service_principal_id=1), App(name="prod", service_principal_id=2)]
    with pytest.raises(ValueError, match="Cross-App promotion.*app.yaml"):
        restore_app_release(workspace, "stage", "selected", destination_app="prod")
    workspace.apps.deploy.assert_not_called()


def test_destination_must_exist_before_deploy(workspace: Mock) -> None:
    workspace.apps.get.side_effect = RuntimeError("missing app")
    with pytest.raises(RuntimeError, match="missing app"):
        restore_app_release(workspace, "prod", "selected")
    workspace.apps.deploy.assert_not_called()


def test_pending_rollout_rejected_before_mutation(workspace: Mock) -> None:
    workspace.apps.get.side_effect = [App(name="prod", pending_deployment=deployment("pending"))]
    with pytest.raises(ValueError, match="pending"):
        restore_app_release(workspace, "prod", "selected")
    workspace.apps.deploy.assert_not_called()


@pytest.mark.parametrize("failure", ["wait", "failed_readback", "wrong_active", "no_snapshot", "wrong_source", "wrong_overrides", "reused_id"])
def test_rollout_success_requires_readback(workspace: Mock, failure: str) -> None:
    selected = deployment()
    rolled = deployment("new")
    rolled.source_code_path = "/Workspace/snapshots/selected"
    workspace.apps.get_deployment.side_effect = [selected, rolled]
    workspace.apps.get.side_effect = [App(name="prod"), App(name="prod", active_deployment=rolled)]
    if failure == "wait":
        workspace.apps.wait_get_deployment_app_succeeded.side_effect = RuntimeError("hidden-env-secret")
    elif failure == "failed_readback":
        rolled.status.state = AppDeploymentState.FAILED
    elif failure == "wrong_active":
        workspace.apps.get.side_effect = [App(name="prod"), App(name="prod", active_deployment=selected)]
    elif failure == "no_snapshot":
        rolled.deployment_artifacts = None
    elif failure == "wrong_source":
        rolled.source_code_path = "/Workspace/mutable-original"
    elif failure == "wrong_overrides":
        rolled.env_vars = None
    else:
        workspace.apps.deploy.return_value.response.deployment_id = "selected"
    with pytest.raises(RuntimeError) as error:
        restore_app_release(workspace, "prod", "selected")
    assert "hidden" not in str(error.value)


def test_nonpositive_timeout_rejected_before_mutation(workspace: Mock) -> None:
    with pytest.raises(ValueError, match="timeout"):
        restore_app_release(workspace, "prod", "selected", timeout=timedelta(0))
    workspace.apps.deploy.assert_not_called()
