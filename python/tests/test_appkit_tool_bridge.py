from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apx_agent import AgentConfig, Dependencies, LlmAgent
from apx_agent._appkit_tool_bridge import build_appkit_tool_bridge_router
from apx_agent._models import AgentCard, AgentContext
from apx_agent._policy import ApprovalRequired, ApprovalStore


def _app(agent: LlmAgent, monkeypatch) -> FastAPI:
    from apx_agent import _appkit_tool_bridge

    app = FastAPI()
    app.state.agent_context = AgentContext(
        config=AgentConfig(name="bridge-agent", model="databricks-claude-sonnet-4-5"),
        tools=[],
        card=AgentCard(name="bridge-agent", description="Bridge test agent"),
        agent=agent,
    )
    ws = MagicMock(name="obo_ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    monkeypatch.setattr(_appkit_tool_bridge, "_obo_ws_from_headers", lambda _: ws)
    monkeypatch.setattr(_appkit_tool_bridge, "_make_workspace_client", MagicMock)
    app.include_router(build_appkit_tool_bridge_router())
    return app


def test_bridge_executes_tool_with_dependencies_and_hooks(monkeypatch) -> None:
    from apx_agent import _appkit_tool_bridge

    seen: list[tuple] = []

    def lookup(
        resource: str,
        ws: Dependencies.Workspace,
        headers: Dependencies.Headers,
    ) -> dict[str, str | bool | None]:
        """Look up policy."""
        return {
            "resource": resource,
            "host": ws.config.host,
            "user": headers.user_id,
        }

    agent = LlmAgent(
        tools=[lookup],
        before_tool=lambda name, args: seen.append(("before", name, args)),
        after_tool=lambda name, args, output: seen.append(("after", name, args, output)),
    )
    app = _app(agent, monkeypatch)
    service_factory = MagicMock(
        side_effect=AssertionError("user tool constructed service credentials")
    )
    monkeypatch.setattr(_appkit_tool_bridge, "_make_workspace_client", service_factory)
    client = TestClient(app)

    response = client.post(
        "/_apx/internal/appkit/tools/lookup",
        json={"args": {"resource": "main.sales.orders"}},
        headers={
            "X-Forwarded-User": "alice",
            "X-Forwarded-Access-Token": "token",
        },
    )

    assert response.status_code == 200
    assert response.json()["result"] == {
        "resource": "main.sales.orders",
        "host": "https://fake.cloud.databricks.com",
        "user": "alice",
    }
    assert seen[0] == ("before", "lookup", {"resource": "main.sales.orders"})
    assert seen[1][0:3] == ("after", "lookup", {"resource": "main.sales.orders"})
    service_factory.assert_not_called()


def test_bridge_uses_ambient_client_for_tokenless_service_tool(monkeypatch) -> None:
    from apx_agent import _appkit_tool_bridge

    service_ws = MagicMock(name="service_ws")
    service_ws.config.host = "https://service.cloud.databricks.com"
    obo = MagicMock(
        side_effect=AssertionError("service tool requested OBO credentials")
    )

    def lookup(ws: Dependencies.Client) -> str:
        """Use service credentials."""
        return ws.config.host

    app = _app(LlmAgent(tools=[lookup]), monkeypatch)
    monkeypatch.setattr(
        _appkit_tool_bridge, "_make_workspace_client", lambda: service_ws
    )
    monkeypatch.setattr(_appkit_tool_bridge, "_obo_ws_from_headers", obo)
    response = TestClient(app).post(
        "/_apx/internal/appkit/tools/lookup",
        json={"args": {}},
    )

    assert response.status_code == 200
    assert response.json() == {"result": "https://service.cloud.databricks.com"}
    obo.assert_not_called()


def test_bridge_runs_pure_tool_without_forwarded_token(monkeypatch) -> None:
    def ping() -> str:
        """Return pong."""
        return "pong"

    response = TestClient(_app(LlmAgent(tools=[ping]), monkeypatch)).post(
        "/_apx/internal/appkit/tools/ping",
        json={"args": {}},
    )

    assert response.status_code == 200
    assert response.json() == {"result": "pong"}


def test_bridge_service_metadata_excludes_forwarded_bearer_token(monkeypatch) -> None:
    from apx_agent import _appkit_tool_bridge

    service_ws = MagicMock(name="service_ws")
    obo = MagicMock(
        side_effect=AssertionError("service tool requested OBO credentials")
    )

    def lookup(
        ws: Dependencies.Client,
        headers: Dependencies.Headers,
    ) -> dict[str, str | bool | None]:
        """Use service credentials with request metadata."""
        return {
            "service": ws is service_ws,
            "user": headers.user_id,
            "has_token": headers.token is not None,
        }

    app = _app(LlmAgent(tools=[lookup]), monkeypatch)
    monkeypatch.setattr(
        _appkit_tool_bridge, "_make_workspace_client", lambda: service_ws
    )
    monkeypatch.setattr(_appkit_tool_bridge, "_obo_ws_from_headers", obo)
    response = TestClient(app).post(
        "/_apx/internal/appkit/tools/lookup",
        json={"args": {}},
        headers={
            "X-Forwarded-User": "alice",
            "X-Forwarded-Access-Token": "must-not-be-read",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "result": {"service": True, "user": "alice", "has_token": False}
    }
    obo.assert_not_called()


def test_bridge_rejects_user_tool_without_forwarded_identity(monkeypatch) -> None:
    def lookup(ws: Dependencies.UserClient) -> str:
        """Use user credentials."""
        return ws.config.host

    response = TestClient(_app(LlmAgent(tools=[lookup]), monkeypatch)).post(
        "/_apx/internal/appkit/tools/lookup",
        json={"args": {}},
    )

    assert response.status_code == 401
    assert response.json() == {
        "detail": "APX tool 'lookup' requires forwarded user identity"
    }


def test_bridge_returns_404_for_unknown_tool(monkeypatch) -> None:
    client = TestClient(_app(LlmAgent(tools=[]), monkeypatch))

    response = client.post(
        "/_apx/internal/appkit/tools/missing",
        json={"args": {}},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown APX tool: missing"


def test_bridge_rejects_stateful_tools(monkeypatch) -> None:
    def remember(value: str, state: Dependencies.State) -> str:
        """Remember a value."""
        state["value"] = value
        return value

    client = TestClient(_app(LlmAgent(tools=[remember]), monkeypatch))

    response = client.post(
        "/_apx/internal/appkit/tools/remember",
        json={"args": {"value": "x"}},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "APX AppKit bridge cannot execute stateful tool: remember"


def test_bridge_returns_403_when_before_tool_denies(monkeypatch) -> None:
    called = False

    def mutate(value: str) -> str:
        nonlocal called
        called = True
        return value

    agent = LlmAgent(
        tools=[mutate],
        before_tool=lambda _name, _args: (_ for _ in ()).throw(PermissionError("blocked")),
    )
    response = TestClient(_app(agent, monkeypatch)).post(
        "/_apx/internal/appkit/tools/mutate",
        json={"args": {"value": "x"}},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "blocked"}
    assert called is False


def test_bridge_returns_bounded_403_when_before_tool_requires_approval(monkeypatch) -> None:
    called = False
    approval = ApprovalStore().request("mutate", {"value": "x"}, reason="manual confirmation")

    def mutate(value: str) -> str:
        nonlocal called
        called = True
        return value

    agent = LlmAgent(
        tools=[mutate],
        before_tool=lambda _name, _args: (_ for _ in ()).throw(ApprovalRequired(approval)),
    )
    response = TestClient(_app(agent, monkeypatch), raise_server_exceptions=False).post(
        "/_apx/internal/appkit/tools/mutate",
        json={"args": {"value": "x"}},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Tool execution is denied"}
    assert approval.id not in response.text
    assert "approval" not in response.text.lower()
    assert "retry" not in response.text.lower()
    assert called is False
