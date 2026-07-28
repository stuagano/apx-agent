"""Workspace-backed Apps deploy state (Foundry-inspired, Apps-only).

Source of truth lives in the Databricks workspace::

    /Shared/apx-agent/<app_name>/_state/<bundle_target>.json

Local ``_state/`` is never written (avoids stale/fork drift). Save failures
are best-effort for callers that treat deploy success as primary.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_STATE_ROOT = "/Shared/apx-agent"
_MAX_AUDIT_ENTRIES = 50


class AuditEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str
    deployer: str = ""
    timestamp: str
    resource_id: str = ""


class ApxAppDeployState(BaseModel):
    """Durable record of a successful Apps deploy for one bundle target."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    app_name: str
    bundle_target: str
    app_url: str | None = None
    experiment_id: str | None = None
    framework_ref: str | None = None
    running_sha: str | None = None
    wheel_name: str | None = None
    deployed_at: str
    audit: list[AuditEntry] = Field(default_factory=list, alias="_audit")

    def model_dump_workspace(self) -> dict[str, Any]:
        """Serialize with ``_audit`` key for workspace JSON."""
        data = self.model_dump(by_alias=True, exclude_none=False)
        return data


def workspace_state_path(app_name: str, bundle_target: str) -> str:
    return f"{_STATE_ROOT}/{app_name}/_state/{bundle_target}.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_workspace_file(ws: Any, path: str) -> dict[str, Any] | None:
    try:
        resp = ws.workspace.export(path)
        content = resp.content
        if content is None:
            return None
        raw = content.decode("utf-8") if isinstance(content, bytes) else content
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = json.loads(base64.b64decode(raw).decode("utf-8"))
        if isinstance(decoded, dict):
            return decoded
        return None
    except Exception:
        return None


def _write_workspace_file(ws: Any, path: str, data: dict[str, Any]) -> None:
    from databricks.sdk.service.workspace import ImportFormat

    parent = path.rsplit("/", 1)[0]
    ws.workspace.mkdirs(parent)
    ws.workspace.upload(
        path=path,
        content=io.BytesIO(json.dumps(data, indent=2).encode("utf-8")),
        format=ImportFormat.AUTO,
        overwrite=True,
    )


def load_deploy_state(
    ws: Any,
    app_name: str,
    bundle_target: str,
) -> ApxAppDeployState | None:
    """Load deploy state from the workspace, or ``None`` if missing."""
    raw = _read_workspace_file(ws, workspace_state_path(app_name, bundle_target))
    if raw is None:
        return None
    try:
        return ApxAppDeployState.model_validate(raw)
    except Exception as exc:
        logger.warning("invalid deploy state for %s/%s: %s", app_name, bundle_target, exc)
        return None


def save_deploy_state(
    ws: Any,
    state: ApxAppDeployState,
    *,
    deployer: str = "",
    action: str = "update",
) -> None:
    """Persist ``state`` to the workspace, appending an audit entry."""
    path = workspace_state_path(state.app_name, state.bundle_target)
    existing = _read_workspace_file(ws, path) or {}
    prior_audit = existing.get("_audit") or []
    if not isinstance(prior_audit, list):
        prior_audit = []

    entries = [
        AuditEntry.model_validate(e) if isinstance(e, dict) else e
        for e in prior_audit
        if isinstance(e, (dict, AuditEntry))
    ]
    if deployer or action:
        entries.append(
            AuditEntry(
                action=action,
                deployer=deployer,
                timestamp=utc_now_iso(),
                resource_id=state.app_name,
            )
        )
    state.audit = entries[-_MAX_AUDIT_ENTRIES:]
    _write_workspace_file(ws, path, state.model_dump_workspace())


def delete_deploy_state(ws: Any, app_name: str, bundle_target: str) -> None:
    """Delete the workspace state file if present (best-effort)."""
    path = workspace_state_path(app_name, bundle_target)
    try:
        ws.workspace.delete(path)
    except Exception as exc:
        logger.debug("delete_deploy_state(%s): %s", path, exc)


def resolve_deployer(ws: Any | None = None) -> str:
    """Best-effort current user email / name for audit."""
    try:
        client = ws
        if client is None:
            from databricks.sdk import WorkspaceClient

            client = WorkspaceClient()
        me = client.current_user.me()
        return getattr(me, "user_name", None) or getattr(me, "userName", None) or ""
    except Exception:
        return ""


__all__ = [
    "ApxAppDeployState",
    "AuditEntry",
    "delete_deploy_state",
    "load_deploy_state",
    "resolve_deployer",
    "save_deploy_state",
    "utc_now_iso",
    "workspace_state_path",
]
