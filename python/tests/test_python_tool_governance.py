"""Python-native factories retain the host policy and inherited caller ceiling."""

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from apx_agent import AgentConfig, LlmAgent, mcp_tool, mcp_toolkit, openapi_tool, setup_agent
from apx_agent._agent_identity import apply_agent_identity
from apx_agent._tool_config import ToolConfigError, load_config_tools
from apx_agent._tool_scope import ToolScope, attach_scope, get_scope


@pytest.mark.parametrize("factory", ["openapi", "mcp_tool", "mcp_toolkit"])
def test_direct_factory_denies_host_before_network(monkeypatch, factory):
    monkeypatch.setenv("APX_TOOLS_ALLOWED_HOSTS", "trusted.example.com")
    fetch = MagicMock(return_value=httpx.Response(
        200, json={"paths": {}}, request=httpx.Request("GET", "https://blocked.example.com")
    ))
    session = MagicMock()
    monkeypatch.setattr("httpx.get", fetch)
    monkeypatch.setattr("apx_agent.mcp_consume._open_session", session)

    with pytest.raises(ToolConfigError, match="not in APX_TOOLS_ALLOWED_HOSTS"):
        if factory == "openapi":
            openapi_tool("https://blocked.example.com/spec.json", "connection")
        elif factory == "mcp_tool":
            mcp_tool("https://blocked.example.com/mcp", "search")
        else:
            mcp_toolkit("https://blocked.example.com/mcp")

    fetch.assert_not_called()
    session.assert_not_called()


def test_mcp_explicit_description_cannot_bypass_host_policy(monkeypatch):
    monkeypatch.setenv("APX_TOOLS_ALLOWED_HOSTS", "trusted.example.com")
    with pytest.raises(ToolConfigError, match="not in APX_TOOLS_ALLOWED_HOSTS"):
        mcp_tool("https://blocked.example.com/mcp", "search", description="Search.")


@pytest.mark.parametrize("allowed_hosts", [None, " other.example.com, trusted.example.com "])
@pytest.mark.parametrize("factory", ["openapi", "mcp_tool", "mcp_toolkit"])
def test_direct_factory_allowed_or_unset_policy_keeps_normal_path(monkeypatch, allowed_hosts, factory):
    if allowed_hosts is None:
        monkeypatch.delenv("APX_TOOLS_ALLOWED_HOSTS", raising=False)
    else:
        monkeypatch.setenv("APX_TOOLS_ALLOWED_HOSTS", allowed_hosts)

    if factory == "openapi":
        url = "https://trusted.example.com/spec.json"
        fetch = MagicMock(return_value=httpx.Response(
            200,
            json={"paths": {"/search": {"get": {"operationId": "search"}}}},
            request=httpx.Request("GET", url),
        ))
        monkeypatch.setattr("httpx.get", fetch)
        tools = openapi_tool(url, "connection")
        fetch.assert_called_once_with(url, timeout=10.0, follow_redirects=True)
    else:
        session = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(
            tools=[SimpleNamespace(name="search", description="Search.")]
        )))
        opened = MagicMock()

        @asynccontextmanager
        async def open_session(server_url, headers, transport):
            opened(server_url, headers, transport)
            yield session

        monkeypatch.setattr("apx_agent.mcp_consume._open_session", open_session)
        url = "https://trusted.example.com/mcp"
        tools = [mcp_tool(url, "search")] if factory == "mcp_tool" else mcp_toolkit(url)
        opened.assert_called_once_with(url, None, "http")
        session.list_tools.assert_awaited_once()

    assert [tool.__name__ for tool in tools] == ["search"]


def test_config_loader_retains_indexed_host_denial(monkeypatch):
    monkeypatch.setenv("APX_TOOLS_ALLOWED_HOSTS", "trusted.example.com")
    with pytest.raises(ToolConfigError, match=r"tool #0 .*not in APX_TOOLS_ALLOWED_HOSTS"):
        load_config_tools([{"type": "mcp_toolkit", "server_url": "https://blocked.example.com/mcp"}])


def test_identity_inheritance_preserves_entire_scope():
    def restricted() -> str:
        return "ok"

    scope = ToolScope(
        catalogs=("main",),
        schemas=("main.finance",),
        tables=("main.finance.ledger",),
        functions=("main.finance.lookup",),
        secret_scopes=("finance",),
        callers=("alice@example.com",),
    )
    attach_scope(restricted, scope)
    apply_agent_identity(SimpleNamespace(_tool_fns=[restricted]), "sp")
    assert get_scope(restricted) == replace(scope, identity="sp")
    assert scope.identity is None


@pytest.mark.asyncio
async def test_inherited_identity_still_denies_unauthorized_rest_calls():
    calls = []

    def restricted(amount: int) -> str:
        """Record an authorized operation."""
        calls.append(amount)
        return f"recorded {amount}"

    attach_scope(restricted, ToolScope(callers=("alice@example.com",)))
    app = FastAPI()
    await setup_agent(
        app,
        LlmAgent(tools=[restricted]),
        AgentConfig(name="inherited-callers", api_prefix="/api", identity="sp"),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.post(
            "/api/tools/restricted", json={"amount": 1},
            headers={"X-Forwarded-Email": "bob@example.com"},
        )
        anonymous = await client.post("/api/tools/restricted", json={"amount": 2})
        allowed = await client.post(
            "/api/tools/restricted", json={"amount": 3},
            headers={"X-Forwarded-Email": "alice@example.com"},
        )

    assert denied.status_code == 403
    assert "caller_denied" in denied.text
    assert anonymous.status_code == 403
    assert allowed.status_code == 200
    assert "recorded 3" in allowed.text
    assert calls == [3]
