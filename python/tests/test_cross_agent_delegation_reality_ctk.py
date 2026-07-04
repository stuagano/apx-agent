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
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from apx_agent import AgentConfig, LlmAgent, create_app  # noqa: E402

SENTINEL = "XYLOPHONE-42"
B_URL = "http://agent-b.internal"

# Sentinel capture: proof B's tool BODY executed (not that some layer claimed it did).
B_TOOL_CALLS: list[str] = []
# What each fake model saw bound as tools — the compiled graph's real tool set.
BOUND_TOOLS: dict[str, list[str]] = {}
# The last user-message content each fake model saw — for B, this is exactly
# what crossed the wire from A's delegate (#442).
SEEN_USER_CONTENT: dict[str, Any] = {}


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
        last_human = next(
            (m for m in reversed(messages) if isinstance(m, HumanMessage)), None
        )
        if last_human is not None:
            SEEN_USER_CONTENT[self.key] = last_human.content
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
    _fast_retries(monkeypatch)

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


# ---------------------------------------------------------------------------
# #440: card fetch retries at startup and repairs lazily on first use.
# ---------------------------------------------------------------------------


def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the startup retry backoff so tests stay fast (real: 0.5s * n)."""
    monkeypatch.setattr("apx_agent._agents._CARD_FETCH_BACKOFF_S", 0.01)


def test_startup_retry_recovers_from_boot_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Card fetch fails twice (peer still booting) then succeeds → the LIVE
    descriptor is registered, not the URL-derived stub the issue showed
    ('127.0.0.1:8799')."""
    B_TOOL_CALLS.clear()
    BOUND_TOOLS.clear()
    _patch_workspace_clients(monkeypatch)
    _fast_retries(monkeypatch)

    card_attempts = {"n": 0}

    def _flaky(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/agent.json":
            card_attempts["n"] += 1
            if card_attempts["n"] < 3:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(
                200, json={"name": "agent-b", "description": "Knows the secret word."}
            )
        raise httpx.ConnectError("connection refused", request=request)

    _route_async_clients_to(monkeypatch, httpx.MockTransport(_flaky))
    _install_models(
        monkeypatch,
        {"model-a": _DelegatingModel(key="A", tool_name="agent_b", tool_args={"message": "x"})},
    )

    agent_a = LlmAgent(tools=[], name="agent-a")
    app_a = create_app(
        agent_a,
        config=AgentConfig(name="agent-a", model="model-a", sub_agents=[B_URL]),
    )
    with TestClient(app_a) as client_a:
        assert card_attempts["n"] == 3, "startup should have retried the card fetch"
        # The card-derived (not URL-derived) tool won, and nothing is degraded.
        assert "agent_b" in [fn.__name__ for fn in agent_a._tool_fns]
        assert agent_a._degraded_sub_agents == {}

        card = client_a.get("/.well-known/agent.json").json()
        skills = {s["name"]: s["description"] for s in card["skills"]}
        assert skills["agent_b"] == "Knows the secret word."


def test_dead_peer_costs_one_card_fetch_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permanently-dead peer: bounded startup retry (3 attempts, then give up),
    startup not blocked, and each later invocation performs exactly ONE card
    fetch — the delegate's own ``init`` — with the lazy repair adding zero
    extra requests (the no-added-latency guarantee for dead peers)."""
    B_TOOL_CALLS.clear()
    BOUND_TOOLS.clear()
    _patch_workspace_clients(monkeypatch)
    _fast_retries(monkeypatch)

    card_attempts = {"n": 0}

    def _refuse(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/agent.json":
            card_attempts["n"] += 1
        raise httpx.ConnectError("connection refused", request=request)

    _route_async_clients_to(monkeypatch, httpx.MockTransport(_refuse))
    _install_models(
        monkeypatch,
        {"model-a": _DelegatingModel(key="A", tool_name="agent", tool_args={"message": "hi"})},
    )

    agent_a = LlmAgent(tools=[], name="agent-a")
    app_a = create_app(
        agent_a,
        config=AgentConfig(name="agent-a", model="model-a", sub_agents=[B_URL]),
    )
    with TestClient(app_a) as client_a:
        assert card_attempts["n"] == 3, "startup retry is bounded at 3 attempts"
        # Degraded state is tracked, keyed by the sub-agent URL.
        assert B_URL in agent_a._degraded_sub_agents

        for expected in (4, 5):
            resp = client_a.post(
                "/invocations",
                json={"messages": [{"role": "user", "content": "try it"}]},
            )
            assert resp.status_code == 200, resp.text
            assert f"sub-agent at {B_URL} unreachable:" in _final_texts(resp.json())
            # Exactly one card fetch per call (RemoteDatabricksAgent.init) —
            # the repair path re-uses it instead of issuing its own.
            assert card_attempts["n"] == expected

        # Still degraded (peer never came up), still callable.
        assert B_URL in agent_a._degraded_sub_agents


def test_degraded_sub_agent_repairs_on_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Peer boots AFTER the parent's startup (the uncontrollable fleet boot
    race): startup registers the degraded stub, then the FIRST invocation both
    succeeds against the now-live peer AND repairs the stored descriptor."""
    B_TOOL_CALLS.clear()
    BOUND_TOOLS.clear()
    _patch_workspace_clients(monkeypatch)
    _fast_retries(monkeypatch)
    _install_models(
        monkeypatch,
        {
            "model-a": _DelegatingModel(
                key="A",
                # Card fetch failed → tool registered under the URL-derived name.
                tool_name="agent",
                tool_args={"message": "What is the secret word?"},
            ),
            "model-b": _DelegatingModel(key="B", tool_name="secret_word", tool_args={}),
        },
    )

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _route_async_clients_to(monkeypatch, httpx.MockTransport(_refuse))

    agent_a = LlmAgent(tools=[], name="agent-a")
    app_a = create_app(
        agent_a,
        config=AgentConfig(name="agent-a", model="model-a", sub_agents=[B_URL]),
    )
    with TestClient(app_a) as client_a:
        stub = agent_a._degraded_sub_agents[B_URL]
        assert "card fetch failed" in stub.description

        # Now agent B comes up — too late for A's startup fetch.
        agent_b = LlmAgent(tools=[secret_word], name="agent-b")
        app_b = create_app(
            agent_b,
            config=AgentConfig(
                name="agent-b",
                description="Knows the secret word.",
                model="model-b",
            ),
        )
        with TestClient(app_b):
            _route_async_clients_to(monkeypatch, httpx.ASGITransport(app=app_b))

            resp = client_a.post(
                "/invocations",
                json={"messages": [{"role": "user", "content": "What is the secret word?"}]},
            )

            assert resp.status_code == 200, resp.text
            # The call went through: B's tool body really executed.
            assert B_TOOL_CALLS == ["ran"], "agent B's tool never executed"
            assert SENTINEL in _final_texts(resp.json())

            # And the first use repaired the degraded entry in place:
            assert agent_a._degraded_sub_agents == {}
            assert stub.description == "Knows the secret word."
            delegate = next(fn for fn in agent_a._tool_fns if fn.__name__ == "agent")
            assert delegate.__doc__ == "Knows the secret word."
            # collect_tools (the /tools + MCP surface) reads __doc__ live.
            collected = {t.name: t.description for t in agent_a.collect_tools()}
            assert collected["agent"] == "Knows the secret word."


# ---------------------------------------------------------------------------
# #442: the peer's REAL input schema propagates — and structured args cross
# the wire as a JSON object, not prose squeezed into one message string.
# ---------------------------------------------------------------------------

B_TOOL_ARGS: list[str] = []


def secret_word_for(team: str) -> str:
    """Return the secret word for a team."""
    B_TOOL_ARGS.append(team)
    return f"{SENTINEL}-{team}"


def test_structured_schema_propagates_and_args_cross_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B's card advertises secret_word_for(team: str). Before #442, A's
    delegate collapsed that to {message: string}; now A's compiled graph
    exposes ``team`` and transmits {"team": ...} as JSON message content —
    which B's LLM sees verbatim."""
    import json

    from apx_agent._inspection import _inspect_tool_fn

    B_TOOL_CALLS.clear()
    B_TOOL_ARGS.clear()
    BOUND_TOOLS.clear()
    SEEN_USER_CONTENT.clear()
    _patch_workspace_clients(monkeypatch)
    _install_models(
        monkeypatch,
        {
            "model-a": _DelegatingModel(
                key="A", tool_name="agent_b", tool_args={"team": "blue"}
            ),
            "model-b": _DelegatingModel(
                key="B", tool_name="secret_word_for", tool_args={"team": "blue"}
            ),
        },
    )

    agent_b = LlmAgent(tools=[secret_word_for], name="agent-b")
    app_b = create_app(
        agent_b,
        config=AgentConfig(
            name="agent-b",
            description="Knows the secret word per team.",
            model="model-b",
        ),
    )
    with TestClient(app_b):
        _route_async_clients_to(monkeypatch, httpx.ASGITransport(app=app_b))

        agent_a = LlmAgent(tools=[], name="agent-a")
        app_a = create_app(
            agent_a,
            config=AgentConfig(name="agent-a", model="model-a", sub_agents=[B_URL]),
        )
        with TestClient(app_a) as client_a:
            # The delegate the compiled graph binds exposes B's real parameter.
            delegate = next(fn for fn in agent_a._tool_fns if fn.__name__ == "agent_b")
            plain_params, dep_names = _inspect_tool_fn(delegate)
            assert list(plain_params) == ["team"]
            assert dep_names == ["headers"]

            # The advertised descriptor carries the card schema, not {message}.
            descriptor = next(
                t for t in agent_a.collect_tools() if t.name == "agent_b"
            )
            assert descriptor.input_schema is not None
            assert "team" in descriptor.input_schema["properties"]
            assert "message" not in descriptor.input_schema["properties"]

            resp = client_a.post(
                "/invocations",
                json={
                    "messages": [
                        {"role": "user", "content": "Secret word for team blue?"}
                    ]
                },
            )

            assert resp.status_code == 200, resp.text
            # REALITY: B's tool body executed with the structured argument.
            assert B_TOOL_ARGS == ["blue"], "agent B's tool never got the arg"
            # WIRE: what B's LLM saw as the user message is the JSON args
            # object A's delegate serialized — named fields, not prose.
            assert json.loads(SEEN_USER_CONTENT["B"]) == {"team": "blue"}
            # And the sentinel still travels back to A's final answer.
            assert f"{SENTINEL}-blue" in _final_texts(resp.json())


# ---------------------------------------------------------------------------
# Cross-agent trace correlation (#443): both sides share ONE join tag.
# ---------------------------------------------------------------------------


def test_cross_agent_traces_join_on_one_tag(
    two_agents, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REALITY (#443): during a real A→B delegation, A's trace is tagged with
    the trace-id it SENT and B's trace with the trace-id it RECEIVED — and
    they are the same value under the same tag (apx.outbound.trace_id), so
    the two traces join on one tag equality. B also learns WHO called
    (apx.caller) and keeps the raw traceparent for debugging."""
    from apx_agent import _audit

    _, client_a = two_agents
    # A knows its own name from the Apps runtime env — that's what crosses
    # the boundary as x-apx-caller. Marking the process as an Apps runtime
    # arms the fail-closed OBO guard (G2), which this offline test satisfies
    # via the documented service-principal escape hatch.
    monkeypatch.setenv("DATABRICKS_APP_NAME", "agent-a")
    monkeypatch.setenv("APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK", "true")

    tag_calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        _audit, "set_trace_tags", lambda tags: tag_calls.append(dict(tags))
    )

    resp = client_a.post(
        "/invocations",
        json={"messages": [{"role": "user", "content": "What is the secret word?"}]},
    )
    assert resp.status_code == 200, resp.text
    assert SENTINEL in _final_texts(resp.json())  # the delegation really ran

    # Caller side (A): stamped when the delegate fired — trace-id only.
    caller_stamps = [
        t for t in tag_calls
        if "apx.outbound.trace_id" in t and "apx.traceparent" not in t
    ]
    # Receiver side (B): stamped from the incoming headers on /invocations.
    receiver_stamps = [t for t in tag_calls if "apx.traceparent" in t]
    assert caller_stamps, "caller side never stamped apx.outbound.trace_id"
    assert receiver_stamps, "receiver side never stamped apx.traceparent"

    sent = caller_stamps[0]["apx.outbound.trace_id"]
    received = receiver_stamps[0]
    # ONE tag equality joins the two traces across experiments.
    assert received["apx.outbound.trace_id"] == sent
    # The raw traceparent B recorded carries the same trace-id A sent…
    assert f"-{sent}-" in received["apx.traceparent"]
    # …and B knows who called.
    assert received["apx.caller"] == "agent-a"
