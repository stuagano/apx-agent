"""Claim-vs-reality: identity attributes never cross the A2A boundary (#755).

Contract: Unity Catalog ABAC evaluates Databricks identity attributes from the
authenticated user identity — the caller's forwarded OBO credential. APX must
not copy, synthesize, or accept identity attributes as request headers, and
attribute values must never appear in trace or audit surfaces.

These tests prove the wire-hygiene half of that contract in-process:

  * ``_obo_headers`` forwards exactly the credential/host allowlist after the
    trusted-origin gate; attribute-shaped inbound headers are dropped.
  * ``_correlation_headers`` carries no identity-attribute keys or values.
  * An end-to-end A → B call whose inbound request carries attribute-shaped
    headers delivers only the OBO credential across the wire to B.

The live two-user outcome (allowed vs denied under a real identity-attribute
ABAC policy) is the opt-in sibling:
``test_identity_attribute_abac_live_reality_ctk.py``.
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
from apx_agent._remote import RemoteDatabricksAgent  # noqa: E402

PEER_CARD_URL = "https://peer-xyz.databricksapps.com/.well-known/agent.json"
B_URL = "http://agent-b.internal"

ORIGINAL_TOKEN = "obo-user-token-abc123"
REGION_MARKER = "attr-region-us-west"
COUNTRY_MARKER = "attr-country-us"

# Attribute-shaped inbound headers that must never be forwarded. The platform
# never sends attributes as headers; APX's allowlist must drop anything shaped
# like one regardless.
ATTRIBUTE_HEADERS = {
    "X-Databricks-Identity-Attribute-Region": REGION_MARKER,
    "X-Identity-Attribute-Country": COUNTRY_MARKER,
}

CREDENTIAL_ALLOWLIST = {"Authorization", "X-Forwarded-Access-Token", "X-Forwarded-Host"}

# (method, headers) pairs captured by the spy transport on the wire to agent B.
SENT_TO_B: list[tuple[str, dict[str, str]]] = []


def _assert_no_attribute_shape(headers: dict[str, str]) -> None:
    for key, value in headers.items():
        normalized = key.lower().replace("_", "-")
        assert "identity-attribute" not in normalized, f"attribute-shaped key leaked: {key}"
        assert value not in (REGION_MARKER, COUNTRY_MARKER), (
            f"attribute value leaked under key {key!r}"
        )


@pytest.mark.unit
def test_obo_headers_forward_exactly_the_credential_allowlist() -> None:
    agent = RemoteDatabricksAgent(PEER_CARD_URL)
    headers = agent._obo_headers(
        {
            "Authorization": f"Bearer {ORIGINAL_TOKEN}",
            "X-Forwarded-Access-Token": ORIGINAL_TOKEN,
            "X-Forwarded-Host": "peer-xyz.databricksapps.com",
            "Cookie": "session=secret",
            **ATTRIBUTE_HEADERS,
        }
    )
    assert set(headers) <= CREDENTIAL_ALLOWLIST, (
        f"unexpected headers forwarded: {sorted(set(headers) - CREDENTIAL_ALLOWLIST)}"
    )
    assert headers["Authorization"] == f"Bearer {ORIGINAL_TOKEN}"
    assert headers["X-Forwarded-Access-Token"] == ORIGINAL_TOKEN
    _assert_no_attribute_shape(headers)


@pytest.mark.unit
def test_obo_headers_withhold_credentials_and_attributes_from_untrusted_origin() -> None:
    agent = RemoteDatabricksAgent(PEER_CARD_URL)
    agent._base_url = "https://evil.example.com"
    headers = agent._obo_headers(
        {
            "Authorization": f"Bearer {ORIGINAL_TOKEN}",
            "X-Forwarded-Access-Token": ORIGINAL_TOKEN,
            "X-Forwarded-Host": "peer-xyz.databricksapps.com",
            **ATTRIBUTE_HEADERS,
        }
    )
    assert "Authorization" not in headers
    assert "X-Forwarded-Access-Token" not in headers
    _assert_no_attribute_shape(headers)


@pytest.mark.unit
def test_correlation_headers_carry_no_identity_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABRICKS_APP_NAME", "caller-app")
    agent = RemoteDatabricksAgent(PEER_CARD_URL)
    headers = agent._correlation_headers()
    for key, value in headers.items():
        assert "identity" not in key.lower(), f"identity-shaped key leaked: {key}"
        assert "attribute" not in key.lower(), f"attribute-shaped key leaked: {key}"
        assert value not in (REGION_MARKER, COUNTRY_MARKER), (
            f"attribute value leaked under key {key!r}"
        )


def _capture(query: str, headers: Dependencies.Headers) -> str:
    if headers is not None and headers.token is not None:
        return f"captured: {headers.token.get_secret_value()}"
    return "captured: NONE"


class _DelegatingModel(BaseChatModel):
    """First call: emit a tool call. Second call (after ToolMessage): relay."""

    key: str
    tool_name: str
    tool_args: dict[str, Any] = {}

    @property
    def _llm_type(self) -> str:
        return "delegating-identity-attribute"

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


class _SpyTransport(httpx.AsyncBaseTransport):
    """Route to B's ASGI app while recording the raw wire headers."""

    def __init__(self, app: Any) -> None:
        self._inner = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        SENT_TO_B.append((request.method, dict(request.headers)))
        return await self._inner.handle_async_request(request)


