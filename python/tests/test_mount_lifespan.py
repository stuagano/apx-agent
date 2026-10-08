"""mount_mcp_endpoints must start and stop under any host lifespan."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apx_agent import LlmAgent, mount_mcp_endpoints


def _host(kind: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FastAPI:
    if kind == "fastapi":
        return FastAPI()
    from databricks_agentkit.runtime.app import DurableAgentServer

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    app = DurableAgentServer()
    app.invoke(lambda value, context: {})
    return app


@pytest.mark.parametrize("kind", ["durable", "fastapi"])
def test_mount_lifecycle_runs_under_any_lifespan(
    kind: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    events: list[str] = []

    class Lifecycle:
        async def __aenter__(self) -> None:
            events.append("enter")

        async def __aexit__(self, *exc: BaseException | None) -> None:
            events.append("exit")

    monkeypatch.setenv("APX_DEV_UI", "0")
    monkeypatch.setattr("apx_agent._wiring.setup_agent", AsyncMock(return_value="ctx"))
    monkeypatch.setattr("apx_agent._wiring._setup_mcp", AsyncMock(return_value=Lifecycle()))
    app = _host(kind, monkeypatch, tmp_path)
    mount_mcp_endpoints(app, LlmAgent(name="mounted"))
    with TestClient(app):
        assert events == ["enter"]
    assert events == ["enter", "exit"]
