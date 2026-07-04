"""Reality check (ctk): a config-declared sub-agent is actually invoked (#436, #448).

Two REAL agents in one process, no mocked peer:

  * **Agent B** — a specialist with one deterministic tool that returns a
    sentinel string. Served by ``create_app`` (its real lifespan runs, so the
    real ``/invocations`` + card routes are mounted).
  * **Agent A** — an orchestrator whose *config* declares
    ``sub_agents=[B's URL]``. Nothing is wired in code.

``httpx.AsyncClient`` construction is patched so requests route into B's ASGI
app in-process (``httpx.ASGITransport`` — no sockets, no ports). A's LLM is a
scripted fake that emits the sub-agent tool call and then *relays* the tool
result, so the sentinel in A's final answer can only come from B's tool
actually executing:

    POST /invocations (A) → A's compiled graph → sub-agent delegate tool
      → RemoteDatabricksAgent → HTTP (ASGI) → B's served /invocations
        → B's compiled graph → B's tool body runs → sentinel

Before #436, A's card advertised the sub-agent but the compiled graph had no
such tool — the LLM could only hallucinate the delegation. These tests pin
both realities: the tool IS in the compiled tool set, and calling it REALLY
executes B.

Since #438, the round-trip travels the natural path: B's served
``/invocations`` accepts the Responses shape (``input``) that
``RemoteDatabricksAgent._call_via_http`` posts, and answers in the Responses
shape the client parses — so A's tool result is B's clean final answer, not a
serialized ChatAgent blob. The relay assertion below pins that.
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
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from apx_agent import AgentConfig, LlmAgent, create_app  # noqa: E402

SENTINEL = "XYLOPHONE-42"
B_URL = "http://agent-b.internal"

# Sentinel capture: proof B's tool BODY executed (not that some layer claimed it did).
B_TOOL_CALLS: list[str] = []
# What each fake model saw bound as tools — the compiled graph's real tool set.
BOUND_TOOLS: dict[str, list[str]] = {}


def secret_word() -> str:
    """Return the secret word for the caller."""
    B_TOOL_CALLS.append("ran")
    return SENTINEL


class _DelegatingModel(BaseChatModel):
    """Scripted-but-reactive fake LLM.

    First call (no ToolMessage in the conversation): emit a call to
    ``tool_name``. Second call: relay the last ToolMessage's content verbatim
    into the final answer. The relay is what makes the sentinel's presence in
    the output equivalent to "the tool really ran" — there is no scripted
    string containing the sentinel anywhere.
    """

    key: str
    tool_name: str
    tool_args: dict[str, Any]

    @property
    def _llm_type(self) -> str:
        return "delegating-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        BOUND_TOOLS[self.key] = sorted(t.name for t in tools)
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


def _install_models(monkeypatch: pytest.MonkeyPatch, models: dict[str, Any]) -> None:
    from apx_agent import _compile

    monkeypatch.setattr(
        _compile,
        "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: models[endpoint],
    )


def _patch_workspace_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _fake_ws()
    monkeypatch.setattr("apx_agent._wiring._make_workspace_client", lambda: ws)
    monkeypatch.setattr(
        "apx_agent._defaults._make_workspace_client", lambda **kw: ws
    )
    # fetch_remote_tools builds a raw WorkspaceClient for card-fetch auth
    # headers — keep it offline too.
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda *a, **kw: ws)


def _route_async_clients_to(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport
) -> None:
    """Default-construct httpx.AsyncClient onto *transport* (explicit wins)."""
    real = httpx.AsyncClient

    class _Routed(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("transport", transport)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Routed)


@pytest.fixture
def two_agents(monkeypatch: pytest.MonkeyPatch):
    """A REAL agent A (config sub_agents=[B]) talking to a REAL served agent B."""
    B_TOOL_CALLS.clear()
    BOUND_TOOLS.clear()
    _patch_workspace_clients(monkeypatch)
    _install_models(
        monkeypatch,
        {
            "model-a": _DelegatingModel(
                key="A",
                tool_name="agent_b",
                tool_args={"message": "What is the secret word?"},
            ),
            "model-b": _DelegatingModel(
                key="B", tool_name="secret_word", tool_args={}
            ),
        },
    )

    agent_b = LlmAgent(tools=[secret_word], name="agent-b")
    app_b = create_app(
        agent_b,
        config=AgentConfig(
            name="agent-b",
            description="Knows the secret word.",
            model="model-b",
        ),
    )
    with TestClient(app_b):  # real lifespan: mounts /invocations + card on app_b
        _route_async_clients_to(monkeypatch, httpx.ASGITransport(app=app_b))

        agent_a = LlmAgent(tools=[], name="agent-a")
        app_a = create_app(
            agent_a,
            config=AgentConfig(
                name="agent-a",
                model="model-a",
                sub_agents=[B_URL],  # the config path under test — nothing code-wired
            ),
        )
        with TestClient(app_a) as client_a:
            yield agent_a, client_a


def _final_texts(body: dict[str, Any]) -> str:
    return " ".join(m["content"] for m in body["messages"] if m.get("content"))


# ---------------------------------------------------------------------------
# The claim-vs-reality proof (#448): B's tool body ran, sentinel reached A.
# ---------------------------------------------------------------------------


def test_config_declared_sub_agent_really_executes(two_agents) -> None:
    _, client_a = two_agents

    resp = client_a.post(
        "/invocations",
        json={"messages": [{"role": "user", "content": "What is the secret word?"}]},
    )

    assert resp.status_code == 200, resp.text
    # REALITY: B's tool body executed — the delegation was not hallucinated.
    assert B_TOOL_CALLS == ["ran"], "agent B's tool never executed"
    # And the sentinel B produced traveled all the way into A's final answer.
    # (A's fake LLM only relays tool output — it cannot invent the sentinel.)
    text = _final_texts(resp.json())
    assert SENTINEL in text
    # #438: the reply crossed the wire on the natural path — A's tool result
    # is B's clean final answer, not a serialized ChatAgent JSON blob.
    assert f"[A] relayed: [B] relayed: {SENTINEL}" in text


# ---------------------------------------------------------------------------
# Regression pin for the #436 wiring: advertised == callable.
# ---------------------------------------------------------------------------


def test_sub_agent_tool_is_in_compiled_graph_tool_set(two_agents) -> None:
    agent_a, client_a = two_agents

    # The delegate landed in _tool_fns — the ONLY source _compile builds from.
    assert "agent_b" in [fn.__name__ for fn in agent_a._tool_fns]

    # The A2A card advertises the same tool it can actually call.
    card = client_a.get("/.well-known/agent.json").json()
    assert "agent_b" in {s["name"] for s in card["skills"]}

    # And after a real turn, the compiled graph bound that tool to the LLM.
    client_a.post(
        "/invocations",
        json={"messages": [{"role": "user", "content": "delegate please"}]},
    )
    assert "agent_b" in BOUND_TOOLS["A"]


# ---------------------------------------------------------------------------
# Degraded mode: unreachable sub-agent must not crash startup or the turn.
# ---------------------------------------------------------------------------


def test_unreachable_sub_agent_degrades_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    B_TOOL_CALLS.clear()
    BOUND_TOOLS.clear()
    _patch_workspace_clients(monkeypatch)

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _route_async_clients_to(monkeypatch, httpx.MockTransport(_refuse))

    # Card fetch fails → tool name degrades to the URL-derived fallback
    # ("agent-b.internal" → "agent"); keep the fake LLM aimed at that name.
    _install_models(
        monkeypatch,
        {
            "model-a": _DelegatingModel(
                key="A", tool_name="agent", tool_args={"message": "hello?"}
            )
        },
    )

    agent_a = LlmAgent(tools=[], name="agent-a")
    app_a = create_app(
        agent_a,
        config=AgentConfig(name="agent-a", model="model-a", sub_agents=[B_URL]),
    )
    # Startup must succeed even though the peer is down…
    with TestClient(app_a) as client_a:
        # …and the degraded tool must still exist (callable, not advertise-only).
        assert "agent" in [fn.__name__ for fn in agent_a._tool_fns]

        resp = client_a.post(
            "/invocations",
            json={"messages": [{"role": "user", "content": "try the sub-agent"}]},
        )

        assert resp.status_code == 200, resp.text
        text = _final_texts(resp.json())
        # Invoking it surfaces a clear error string instead of killing the turn.
        assert f"sub-agent at {B_URL} unreachable:" in text