@pytest.mark.unit
def test_attribute_shaped_headers_never_cross_a_to_b_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller → A → B: only the OBO credential crosses the boundary to B."""
    SENT_TO_B.clear()
    ws = MagicMock(name="fake_ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    ws.config.authenticate.return_value = {}
    monkeypatch.setattr("apx_agent._wiring._make_workspace_client", lambda: ws)
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda **kw: ws)
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda *a, **kw: ws)
    monkeypatch.setattr(
        _compile,
        "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: {
            "model-a": _DelegatingModel(
                key="A", tool_name="agent_b", tool_args={"query": "who am I?"}
            ),
            "model-b": _DelegatingModel(key="B", tool_name="unused", tool_args={}),
        }[endpoint],
    )

    # Build B first; A's lifespan fetches B's card at startup, so the routed
    # transport must point at B before A is created.
    agent_b = LlmAgent(tools=[_capture], name="agent-b")
    app_b = create_app(
        agent_b,
        config=AgentConfig(name="agent-b", description="Leaf.", model="model-b"),
    )

    real_client = httpx.AsyncClient
    spy = _SpyTransport(app_b)

    class _Routed(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("transport", spy)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Routed)

    agent_a = LlmAgent(tools=[], name="agent-a")
    app_a = create_app(
        agent_a,
        config=AgentConfig(name="agent-a", model="model-a", sub_agents=[B_URL]),
    )

    # Same leaf-first lifespan shape as the multi-hop sibling: fire B so
    # include_router mounts stick after its blocking portal exits, then keep
    # only A live.
    with TestClient(app_b):
        pass
    with TestClient(app_a) as client_a:
        resp = client_a.post(
            "/invocations",
            json={"messages": [{"role": "user", "content": "Who am I?"}]},
            headers={
                "X-Forwarded-Access-Token": ORIGINAL_TOKEN,
                "Authorization": f"Bearer {ORIGINAL_TOKEN}",
                "X-Forwarded-Host": "peer-xyz.databricksapps.com",
                **ATTRIBUTE_HEADERS,
            },
        )

    assert resp.status_code == 200, resp.text

    posts = [headers for method, headers in SENT_TO_B if method == "POST"]
    assert posts, "A never called B — delegation chain broken"
    for wire_headers in posts:
        lowered = {k.lower(): v for k, v in wire_headers.items()}
        assert "x-databricks-identity-attribute-region" not in lowered
        assert "x-identity-attribute-country" not in lowered
        assert REGION_MARKER not in wire_headers.values()
        assert COUNTRY_MARKER not in wire_headers.values()

    # At least one wire call carried the OBO credential itself: the contract is
    # "attributes come from the authenticated user", not "no identity at all".
    assert any(
        ORIGINAL_TOKEN in h.values() or f"Bearer {ORIGINAL_TOKEN}" in h.values()
        for h in posts
    ), "OBO credential did not reach B — user-scoped access would be impossible"
