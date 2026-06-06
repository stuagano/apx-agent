"""Serving-conformance suite: ChatAgent adapter vs ResponsesAgent adapter.

apx-agent's DSL defines ONE declarative agent that is served two ways via two
adapters that diverge ONLY at the serving boundary but must behave IDENTICALLY:

  * **ChatAgent adapter** (dev loop / ``apx-agent run``):
    ``chat_agent_for(agent, model=...)`` →
    ``_ApxChatAgent.predict(messages, custom_inputs)``. Wire shape
    ``{"messages": [...]}`` (``ChatAgentMessage`` list).
  * **ResponsesAgent adapter** (deploy):
    ``compile_to_responses_agent(agent, model=...)`` →
    ``(non_streaming_fn, stream_fn)``;
    ``non_streaming_fn(ResponsesAgentRequest(input=[...], custom_inputs=...))``.
    Wire shape ``{"input": [...]}``.

Both funnel into the SAME ``compile_to_langgraph(agent, *, ws, model, headers)``
and both resolve OBO via ``apx_agent._obo.extract_obo_headers``. Three real bugs
fixed this cycle were all *behavioral divergence between these two adapters*:
OBO workspace-client built differently (#119), tracing on in one path not the
other (#120), dev UI hardcoded to one wire shape (#124).

This suite encodes the contract that the two adapters are behaviorally
equivalent: it drives BOTH adapters with the SAME logical request (the model
stubbed out) and asserts the seams they feed into the shared core — and the
output they return — are identical. The next behavioral leak is caught before
it ships.

Where the two adapters genuinely CANNOT be made identical (e.g. the session key
``session_id`` vs ``thread_id``), the divergence is documented in a focused test
rather than forced — so the contract is honest, not green-by-fudging.

Skips if optional extras (``langgraph``, ``eval``/mlflow) are not installed.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("mlflow")

# Responses contract types only import when mlflow >= 3.x is available.
mlflow_responses = pytest.importorskip("mlflow.types.responses")
ResponsesAgentRequest = mlflow_responses.ResponsesAgentRequest

from langchain_core.messages import AIMessage  # noqa: E402
from mlflow.types.agent import ChatAgentMessage  # noqa: E402

from apx_agent import (  # noqa: E402
    LlmAgent,
    chat_agent_for,
    compile_to_responses_agent,
)


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------

MODEL = "databricks-claude-sonnet-4-6"


def _trivial_tool(query: str) -> str:
    """A tool with no dependencies (compile-friendly without mocks)."""
    return f"got: {query}"


def _make_agent() -> LlmAgent:
    return LlmAgent(tools=[_trivial_tool])


def _json_args(args: Any) -> str:
    """Stable JSON rendering of a tool-call ``args`` dict for tuple comparison."""
    if isinstance(args, str):
        return args
    return json.dumps(args, sort_keys=True, default=str)


def _normalize_lc(msgs: list[Any]) -> list[tuple[Any, ...]]:
    """Normalize a langchain message list to comparable tuples.

    Captures the load-bearing identity of each message: its type, content,
    its tool-calls (name/args/id, order-stable), and its tool_call_id. This is
    the shape the shared core (``compile_to_langgraph`` graph) actually sees, so
    two adapters that produce equal normalized lists feed the core identically.
    """
    out: list[tuple[Any, ...]] = []
    for m in msgs:
        tool_calls = tuple(
            sorted(
                (tc["name"], _json_args(tc["args"]), tc["id"])
                for tc in (getattr(m, "tool_calls", None) or [])
            )
        )
        out.append(
            (
                m.type,
                m.content,
                tool_calls,
                getattr(m, "tool_call_id", None),
            )
        )
    return out


def _normalize_headers(h: Any) -> tuple[Any, ...] | None:
    """Normalize a ``DatabricksAppsHeaders`` (or None) to a comparable tuple.

    Compares the load-bearing identity fields directly rather than relying on
    pydantic ``__eq__`` over ``SecretStr`` — clearer failure messages, no
    equality surprises around the wrapped secret.
    """
    if h is None:
        return None
    token = h.token.get_secret_value() if h.token else None
    return (h.host, h.user_id, h.user_email, token)


class _Capture:
    """What a single adapter fed into the shared core + what it returned."""

    def __init__(self) -> None:
        self.ws: Any = None
        self.model: Any = None
        self.headers: Any = None
        self.graph_input: list[Any] = []
        self.factory_call: Any = None  # mock_factory.call_args
        self.response: Any = None
        self._mock_factory: Any = None  # the patched _make_workspace_client mock


def _spy_compile_and_graph(cap: _Capture, final_text: str) -> Any:
    """Return a ``compile_to_langgraph`` spy that records (ws, model, headers)
    and a fake graph whose ``.invoke`` records the langchain input messages.

    The spy captures the graph-input message list by having the fake graph's
    ``.invoke`` write ``state["messages"]`` into ``cap.graph_input`` before
    producing output — exactly the messages the shared core would consume.
    """

    def _invoke(state: dict[str, Any]) -> dict[str, Any]:
        cap.graph_input = list(state["messages"])
        return {"messages": [*state["messages"], AIMessage(content=final_text)]}

    fake_graph = MagicMock(name="fake_graph")
    fake_graph.invoke.side_effect = _invoke

    def _spy(agent_arg: Any, *, ws: Any, model: Any, headers: Any = None) -> Any:
        cap.ws = ws
        cap.model = model
        cap.headers = headers
        return fake_graph

    return _spy


# ---------------------------------------------------------------------------
# The harness — ONE patch context PER path
# ---------------------------------------------------------------------------
#
# Each adapter runs in its OWN patch context (NOT a shared one) so that
# per-path ``_make_workspace_client.assert_called_once_with(...)`` assertions
# are honest: a single shared mock would record two calls and break
# ``assert_called_once``. The SAME ``sentinel_ws`` object is threaded into both
# so test 1's "captured ws IS the sentinel in both paths" is a literal ``is``.


def _run_chat(
    agent: LlmAgent,
    *,
    messages: list[ChatAgentMessage],
    custom_inputs: dict[str, Any] | None,
    sentinel_ws: Any,
    final_text: str = "OK",
) -> _Capture:
    """Drive the ChatAgent adapter for one logical request; capture the seams."""
    cap = _Capture()
    wrapped = chat_agent_for(agent, model=MODEL)
    with patch(
        "apx_agent._defaults._make_workspace_client",
        return_value=sentinel_ws,
    ) as mock_factory, patch(
        "apx_agent._chat_agent.compile_to_langgraph",
        side_effect=_spy_compile_and_graph(cap, final_text),
    ):
        cap.response = wrapped.predict(
            messages=messages, custom_inputs=custom_inputs
        )
        cap.factory_call = mock_factory.call_args
        cap._mock_factory = mock_factory
    return cap


def _run_responses(
    agent: LlmAgent,
    *,
    input_items: list[dict[str, Any]],
    custom_inputs: dict[str, Any] | None,
    sentinel_ws: Any,
    final_text: str = "OK",
) -> _Capture:
    """Drive the ResponsesAgent adapter for one logical request; capture seams."""
    cap = _Capture()
    non_streaming, _ = compile_to_responses_agent(agent, model=MODEL)
    with patch(
        "apx_agent._defaults._make_workspace_client",
        return_value=sentinel_ws,
    ) as mock_factory, patch(
        "apx_agent._responses_agent.compile_to_langgraph",
        side_effect=_spy_compile_and_graph(cap, final_text),
    ):
        cap.response = non_streaming(
            ResponsesAgentRequest(
                input=input_items, custom_inputs=custom_inputs or None
            )
        )
        cap.factory_call = mock_factory.call_args
        cap._mock_factory = mock_factory
    return cap


def _run_both(
    *,
    chat_messages: list[ChatAgentMessage],
    responses_input: list[dict[str, Any]],
    custom_inputs: dict[str, Any] | None = None,
    sentinel_ws: Any | None = None,
    final_text: str = "OK",
) -> tuple[_Capture, _Capture]:
    """Run the SAME logical request through both adapters.

    Both wire shapes are supplied by hand (``chat_messages`` AND
    ``responses_input``) — the harness deliberately does NOT auto-convert one to
    the other, because that would re-implement the exact conversion code under
    test. The agent and ``custom_inputs`` ARE shared; only the wire framing
    differs, which is precisely the boundary under test.
    """
    sentinel_ws = sentinel_ws if sentinel_ws is not None else MagicMock(name="ws")
    agent = _make_agent()
    chat_cap = _run_chat(
        agent,
        messages=chat_messages,
        custom_inputs=custom_inputs,
        sentinel_ws=sentinel_ws,
        final_text=final_text,
    )
    resp_cap = _run_responses(
        agent,
        input_items=responses_input,
        custom_inputs=custom_inputs,
        sentinel_ws=sentinel_ws,
        final_text=final_text,
    )
    return chat_cap, resp_cap


def _responses_assistant_text(response: Any) -> str:
    """Dig the assistant text out of a ResponsesAgentResponse.output list.

    The assistant turn is a ``{"type":"message","content":[{"type":
    "output_text","text":...}]}`` item.
    """
    for item in response.output:
        d = item.model_dump() if hasattr(item, "model_dump") else item
        if d.get("type") == "message" and d.get("role") == "assistant":
            for part in d.get("content", []) or []:
                if part.get("type") == "output_text":
                    return part.get("text", "")
    return ""


# ---------------------------------------------------------------------------
# 1. Same OBO workspace client (#119)
# ---------------------------------------------------------------------------


class TestSameOboWorkspaceClient:
    def test_same_obo_workspace_client_kwargs_and_sentinel(self) -> None:
        sentinel = MagicMock(name="obo_ws")
        ci = {
            "user_token": "tok",
            "workspace_host": "https://ws.databricks.com",
        }
        chat_cap, resp_cap = _run_both(
            chat_messages=[ChatAgentMessage(role="user", content="hi", id="u1")],
            responses_input=[{"role": "user", "content": "hi"}],
            custom_inputs=ci,
            sentinel_ws=sentinel,
        )

        # Both paths built the WorkspaceClient with the SAME token+host kwargs.
        expected = {"token": "tok", "host": "https://ws.databricks.com"}
        assert chat_cap.factory_call.kwargs == expected
        assert resp_cap.factory_call.kwargs == expected
        assert chat_cap.factory_call.kwargs == resp_cap.factory_call.kwargs

        # Each path called the factory exactly once.
        chat_cap._mock_factory.assert_called_once()
        resp_cap._mock_factory.assert_called_once()

        # The ws handed to compile_to_langgraph IS the OBO sentinel in BOTH.
        assert chat_cap.ws is sentinel
        assert resp_cap.ws is sentinel


# ---------------------------------------------------------------------------
# 2. Same headers when user_id present (and both None when absent)
# ---------------------------------------------------------------------------


class TestSameHeaders:
    def test_headers_identical_when_user_id_present(self) -> None:
        ci = {
            "user_token": "tok",
            "workspace_host": "https://ws.databricks.com",
            "user_id": "u-123",
            "user_email": "a@b.com",
        }
        chat_cap, resp_cap = _run_both(
            chat_messages=[ChatAgentMessage(role="user", content="hi", id="u1")],
            responses_input=[{"role": "user", "content": "hi"}],
            custom_inputs=ci,
        )

        chat_h = _normalize_headers(chat_cap.headers)
        resp_h = _normalize_headers(resp_cap.headers)
        assert chat_h is not None
        assert resp_h is not None
        # Same host, user_id, user_email, token in both paths.
        assert chat_h == resp_h
        assert chat_h == (
            "https://ws.databricks.com",
            "u-123",
            "a@b.com",
            "tok",
        )

    def test_headers_none_in_both_when_no_user_id(self) -> None:
        # No user_id → both adapters keep headers=None (preserves the
        # NO_PRINCIPAL semantics for requests without a user context).
        ci = {
            "user_token": "tok",
            "workspace_host": "https://ws.databricks.com",
        }
        chat_cap, resp_cap = _run_both(
            chat_messages=[ChatAgentMessage(role="user", content="hi", id="u1")],
            responses_input=[{"role": "user", "content": "hi"}],
            custom_inputs=ci,
        )
        assert chat_cap.headers is None
        assert resp_cap.headers is None


# ---------------------------------------------------------------------------
# 3. Same langchain messages — simple single user turn (#124 wire shape)
# ---------------------------------------------------------------------------


class TestSameMessagesSimple:
    def test_single_user_message_normalizes_equal(self) -> None:
        chat_cap, resp_cap = _run_both(
            chat_messages=[ChatAgentMessage(role="user", content="hi", id="u1")],
            responses_input=[{"role": "user", "content": "hi"}],
        )
        chat_norm = _normalize_lc(chat_cap.graph_input)
        resp_norm = _normalize_lc(resp_cap.graph_input)
        assert chat_norm == resp_norm
        # And it really is a single HumanMessage("hi").
        assert chat_norm == [("human", "hi", (), None)]


# ---------------------------------------------------------------------------
# 4. Same langchain messages — tool-call history round-trip (subtlest leak)
# ---------------------------------------------------------------------------


class TestSameMessagesToolCallHistory:
    def test_tool_call_history_normalizes_equal(self) -> None:
        """The SAME logical multi-turn convo expressed in each wire shape:
        a prior assistant tool call + its tool result, then a new user turn.

        Both must normalize to the SAME langchain list:
        ``AIMessage(tool_calls=[...]) + ToolMessage + HumanMessage``.

        Construction notes (verified against the conversion code):
          * The Responses ``function_call`` path hardcodes ``AIMessage(content
            ="")``, so the ChatAgent assistant turn must carry ``content=""``
            for the two to match.
          * The same JSON ``arguments`` string is supplied on both wire shapes
            so both coerce to the same args dict.
          * tool-call ``id`` == Responses ``call_id``; ToolMessage content ==
            ``function_call_output`` ``output``.
        """
        args_json = json.dumps({"query": "SELECT 1"})

        # Wire-shape tool call as ChatAgentMessage accepts it on the wire
        # ({"id","type","function":{"name","arguments":<json str>}}). Typed
        # loose because ChatAgentMessage.tool_calls validates these dicts at
        # runtime via pydantic.
        assistant_tool_calls: list[Any] = [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "run_sql",
                    "arguments": args_json,
                },
            }
        ]
        chat_messages = [
            ChatAgentMessage(
                role="assistant",
                content="",
                id="a1",
                tool_calls=assistant_tool_calls,
            ),
            ChatAgentMessage(
                role="tool",
                content="result-rows",
                id="t1",
                name="run_sql",
                tool_call_id="call_1",
            ),
            ChatAgentMessage(role="user", content="and now?", id="u2"),
        ]

        responses_input = [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "run_sql",
                "arguments": args_json,
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "result-rows",
            },
            {"role": "user", "content": "and now?"},
        ]

        chat_cap, resp_cap = _run_both(
            chat_messages=chat_messages,
            responses_input=responses_input,
        )
        chat_norm = _normalize_lc(chat_cap.graph_input)
        resp_norm = _normalize_lc(resp_cap.graph_input)

        # The load-bearing assertion: the tool-call round-trip is identical
        # across adapters. If this fails, it's a REAL divergence (the subtlest
        # leak) — report it, don't weaken.
        assert chat_norm == resp_norm

        # And it really is AIMessage(tool_calls) + ToolMessage + HumanMessage.
        assert chat_norm == [
            ("ai", "", (("run_sql", _json_args({"query": "SELECT 1"}), "call_1"),), None),
            ("tool", "result-rows", (), "call_1"),
            ("human", "and now?", (), None),
        ]


# ---------------------------------------------------------------------------
# 5. Same output
# ---------------------------------------------------------------------------


class TestSameOutput:
    def test_assistant_output_text_matches(self) -> None:
        chat_cap, resp_cap = _run_both(
            chat_messages=[ChatAgentMessage(role="user", content="hi", id="u1")],
            responses_input=[{"role": "user", "content": "hi"}],
            final_text="hello world",
        )
        chat_text = chat_cap.response.messages[-1].content
        resp_text = _responses_assistant_text(resp_cap.response)
        assert chat_text == "hello world"
        assert chat_text == resp_text


# ---------------------------------------------------------------------------
# 6. No-OBO fallback parity (local-dev)
# ---------------------------------------------------------------------------


class TestNoOboFallbackParity:
    def test_no_custom_inputs_calls_factory_with_no_kwargs_in_both(self) -> None:
        chat_cap, resp_cap = _run_both(
            chat_messages=[ChatAgentMessage(role="user", content="hi", id="u1")],
            responses_input=[{"role": "user", "content": "hi"}],
            custom_inputs=None,
        )
        # Both paths fall back to the default SP/CLI client: factory called
        # with NO token/host kwargs.
        chat_cap._mock_factory.assert_called_once_with()
        resp_cap._mock_factory.assert_called_once_with()

    def test_empty_custom_inputs_calls_factory_with_no_kwargs_in_both(self) -> None:
        chat_cap, resp_cap = _run_both(
            chat_messages=[ChatAgentMessage(role="user", content="hi", id="u1")],
            responses_input=[{"role": "user", "content": "hi"}],
            custom_inputs={},
        )
        chat_cap._mock_factory.assert_called_once_with()
        resp_cap._mock_factory.assert_called_once_with()


# ---------------------------------------------------------------------------
# Known asymmetry — session key (session_id vs thread_id)
# ---------------------------------------------------------------------------
#
# This is a genuine, intentional difference between the two adapters, NOT a
# bug. It is documented here rather than forced so the conformance contract is
# honest:
#
#   * ChatAgent (Model Serving) ``_load_session`` reads ``session_id`` ONLY.
#   * ResponsesAgent (Apps) ``_load_session`` reads ``thread_id`` OR
#     ``session_id`` (Apps convention is ``thread_id``, with ``session_id`` as
#     a symmetry fallback).
#
# Consequence: a request carrying ONLY ``thread_id`` loads a session on the
# Responses path but NOT on the ChatAgent path. With ``session_id``, BOTH load.


class TestSessionKeyAsymmetryIsKnown:
    def test_session_id_loads_in_both_thread_id_only_loads_in_responses(
        self,
    ) -> None:
        from apx_agent import InMemorySessionStore

        # --- session_id: both adapters load -------------------------------
        store_chat = InMemorySessionStore()
        store_resp = InMemorySessionStore()
        agent = _make_agent()

        chat = chat_agent_for(agent, model=MODEL, session_store=store_chat)
        resp_ns, _ = compile_to_responses_agent(
            agent, model=MODEL, session_store=store_resp
        )

        fake = MagicMock(name="g")
        fake.invoke.side_effect = lambda s: {
            "messages": [*s["messages"], AIMessage(content="ok")]
        }

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="ws"),
        ), patch(
            "apx_agent._chat_agent.compile_to_langgraph", return_value=fake
        ), patch(
            "apx_agent._responses_agent.compile_to_langgraph", return_value=fake
        ):
            chat.predict(
                messages=[ChatAgentMessage(role="user", content="hi", id="u1")],
                custom_inputs={"session_id": "s-1"},
            )
            resp_ns(
                ResponsesAgentRequest(
                    input=[{"role": "user", "content": "hi"}],
                    custom_inputs={"session_id": "s-1"},
                )
            )

        # With session_id, BOTH adapters created+persisted a session.
        assert store_chat.get("s-1") is not None
        assert store_resp.get("s-1") is not None

        # --- thread_id ONLY: Responses loads, ChatAgent does NOT ----------
        store_chat2 = InMemorySessionStore()
        store_resp2 = InMemorySessionStore()
        chat2 = chat_agent_for(agent, model=MODEL, session_store=store_chat2)
        resp_ns2, _ = compile_to_responses_agent(
            agent, model=MODEL, session_store=store_resp2
        )

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="ws"),
        ), patch(
            "apx_agent._chat_agent.compile_to_langgraph", return_value=fake
        ), patch(
            "apx_agent._responses_agent.compile_to_langgraph", return_value=fake
        ):
            chat2.predict(
                messages=[ChatAgentMessage(role="user", content="hi", id="u1")],
                custom_inputs={"thread_id": "t-1"},
            )
            resp_ns2(
                ResponsesAgentRequest(
                    input=[{"role": "user", "content": "hi"}],
                    custom_inputs={"thread_id": "t-1"},
                )
            )

        # KNOWN ASYMMETRY: Responses honors thread_id; ChatAgent ignores it.
        assert store_resp2.get("t-1") is not None
        assert store_chat2.get("t-1") is None
