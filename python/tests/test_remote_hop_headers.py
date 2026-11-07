"""The remote-hop OBO header rule lives in one place (#1050).

``remote_hop_headers`` is the per-hop user-OBO trust rule from
docs/design/a2a-trust-model.md. These tests pin its header set and prove each
remote-hop call site routes through it: the helper is swapped for a spy whose
output carries a marker, and the marker must reach the peer.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import SecretStr

from apx_agent._obo import remote_hop_headers

TOKEN = "opaque-hop-token"


def test_token_forwards_both_credential_headers_and_host() -> None:
    assert remote_hop_headers(TOKEN, "ws.example.com") == {
        "X-Forwarded-Access-Token": TOKEN,
        "Authorization": f"Bearer {TOKEN}",
        "X-Forwarded-Host": "ws.example.com",
    }


@pytest.mark.parametrize("token", [None, ""])
def test_no_token_forwards_no_credentials(token: str | None) -> None:
    assert remote_hop_headers(token) == {}
    assert remote_hop_headers(token, "ws.example.com") == {"X-Forwarded-Host": "ws.example.com"}


def test_never_logs_the_token(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        remote_hop_headers(TOKEN, "ws.example.com")
    assert TOKEN not in caplog.text


def _spy(monkeypatch: pytest.MonkeyPatch, module: str) -> list[tuple[Any, Any]]:
    """Swap ``module.remote_hop_headers`` for a marker-adding spy."""
    calls: list[tuple[Any, Any]] = []

    def spy(token: str | None, host: str | None = None) -> dict[str, str]:
        calls.append((token, host))
        return {**remote_hop_headers(token, host), "X-Hop-Rule": "remote_hop_headers"}

    monkeypatch.setattr(f"{module}.remote_hop_headers", spy)
    return calls


@pytest.mark.asyncio
async def test_remote_agent_tool_forwards_via_the_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent._agent_tool import remote_agent_tool
    from apx_agent._defaults import DatabricksAppsHeaders
    from apx_agent._remote import RemoteDatabricksAgent

    calls = _spy(monkeypatch, "apx_agent._agent_tool")
    sent: list[dict[str, str]] = []

    async def capture(_self: Any, _messages: Any, forwarded: dict[str, str]) -> str:
        sent.append(dict(forwarded))
        return "peer ok"

    monkeypatch.setattr(RemoteDatabricksAgent, "_run_with_incoming_headers", capture)
    tool = remote_agent_tool("https://peer.example.databricksapps.com", name="peer", description="Peer.")
    headers = DatabricksAppsHeaders(
        host="ws.example.com", user_name=None, user_id=None, user_email=None,
        request_id=None, token=SecretStr(TOKEN),
    )

    assert await tool("hi", headers) == "peer ok"
    assert calls == [(TOKEN, "ws.example.com")]
    assert sent == [{**remote_hop_headers(TOKEN, "ws.example.com"), "X-Hop-Rule": "remote_hop_headers"}]


def test_hub_invoke_forwards_via_the_rule(dev_ui_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.testclient import TestClient

    from apx_agent._hub_models import AgentCard as HubCard
    from apx_agent._hub_store import HUB_STORE
    from apx_agent._remote import RemoteDatabricksAgent

    calls = _spy(monkeypatch, "apx_agent._obo")
    sent: list[dict[str, str]] = []

    async def no_init(_self: Any) -> None:
        return None

    async def capture(_self: Any, _messages: Any, forwarded: dict[str, str]) -> dict[str, str]:
        sent.append(dict(forwarded))
        return {"output": "peer ok"}

    monkeypatch.setattr(RemoteDatabricksAgent, "init", no_init)
    monkeypatch.setattr(RemoteDatabricksAgent, "_post_via_http", capture)
    HUB_STORE.put("alice@x.com", HubCard(id="peer-1050", name="peer", display_name="peer",
                                          description="", url="https://peer.test", tools=[]))

    try:
        response = TestClient(dev_ui_app).post(
            "/_apx/hub/agents/peer-1050/invoke",
            json={"input": "hi"},
            headers={"X-Forwarded-User": "alice@x.com", "X-Forwarded-Access-Token": TOKEN,
                     "X-Forwarded-Host": "ws.example.com"},
        )
    finally:
        HUB_STORE.delete("alice@x.com", "peer-1050")

    assert response.status_code == 200, response.text
    assert calls == [(TOKEN, "ws.example.com")]
    assert sent == [{**remote_hop_headers(TOKEN, "ws.example.com"), "X-Hop-Rule": "remote_hop_headers"}]


def test_bound_remote_leaf_forwards_via_the_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("langgraph")
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.runnables import RunnableLambda

    from apx_agent import Agent, RemoteDatabricksAgent, SequentialAgent, compile_to_langgraph
    from apx_agent import _compile as compile_module
    from apx_agent._defaults import DatabricksAppsHeaders
    from apx_agent._remote import _RemoteLeafBinding, _RemoteReply

    calls = _spy(monkeypatch, "apx_agent._compile")
    sent: list[dict[str, str]] = []

    async def capture(_self: Any, _messages: Any, forwarded: dict[str, str]) -> Any:
        sent.append(dict(forwarded))
        return _RemoteReply(text="approved", control=None)

    monkeypatch.setattr(RemoteDatabricksAgent, "run_with_control", capture)
    monkeypatch.setattr(compile_module, "_compile_llm_agent",
                        lambda *_a, **_k: RunnableLambda(lambda _s: {"messages": [AIMessage(content="local")]}))
    root = SequentialAgent([Agent(name="pricing")], name="sequence")
    root._apx_remote_leaf_bindings = {
        "pricing": _RemoteLeafBinding(logical_name="pricing",
                                      card_url="https://pricing.example.com/.well-known/agent.json"),
    }
    headers = DatabricksAppsHeaders(
        host="ws.example.com", user_name=None, user_id=None, user_email=None,
        request_id=None, token=SecretStr(TOKEN),
    )

    graph = compile_to_langgraph(root, ws=None, model="test-model", headers=headers)
    graph.invoke({"messages": [HumanMessage(content="price")]})

    assert calls == [(TOKEN, "ws.example.com")]
    assert sent == [{**remote_hop_headers(TOKEN, "ws.example.com"), "X-Hop-Rule": "remote_hop_headers"}]
