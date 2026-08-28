from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apx_agent import AgentConfig, Dependencies, LlmAgent
from apx_agent._appkit_tool_bridge import build_appkit_tool_bridge_router
from apx_agent._models import AgentCard, AgentContext


def _app(agent: LlmAgent, monkeypatch) -> FastAPI:
    app = FastAPI()
    app.state.agent_context = AgentContext(
        config=AgentConfig(name="bridge-agent", model="databricks-claude-sonnet-4-5"),
        tools=[],
        card=AgentCard(name="bridge-agent", description="Bridge test agent"),
        agent=agent,
    )
    ws = MagicMock(name="obo_ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    monkeypatch.setattr("apx_agent._appkit_tool_bridge._obo_ws_from_headers", lambda _: ws)
    app.include_router(build_appkit_tool_bridge_router())
    return app


def test_bridge_executes_tool_with_dependencies_and_hooks(monkeypatch) -> None:
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
    client = TestClient(_app(agent, monkeypatch))

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
