"""Tests for _readyz.py — the /readyz capability self-test endpoint.

Unlike /health (liveness — 200 once the server booted), /readyz proves the
agent can actually answer a canned prompt and that an MLflow trace was
recorded for the run. It is later called by ``apx deploy`` as a gate (Slice C).

The handler delegates the agent-run step to the module-global
``_run_canned_probe`` helper, which these tests monkeypatch so nothing hits
the network or a live model.
"""

from __future__ import annotations

import apx_agent._readyz as readyz_mod
from apx_agent import Agent, mount_readyz


def _make_app(agent: Agent):
    from fastapi import FastAPI

    app = FastAPI()
    mount_readyz(app, agent)
    return app


def _trivial_tool(query: str) -> str:
    """A tool with no dependencies (compile-friendly)."""
    return f"got: {query}"


# ---------------------------------------------------------------------------
# Import / export
# ---------------------------------------------------------------------------


def test_mount_readyz_importable_from_package() -> None:
    from apx_agent import mount_readyz as _mr  # noqa: F401

    assert callable(_mr)


# ---------------------------------------------------------------------------
# Ready path
# ---------------------------------------------------------------------------


def test_readyz_ready_when_llm_and_trace_ok(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    agent = Agent(tools=[_trivial_tool])

    def _fake_probe(_agent, _model):
        return ("READY", "tr-abc123")

    monkeypatch.setattr(readyz_mod, "_run_canned_probe", _fake_probe)

    client = TestClient(_make_app(agent))
    resp = client.get("/readyz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["llm"] == "ok"
    assert body["checks"]["tracing"] == "ok"
    # tools_registered is informational — the agent has one tool.
    assert body["checks"]["tools_registered"] == 1
    # tool_exec is best-effort and skipped by default.
    assert body["checks"]["tool_exec"] == "skipped"


def test_readyz_ready_when_tracing_unavailable(monkeypatch) -> None:
    """No trace id (mlflow off / no trace) → tracing 'unavailable' but still ready."""
    from fastapi.testclient import TestClient

    agent = Agent(tools=[_trivial_tool])

    def _fake_probe(_agent, _model):
        return ("READY", None)

    monkeypatch.setattr(readyz_mod, "_run_canned_probe", _fake_probe)

    client = TestClient(_make_app(agent))
    resp = client.get("/readyz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["llm"] == "ok"
    assert body["checks"]["tracing"] == "unavailable"


# ---------------------------------------------------------------------------
# Degraded paths
# ---------------------------------------------------------------------------


def test_readyz_degraded_when_llm_empty(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    agent = Agent(tools=[_trivial_tool])

    def _fake_probe(_agent, _model):
        return ("", "tr-abc123")

    monkeypatch.setattr(readyz_mod, "_run_canned_probe", _fake_probe)

    client = TestClient(_make_app(agent))
    resp = client.get("/readyz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["llm"] == "fail"


def test_readyz_degraded_when_probe_raises(monkeypatch) -> None:
    """Unexpected error in the run step → never 500; 503 degraded with short error."""
    from fastapi.testclient import TestClient

    agent = Agent(tools=[_trivial_tool])

    def _fake_probe(_agent, _model):
        raise RuntimeError("boom: model endpoint unreachable")

    monkeypatch.setattr(readyz_mod, "_run_canned_probe", _fake_probe)

    client = TestClient(_make_app(agent))
    resp = client.get("/readyz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert "error" in body
    assert isinstance(body["error"], str) and body["error"]


def test_readyz_never_500s(monkeypatch) -> None:
    """Even a bizarre failure stays a 503, not a 500."""
    from fastapi.testclient import TestClient

    agent = Agent(tools=[_trivial_tool])

    def _fake_probe(_agent, _model):
        raise ValueError("unexpected")

    monkeypatch.setattr(readyz_mod, "_run_canned_probe", _fake_probe)

    client = TestClient(_make_app(agent))
    resp = client.get("/readyz")
    assert resp.status_code == 503
