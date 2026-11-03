"""Native Databricks Apps release history and same-App snapshot rollback.

The artifact path is the platform's stable packaged-source snapshot, NOT the
original mutable ``source_code_path``. Re-deployment may reinstall dependencies;
this does not promise an identical container or historical App permissions.
Cross-App promotion is deliberately unsupported: matching App resources/scopes
cannot prove that app.yaml (or source code) is free of baked stage identity and
secrets. No identity, resources, permissions, or App configuration are copied.
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import timedelta
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.apps import (
    App,
    AppDeployment,
    AppDeploymentMode,
    AppDeploymentState,
)


def _release_metadata(deployment: AppDeployment, active_id: str | None) -> dict[str, Any]:
    """Allowlist metadata; never serialize commands, env values, or status messages."""
    artifacts = deployment.deployment_artifacts
    return {
        "deployment_id": deployment.deployment_id,
        "active": deployment.deployment_id is not None and deployment.deployment_id == active_id,
        "mode": deployment.mode.value if deployment.mode else None,
        "state": deployment.status.state.value if deployment.status and deployment.status.state else None,
        "snapshot_source_code_path": artifacts.source_code_path if artifacts else None,
        "create_time": deployment.create_time,
        "update_time": deployment.update_time,
    }


def list_app_releases(workspace: WorkspaceClient, app_name: str) -> dict[str, Any]:
    """Read native history and the authoritative active ID, never infer latest=active.

    The two platform reads are not atomic. This reports the active ID observed by
    apps.get, including when that ID is absent from the returned history.
    """
    app = workspace.apps.get(app_name)
    active_id = app.active_deployment.deployment_id if app.active_deployment else None
    return {
        "app_name": app_name,
        "active_deployment_id": active_id,
        "deployments": [
            _release_metadata(deployment, active_id)
            for deployment in workspace.apps.list_deployments(app_name)
        ],
    }


def _snapshot_path(deployment: AppDeployment) -> str:
    if (
        deployment.mode != AppDeploymentMode.SNAPSHOT
        or deployment.status is None
        or deployment.status.state != AppDeploymentState.SUCCEEDED
    ):
        raise ValueError("Release must be a SUCCEEDED SNAPSHOT deployment.")
    artifacts = deployment.deployment_artifacts
    if artifacts is None or not artifacts.source_code_path or not artifacts.source_code_path.strip():
        raise ValueError("Release has no stable deployment_artifacts.source_code_path; mutable source fallback is forbidden.")
    return artifacts.source_code_path


def _governance(app: App) -> dict[str, Any]:
    # Principals are intentionally excluded: distinct Apps have distinct runtime
    # identities. This comparison must never authorize copying those identities.
    return {
        "user_api_scopes": sorted(app.user_api_scopes or []),
        "effective_user_api_scopes": sorted(app.effective_user_api_scopes or []),
        "forward_user_access_token": app.forward_user_access_token,
        "space": app.space,
        "resources": sorted(json.dumps(resource.as_dict(), sort_keys=True) for resource in (app.resources or [])),
    }


def restore_app_release(
    workspace: WorkspaceClient,
    source_app: str,
    deployment_id: str,
    *,
    destination_app: str | None = None,
    timeout: timedelta = timedelta(minutes=20),
) -> dict[str, Any]:
    """Restore exactly one native source snapshot into the same existing App.

    The caller authorizes mutation. Preflight failures never deploy. Command and
    environment overrides are replayed only within the SAME App, using the SDK
    fields which override app.yaml. Current App governance remains in effect;
    historical governance is not available from a deployment record.

    Return only allowlisted metadata after reading back the newly created
    deployment AND its active identity. No ledger or local files are changed.
    There is no server-side compare-and-swap: concurrent deployments may cause
    verification to fail after mutation. Failure never triggers a second deploy.
    """
    if not source_app.strip() or not deployment_id.strip():
        raise ValueError("source_app and deployment_id must be nonempty.")
    if timeout.total_seconds() <= 0:
        raise ValueError("timeout must be positive.")
    target = source_app if destination_app is None else destination_app
    if not target.strip():
        raise ValueError("destination_app must be nonempty.")
    source = workspace.apps.get(source_app)
    destination = source if target == source_app else workspace.apps.get(target)
    if source.name != source_app or destination.name != target:
        raise ValueError("App readback did not match the requested App names.")
    selected = workspace.apps.get_deployment(app_name=source_app, deployment_id=deployment_id)
    if selected.deployment_id != deployment_id:
        raise ValueError("Source deployment readback did not match the selected deployment_id.")
    path = _snapshot_path(selected)
    if _governance(source) != _governance(destination):
        raise ValueError("Source and destination App governance configuration differs (scopes/resources/space/token forwarding).")
    if target != source_app:
        raise ValueError(
            "Cross-App promotion is unsupported: app.yaml and source may embed stage identity/env; "
            "App governance equality cannot validate safe destination command/environment configuration."
        )
    if destination.pending_deployment is not None:
        raise ValueError("Destination App has a pending deployment; wait before restoring.")
    request = AppDeployment(
        source_code_path=path,
        mode=AppDeploymentMode.SNAPSHOT,
        command=deepcopy(selected.command),
        env_vars=deepcopy(selected.env_vars),
    )
    # SDK serialization omits empty override lists. None and [] both mean no
    # overrides, so app.yaml in the selected source snapshot supplies them.
    try:
        operation = workspace.apps.deploy(app_name=target, app_deployment=request)
        new_id = operation.response.deployment_id
        if not new_id or new_id == deployment_id:
            raise RuntimeError("No new deployment identity.")
        # Not operation.result(): the SDK's Wait.result declares a non-optional
        # callback with a None default, so calling it fails the pyright gate.
        workspace.apps.wait_get_deployment_app_succeeded(
            app_name=target, deployment_id=new_id, timeout=timeout,
        )
        deployed = workspace.apps.get_deployment(app_name=target, deployment_id=new_id)
        active_app = workspace.apps.get(target)
        active = active_app.active_deployment
        _snapshot_path(deployed)
        if (
            deployed.deployment_id != new_id
            or deployed.source_code_path != path
            or deployed.command != selected.command
            or deployed.env_vars != selected.env_vars
            or active_app.name != target
            or active is None
            or active.deployment_id != new_id
        ):
            raise RuntimeError("Deployment readback mismatch.")
    except Exception:
        # Platform errors/status messages can include command/env values. Avoid
        # leaking their text or chained traceback; CLI may safely print this.
        raise RuntimeError(
            "App snapshot restore failed or could not be verified active; "
            "inspect native App deployment status securely before retrying."
        ) from None
    return {"app_name": target, "restored_from_deployment_id": deployment_id, **_release_metadata(deployed, new_id)}
