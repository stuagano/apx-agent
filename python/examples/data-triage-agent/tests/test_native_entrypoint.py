"""The deployed entrypoint compiles durable and its mounted surface goes live."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def test_app_is_durable_with_live_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("APX_DEV_UI", "0")
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda **k: MagicMock())
    setup = AsyncMock(return_value=None)
    monkeypatch.setattr("apx_agent._wiring.setup_agent", setup)
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr("apx_agent._agents.LlmAgent.fetch_remote_tools", fetch)
    sys.modules.pop("app", None)
    app = importlib.import_module("app").app

    from databricks_agentkit.runtime.app import DurableAgentServer

    assert isinstance(app, DurableAgentServer)
    paths = {r.path for r in app.routes}
    assert {"/readyz", "/api/version"} <= paths
    assert any(p.startswith("/mcp") for p in paths)
    with TestClient(app) as client:
        setup.assert_awaited()             # mount startup ran under the durable lifespan
        fetch.assert_awaited()             # sub-agents materialized at durable startup
        assert client.get("/readyz").status_code == 200
