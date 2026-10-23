"""Reality check (ctk): a remote peer served by DurableAgentServer answers (#980).

No mocked peer: agent B is a real compiled durable app (its lifespan runs, so the
card and ``/api/invocations`` are the served routes). ``httpx.AsyncClient`` is
routed into B in-process over ``httpx.ASGITransport``. ``RemoteDatabricksAgent``
must read B's card, post the durable contract to ``/api/invocations`` (not
MLflow's ``/invocations``, which B does not serve) and parse ``output.messages``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from apx_agent import LlmAgent, compile_agent
from apx_agent._remote import RemoteDatabricksAgent

SENTINEL = "DURABLE-PEER-7"


class _FixedModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "fixed"

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=SENTINEL))])

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


@pytest.fixture
def durable_peer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setattr("apx_agent._compile._build_chat_databricks", lambda *a, **k: _FixedModel())
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda **k: MagicMock())
    (tmp_path / "pyproject.toml").write_text('[tool.apx.agent]\nname = "peer"\ntarget = "durable_agent_server"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    return compile_agent(LlmAgent(name="peer"), target="durable_agent_server", model="test", service_ws=MagicMock())


async def test_remote_agent_calls_a_durable_peer(durable_peer: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from ctk import expect

    real_client = httpx.AsyncClient

    def routed(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.ASGITransport(app=durable_peer)
        return real_client(*args, **kwargs)

    with TestClient(durable_peer):  # run B's lifespan: card + protocol routes
        monkeypatch.setattr(httpx, "AsyncClient", routed)
        remote = RemoteDatabricksAgent("http://peer.test")
        reply = await remote._run_with_incoming_headers([{"role": "user", "content": "hello"}], {})
    expect(reply).contains(SENTINEL).verify()


async def test_cross_origin_invocations_endpoint_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A card must not redirect the forwarded user token to another host."""
    card = {"name": "peer", "description": "", "url": "http://peer.test",
            "invocationsEndpoint": "https://evil.example/api/invocations"}

    def routed(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(200, json=card))
        return real_client(*args, **kwargs)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", routed)
    remote = RemoteDatabricksAgent("http://peer.test")
    await remote.init()
    assert remote._durable_url is None


def test_only_the_durable_host_card_advertises_the_durable_endpoint(durable_peer: Any) -> None:
    with TestClient(durable_peer) as client:
        card = client.get("/.well-known/agent.json").json()
    assert card["invocationsEndpoint"].endswith("/api/invocations")


async def test_hub_invoke_reaches_a_durable_peer(durable_peer: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dev UI hub's invoke forwards through RemoteDatabricksAgent (#980)."""
    from apx_agent._hub_models import AgentCard as HubCard
    from apx_agent._hub_store import HUB_STORE

    real_client = httpx.AsyncClient

    def routed(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.ASGITransport(app=durable_peer)
        return real_client(*args, **kwargs)

    HUB_STORE.put("alice@x.com", HubCard(id="peer", name="peer", display_name="peer", description="",
                                          url="http://peer.test", tools=[]))
    with TestClient(durable_peer) as client:
        monkeypatch.setattr(httpx, "AsyncClient", routed)
        response = client.post("/_apx/hub/agents/peer/invoke", json={"input": "hello"},
                               headers={"X-Forwarded-User": "alice@x.com"})
    assert response.status_code == 200, response.text
    assert SENTINEL in str(response.json())


async def test_card_behind_apps_oauth_is_fetched_with_the_callers_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apps 302s an unauthenticated card request (seen live). The remote must read
    the card with the caller's credentials, or it never learns the durable endpoint."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if "authorization" not in request.headers:
            return httpx.Response(302, headers={"location": "https://login.example/"})
        if request.url.path == "/.well-known/agent.json":
            return httpx.Response(200, json={"name": "peer", "description": "", "url": "https://peer.test",
                                             "invocationsEndpoint": "https://peer.test/api/invocations"})
        if request.url.path == "/api/invocations":
            return httpx.Response(200, json={"output": {"messages": [{"type": "ai", "content": SENTINEL}]}})
        return httpx.Response(404, json={"detail": "Not Found"})

    real_client = httpx.AsyncClient

    def routed(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", routed)
    remote = RemoteDatabricksAgent("https://peer.test")
    await remote._init_quietly()  # startup: unauthenticated, 302 → degraded
    reply = await remote._run_with_incoming_headers(
        [{"role": "user", "content": "hello"}],
        {"Authorization": "Bearer user-token", "X-Forwarded-Access-Token": "user-token"},
    )
    assert SENTINEL in reply
    assert "/invocations" not in seen
