"""Unit tests for workspace-backed Apps deploy state."""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import MagicMock

from apx_agent._deploy_state import (
    ApxAppDeployState,
    delete_deploy_state,
    load_deploy_state,
    save_deploy_state,
    utc_now_iso,
    workspace_state_path,
)


def test_workspace_state_path() -> None:
    assert workspace_state_path("my_agent", "staging") == (
        "/Shared/apx-agent/my_agent/_state/staging.json"
    )


def test_save_load_roundtrip_via_mock_ws() -> None:
    store: dict[str, bytes] = {}

    class _Resp:
        def __init__(self, content: str | bytes) -> None:
            self.content = content

    ws = MagicMock()

    def export(path: str) -> _Resp:
        if path not in store:
            raise FileNotFoundError(path)
        return _Resp(store[path])

    def upload(*, path: str, content: Any, format: Any = None, overwrite: bool = False) -> None:
        data = content.read() if hasattr(content, "read") else content
        store[path] = data if isinstance(data, bytes) else str(data).encode("utf-8")

    def delete(path: str) -> None:
        store.pop(path, None)

    ws.workspace.export.side_effect = export
    ws.workspace.upload.side_effect = upload
    ws.workspace.delete.side_effect = delete
    ws.workspace.mkdirs = MagicMock()

    state = ApxAppDeployState(
        app_name="my_agent",
        bundle_target="dev",
        app_url="https://my_agent.example.databricksapps.com",
        experiment_id="123",
        framework_ref="abc123",
        running_sha="abc123def",
        wheel_name="apx_agent-0.1.0-py3-none-any.whl",
        deployed_at=utc_now_iso(),
    )
    save_deploy_state(ws, state, deployer="alice@databricks.com", action="update")

    path = workspace_state_path("my_agent", "dev")
    assert path in store
    loaded = load_deploy_state(ws, "my_agent", "dev")
    assert loaded is not None
    assert loaded.app_name == "my_agent"
    assert loaded.app_url and "databricksapps.com" in loaded.app_url
    assert loaded.experiment_id == "123"
    assert len(loaded.audit) == 1
    assert loaded.audit[0].deployer == "alice@databricks.com"

    delete_deploy_state(ws, "my_agent", "dev")
    assert path not in store
    assert load_deploy_state(ws, "my_agent", "dev") is None


def test_load_accepts_base64_export() -> None:
    payload = {
        "app_name": "x",
        "bundle_target": "prod",
        "deployed_at": "2026-07-28T00:00:00Z",
        "_audit": [],
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")

    class _Resp:
        content = encoded

    ws = MagicMock()
    ws.workspace.export.return_value = _Resp()
    loaded = load_deploy_state(ws, "x", "prod")
    assert loaded is not None
    assert loaded.bundle_target == "prod"


def test_load_missing_returns_none() -> None:
    ws = MagicMock()
    ws.workspace.export.side_effect = Exception("missing")
    assert load_deploy_state(ws, "nope", "dev") is None
