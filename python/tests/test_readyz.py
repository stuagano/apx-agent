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


class TestReadyzSession:
    def _client(self, agent, *, degraded):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from apx_agent._readyz import mount_readyz
        import apx_agent._readyz as rz
        rz._run_canned_probe = lambda a, m: ProbeResult(assistant_text="hi", trace_id="tr-1")  # type: ignore
        app = FastAPI()
        app.state.checkpointer_degraded = degraded
        mount_readyz(app, agent)
        return TestClient(app)

    def test_session_degraded_surfaced_and_503(self):
        """#490: a lakebase checkpointer that failed to build must surface as
        session=degraded and gate readiness (not silently green)."""
        from apx_agent import Agent
        agent = Agent(instructions="x", tools=[])
        resp = self._client(agent, degraded=True).get("/readyz")
        assert resp.json()["checks"]["session"] == "degraded"
        assert resp.status_code == 503
        assert resp.json()["status"] == "degraded"

    def test_session_ok_when_not_degraded(self):
        from apx_agent import Agent
        agent = Agent(instructions="x", tools=[])
        resp = self._client(agent, degraded=False).get("/readyz")
        assert resp.json()["checks"]["session"] == "ok"
        assert resp.status_code == 200

    def test_session_ok_when_flag_absent(self):
        """No checkpointer wanted (in-process default) → healthy, not degraded."""
        from apx_agent import Agent
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from apx_agent._readyz import mount_readyz
        import apx_agent._readyz as rz
        rz._run_canned_probe = lambda a, m: ProbeResult(assistant_text="hi", trace_id="tr-1")  # type: ignore
        app = FastAPI()  # no checkpointer_degraded on app.state
        mount_readyz(app, Agent(instructions="x", tools=[]))
        resp = TestClient(app).get("/readyz")
        assert resp.json()["checks"]["session"] == "ok"
        assert resp.status_code == 200


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


class TestReadyzData:
    """checks['data'] surfaces DataAgent discovery/wiring health. Informational —
    an ungrounded data agent is still a working generic SQL assistant, so it
    never gates readiness (unlike memory)."""

    def _app_for(self, agent):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import apx_agent._readyz as rz
        rz._run_canned_probe = lambda a, m: ProbeResult(assistant_text="hi", trace_id="tr-1")  # type: ignore
        app = FastAPI()
        from apx_agent._readyz import mount_readyz
        mount_readyz(app, agent)
        return TestClient(app)

    def test_data_ok_when_not_set(self):
        from apx_agent import Agent
        agent = Agent(instructions="x", tools=[])
        body = self._app_for(agent).get("/readyz").json()
        assert body["checks"]["data"] == "ok"

    def test_data_degraded_surfaced_but_still_ready(self):
        from apx_agent import DataAgent
        # Ungrounded DataAgent — degraded reason set, but readiness not gated.
        agent = DataAgent("main", "sales", tables={})
        agent._apx_data_degraded = "no schema grounding — running as a generic SQL assistant"
        resp = self._app_for(agent).get("/readyz")
        body = resp.json()
        assert "no schema grounding" in body["checks"]["data"]
        assert resp.status_code == 200
        assert body["status"] == "ready"


class TestReadyzSubAgents:
    """checks['sub_agents'] — declared-peer reachability (issue #445).

    Informational: present only when sub-agents are declared, and a down peer
    marks degraded:true in the detail WITHOUT flipping overall readiness — a
    down peer degrades delegation, it does not kill the agent.
    """

    def _client(self, agent, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setattr(
            readyz_mod, "_run_canned_probe",
            lambda a, m: ProbeResult(assistant_text="READY", trace_id="tr-1"),
        )
        return TestClient(_make_app(agent))

    def test_absent_when_no_sub_agents_declared(self, monkeypatch):
        agent = Agent(tools=[_trivial_tool])
        body = self._client(agent, monkeypatch).get("/readyz").json()
        assert "sub_agents" not in body["checks"]

    def test_down_peer_degrades_detail_but_stays_ready(self, monkeypatch):
        from apx_agent._doctor import SubAgentProbe

        agent = Agent(tools=[_trivial_tool], sub_agents=["https://peer.example.com"])
        monkeypatch.setattr(
            "apx_agent._doctor.probe_sub_agents",
            lambda urls: [
                SubAgentProbe(url=u, reachable=False, error="connection refused")
                for u in urls
            ],
        )
        resp = self._client(agent, monkeypatch).get("/readyz")
        body = resp.json()
        assert resp.status_code == 200  # a down peer NEVER flips ready on its own
        assert body["status"] == "ready"
        assert body["checks"]["sub_agents"]["degraded"] is True
        assert body["checks"]["sub_agents"]["agents"] == [
            {"url": "https://peer.example.com", "reachable": False,
             "error": "connection refused"},
        ]

    def test_reachable_peer_reports_card_name(self, monkeypatch):
        from apx_agent._doctor import SubAgentProbe

        agent = Agent(tools=[_trivial_tool], sub_agents=["https://peer.example.com"])
        monkeypatch.setattr(
            "apx_agent._doctor.probe_sub_agents",
            lambda urls: [
                SubAgentProbe(url=u, reachable=True, name="orders-agent")
                for u in urls
            ],
        )
        body = self._client(agent, monkeypatch).get("/readyz").json()
        assert body["checks"]["sub_agents"] == {
            "degraded": False,
            "agents": [{"url": "https://peer.example.com", "reachable": True,
                        "name": "orders-agent"}],
        }

    def test_probe_error_reports_degraded_never_breaks_payload(self, monkeypatch):
        def _boom(urls):
            raise RuntimeError("event loop exploded")

        agent = Agent(tools=[_trivial_tool], sub_agents=["https://peer.example.com"])
        monkeypatch.setattr("apx_agent._doctor.probe_sub_agents", _boom)
        resp = self._client(agent, monkeypatch).get("/readyz")
        body = resp.json()
        assert resp.status_code == 200
        assert body["status"] == "ready"
        assert body["checks"]["sub_agents"]["degraded"] is True
        assert "event loop exploded" in body["checks"]["sub_agents"]["error"]
