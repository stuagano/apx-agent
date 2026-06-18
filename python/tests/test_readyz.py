"""Tests for _readyz.py — the /readyz capability self-test endpoint.

Unlike /health (liveness — 200 once the server booted), /readyz proves the
agent can actually answer a canned prompt and that an MLflow trace was
recorded for the run. It is later called by ``apx-agent deploy`` as a gate (Slice C).

The handler delegates the agent-run step to the module-global
``_run_canned_probe`` helper, which these tests monkeypatch so nothing hits
the network or a live model.
"""

from __future__ import annotations

import apx_agent._readyz as readyz_mod
from apx_agent import Agent, mount_readyz
from apx_agent._readyz import ProbeResult


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
        return ProbeResult(assistant_text="READY", trace_id="tr-abc123")

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
        return ProbeResult(assistant_text="READY", trace_id=None)

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
        return ProbeResult(assistant_text="", trace_id="tr-abc123")

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


class TestReadyzMemory:
    def _app_for(self, agent):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from apx_agent._readyz import mount_readyz
        import apx_agent._readyz as rz
        # Stub the canned probe so the test doesn't need a real model.
        rz._run_canned_probe = lambda a, m: ProbeResult(assistant_text="hi", trace_id="tr-1")  # type: ignore
        app = FastAPI()
        mount_readyz(app, agent)
        return TestClient(app)

    def test_memory_degraded_surfaced(self):
        from apx_agent import Agent
        agent = Agent(instructions="x", tools=[])
        agent._apx_memory_degraded = "delta memory needs a workspace/warehouse — not active"
        body = self._app_for(agent).get("/readyz").json()
        assert "delta memory needs" in body["checks"]["memory"]

    def test_memory_ok_when_not_degraded(self):
        from apx_agent import Agent
        agent = Agent(instructions="x", tools=[])
        body = self._app_for(agent).get("/readyz").json()
        assert body["checks"]["memory"] == "ok"

    def test_memory_degraded_returns_503(self):
        from apx_agent import Agent
        agent = Agent(instructions="x", tools=[])
        agent._apx_memory_degraded = "delta backend not reachable"
        resp = self._app_for(agent).get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["status"] == "degraded"

    def test_memory_no_config_is_still_ready(self):
        """memory=None (not configured) must not block readiness."""
        from apx_agent import Agent
        agent = Agent(instructions="x", tools=[])
        # _apx_memory_degraded not set — same as None / "ok"
        resp = self._app_for(agent).get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"


class TestReadyzMcp:
    """checks['mcp'] is a tri-state, informational signal — it never gates
    readiness (MCP is an optional, extra-gated surface)."""

    def _app_for(self, agent, *, mcp_mount_error=..., mcp_server=...):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import apx_agent._readyz as rz
        # Stub the canned probe so the test doesn't need a real model.
        rz._run_canned_probe = lambda a, m: ProbeResult(assistant_text="hi", trace_id="tr-1")  # type: ignore
        app = FastAPI()
        # The readyz handler reads MCP status off app.state at request time.
        if mcp_mount_error is not ...:
            app.state.mcp_mount_error = mcp_mount_error
        if mcp_server is not ...:
            app.state.mcp_server = mcp_server
        mount_readyz(app, agent)
        return TestClient(app)

    def test_mcp_intended_but_errored_is_degraded_but_still_ready(self):
        """A real MCP mount failure surfaces as degraded but stays ready/200."""
        from apx_agent import Agent
        agent = Agent(instructions="x", tools=[])
        client = self._app_for(agent, mcp_mount_error="RuntimeError: transport bind failed")
        resp = client.get("/readyz")
        body = resp.json()
        assert body["checks"]["mcp"] == "degraded"
        # Informational only — does NOT gate readiness.
        assert resp.status_code == 200
        assert body["status"] == "ready"

    def test_mcp_ok_when_server_mounted(self):
        from apx_agent import Agent
        agent = Agent(instructions="x", tools=[])
        client = self._app_for(agent, mcp_mount_error=None, mcp_server=object())
        body = client.get("/readyz").json()
        assert body["checks"]["mcp"] == "ok"

    def test_mcp_not_configured_stays_ready(self):
        """Missing mcp extra / never set up → not-configured, still ready/200."""
        from apx_agent import Agent
        agent = Agent(instructions="x", tools=[])
        # Neither mcp_mount_error nor mcp_server set on app.state.
        resp = self._app_for(agent).get("/readyz")
        body = resp.json()
        assert body["checks"]["mcp"] == "not-configured"
        assert resp.status_code == 200
        assert body["status"] == "ready"
