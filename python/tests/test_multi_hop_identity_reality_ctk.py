"""Claim-vs-reality: OBO identity passthrough survives multi-hop delegation.

Three REAL agents in one process:

  * **Agent A** — orchestrator, declares ``sub_agents=[B_URL]``
  * **Agent B** — middle tier, declares ``sub_agents=[C_URL]``
  * **Agent C** — leaf specialist, has a tool that captures the OBO headers

The caller sends a request to A with a bearer token. A delegates to B, B
delegates to C. C's tool captures whatever ``X-Forwarded-Access-Token`` it
received. The test asserts the original caller's token arrives at C — proving
identity passthrough survives 2 hops across the A2A boundary.

Uses ``httpx.ASGITransport`` to route HTTP calls in-process (no real sockets).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("mlflow")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    BaseMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from apx_agent import AgentConfig, LlmAgent, create_app  # noqa: E402
from apx_agent import _compile  # noqa: E402
from apx_agent._defaults import Dependencies  # noqa: E402

B_URL = "http://agent-b.internal"
C_URL = "http://agent-c.internal"
ORIGINAL_TOKEN = "obo-user-token-abc123"

CAPTURED_HEADERS: list[dict[str, str]] = []


def capture_identity(query: str, headers: Dependencies.Headers) -> str:
    """Capture the OBO headers received by the leaf agent's tool."""
    captured: dict[str, str] = {}
    if headers is not None:
        if headers.token is not None:
            captured["token"] = headers.token.get_secret_value()
        if headers.host is not None:
            captured["host"] = headers.host
    CAPTURED_HEADERS.append(captured)
    return f"captured: {captured.get('token', 'NONE')}"


class _DelegatingModel(BaseChatModel):
    """First call: emit a tool call. Second call (after ToolMessage): relay."""

    key: str
    tool_name: str
    tool_args: dict[str, Any] = {}

    @property
    def _llm_type(self) -> str:
        return "delegating-identity"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        last_tool = next(
            (m for m in reversed(messages) if isinstance(m, ToolMessage)), None
        )
        if last_tool is None:
            msg = AIMessage(
                content="",
                tool_calls=[
                    {"name": self.tool_name, "args": dict(self.tool_args), "id": "t1"}
                ],
            )
        else:
            msg = AIMessage(content=f"[{self.key}] relayed: {last_tool.content}")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _fake_ws() -> MagicMock:
    ws = MagicMock(name="fake_ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    ws.config.authenticate.return_value = {}
    return ws


def _patch_infra(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _fake_ws()
    monkeypatch.setattr("apx_agent._wiring._make_workspace_client", lambda: ws)
    monkeypatch.setattr(
        "apx_agent._defaults._make_workspace_client", lambda **kw: ws
    )
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda *a, **kw: ws)


def _install_models(monkeypatch: pytest.MonkeyPatch, models: dict[str, Any]) -> None:
    monkeypatch.setattr(
        _compile,
        "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: models[endpoint],
    )


def _final_text(body: dict[str, Any]) -> str:
    return " ".join(m["content"] for m in body["messages"] if m.get("content"))


class _RoutingTransport(httpx.AsyncBaseTransport):
    """Route requests to the correct ASGI app based on the URL host."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self._transports = {
            host: httpx.ASGITransport(app=app)
            for host, app in routes.items()
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        transport = self._transports.get(host)
        if transport is None:
            raise httpx.ConnectError(
                f"No route for {host}", request=request
            )
        return await transport.handle_async_request(request)


def _route_async_clients_to(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport
) -> None:
    """Monkeypatch httpx.AsyncClient so new instances use *transport*."""
    real_client = httpx.AsyncClient

    class _Routed(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("transport", transport)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Routed)


@pytest.mark.unit
def test_obo_token_reaches_leaf_through_two_hops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A→B→C: the original caller's OBO token reaches C's tool body."""
    CAPTURED_HEADERS.clear()
    _patch_infra(monkeypatch)
    _install_models(monkeypatch, {
        "model-a": _DelegatingModel(
            key="A", tool_name="agent_b", tool_args={"query": "call C"}
        ),
        "model-b": _DelegatingModel(
            key="B", tool_name="agent_c", tool_args={"query": "capture it"}
        ),
        "model-c": _DelegatingModel(
            key="C", tool_name="capture_identity", tool_args={"query": "who am I?"}
        ),
    })

    # Build from leaf inward: C first (no sub-agents), then B (sub_agents=[C]),
    # then A (sub_agents=[B]). Each app's lifespan fetches its sub-agent cards
    # at startup, so the transport must route to the already-created downstream
    # apps BEFORE the upstream app is created.

    # Step 1: Create C (no dependencies, no transport needed yet)
    agent_c = LlmAgent(tools=[capture_identity], name="agent-c")
    app_c = create_app(
        agent_c,
        config=AgentConfig(name="agent-c", description="Leaf.", model="model-c"),
    )

    # Step 2: Install transport routing to C, then create B
    _route_async_clients_to(monkeypatch, httpx.ASGITransport(app=app_c))
    agent_b = LlmAgent(tools=[], name="agent-b")
    app_b = create_app(
        agent_b,
        config=AgentConfig(
            name="agent-b", description="Middle.", model="model-b", sub_agents=[C_URL]
        ),
    )

    # Step 3: Install transport routing to BOTH B and C, then create A
    routing = _RoutingTransport({
        "agent-b.internal": app_b,
        "agent-c.internal": app_c,
    })
    _route_async_clients_to(monkeypatch, routing)
    agent_a = LlmAgent(tools=[], name="agent-a")
    app_a = create_app(
        agent_a,
        config=AgentConfig(name="agent-a", model="model-a", sub_agents=[B_URL]),
    )

    # Step 4: Run the full chain — lifespans already fired during create_app
    with TestClient(app_c), TestClient(app_b), TestClient(app_a) as client_a:
        resp = client_a.post(
            "/invocations",
            json={"messages": [{"role": "user", "content": "Who am I?"}]},
            headers={
                "X-Forwarded-Access-Token": ORIGINAL_TOKEN,
                "Authorization": f"Bearer {ORIGINAL_TOKEN}",
            },
        )

    assert resp.status_code == 200, resp.text
    assert CAPTURED_HEADERS, "C's tool never executed — delegation chain broken"
    assert CAPTURED_HEADERS[0].get("token") == ORIGINAL_TOKEN, (
        f"OBO token did not reach the leaf agent. Got: {CAPTURED_HEADERS[0]}"
    )
