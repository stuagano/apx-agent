"""Shared helpers for the horizontal-scaling gate tests.

The runtime boot guard lives in ``create_app``'s lifespan, so these tests drive
it via ``TestClient(app)`` (context-manager entry runs startup). We stub out the
workspace client, MCP, and tracing so nothing hits the network — mirroring
``tests/test_readyz.py::_stub_create_app_startup``.
"""
from __future__ import annotations

import pytest


def _trivial_tool(query: str) -> str:
    """A dependency-free tool so the agent compiles."""
    return f"got: {query}"


@pytest.fixture
def stub_startup(monkeypatch):
    import apx_agent._wiring as wiring

    async def _no_mcp(*_args, **_kwargs):
        from contextlib import nullcontext

        return nullcontext()

    monkeypatch.setattr(wiring, "_make_workspace_client", lambda: None)
    monkeypatch.setattr(wiring, "_setup_mcp", _no_mcp)
    monkeypatch.setattr(
        "apx_agent._mlflow_tracing.autolog_if_env", lambda: None, raising=False
    )
    monkeypatch.setattr(
        "apx_agent._trace_store.install_capture_processor_at_startup",
        lambda: None,
        raising=False,
    )


def boot_app(config):
    """Build a served app for *config* and enter its lifespan (runs the guard)."""
    from fastapi.testclient import TestClient

    from apx_agent import Agent, create_app

    app = create_app(Agent(tools=[_trivial_tool]), config)
    with TestClient(app):
        pass
