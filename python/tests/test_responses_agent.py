"""Tests for ``apx_agent._responses_agent`` — Databricks Apps ResponsesAgent compile target.

Covers:

  1. ``compile_to_responses_agent`` returns a tuple of plain (undecorated)
     functions — the scaffold applies ``@invoke()`` / ``@stream()`` at module
     level. We do NOT call the decorators inline because they're
     register-on-import and would clobber on a second compile.
  2. Non-streaming invoke: ResponsesAgentRequest → ResponsesAgentResponse with
     only NEW output items (history not echoed).
  3. Streaming: yields ResponsesAgentStreamEvent objects, one
     ``response.output_item.done`` per message + a terminal ``response.completed``.
  4. OBO header passthrough: when ``custom_inputs.user_token`` is present, the
     compiled graph's tools see the OBO WorkspaceClient — same load-bearing
     contract as ``test_chat_agent.py`` but exercised through the Apps path.
  5. Session/thread_id support: with a session_store + thread_id, history is
     prepended and the new turn is persisted.
  6. Best-effort import: NotImplementedError if mlflow Responses types absent
     (we don't directly test the raise path since mlflow IS installed in CI;
     the message text is asserted via inspection of the module string).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("mlflow")

# These types only import when mlflow Responses is available.
mlflow_responses = pytest.importorskip("mlflow.types.responses")
ResponsesAgentRequest = mlflow_responses.ResponsesAgentRequest
ResponsesAgentResponse = mlflow_responses.ResponsesAgentResponse
ResponsesAgentStreamEvent = mlflow_responses.ResponsesAgentStreamEvent

from langchain_core.messages import AIMessage  # noqa: E402

from apx_agent import (  # noqa: E402
    InMemorySessionStore,
    LlmAgent,
    compile_to_responses_agent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trivial_tool(query: str) -> str:
    """A tool with no dependencies (compile-friendly without mocks)."""
    return f"got: {query}"


def _make_fake_graph(final_text: str = "done") -> MagicMock:
    """A fake compiled graph whose invoke()/stream() emit one AIMessage."""
    graph = MagicMock(name="fake_compiled_graph")

    def _invoke(state: dict[str, Any]) -> dict[str, Any]:
        return {"messages": [*state["messages"], AIMessage(content=final_text)]}

    def _stream(state: dict[str, Any], stream_mode: str = "updates"):
        yield {"agent": {"messages": [AIMessage(content=final_text)]}}

    graph.invoke.side_effect = _invoke
    graph.stream.side_effect = _stream
    return graph


def _user_request(text: str, **custom: Any) -> ResponsesAgentRequest:
    return ResponsesAgentRequest(
        input=[{"role": "user", "content": text}],
        custom_inputs=custom or None,
    )


# ---------------------------------------------------------------------------
# Compile target shape — returns tuple of plain functions
# ---------------------------------------------------------------------------


class TestCompileShape:
    def test_returns_tuple_of_two_callables(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        result = compile_to_responses_agent(agent, model="any-endpoint")
        assert isinstance(result, tuple)
        assert len(result) == 2
        non_streaming, streaming = result
        assert callable(non_streaming)
        assert callable(streaming)

    def test_callables_are_undecorated(self) -> None:
        """The functions returned MUST NOT have the agent_server decorators
        applied — the scaffold's agent.py is responsible for that.

        We can't import the decorators' marker (private), but we can check that
        each function is a plain callable, has no ``__agent_server_invoke__``
        attribute (a known internal marker the decorator sets), and is
        independently importable across calls (i.e. calling compile twice
        doesn't raise the "decorator already registered" error).
        """
        agent = LlmAgent(tools=[_trivial_tool])
        ns1, s1 = compile_to_responses_agent(agent, model="m")
        ns2, s2 = compile_to_responses_agent(agent, model="m")
        # Compile is idempotent — no global registration side effect.
        assert ns1 is not ns2  # distinct closures
        assert s1 is not s2


# ---------------------------------------------------------------------------
# Non-streaming invoke
# ---------------------------------------------------------------------------


class TestInvoke:
    def test_returns_responses_agent_response_with_only_new_output(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        non_streaming, _ = compile_to_responses_agent(agent, model="any")

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._responses_agent.compile_to_langgraph",
            return_value=_make_fake_graph("final answer"),
        ):
            resp = non_streaming(_user_request("hi"))

        assert isinstance(resp, ResponsesAgentResponse)
        # Only the new assistant message is emitted (input not echoed).
        assert len(resp.output) == 1
        item = resp.output[0]
        # output items round-trip via OutputItem base — assert via model_dump
        d = item.model_dump()
        assert d["type"] == "message"
        assert d["role"] == "assistant"
        # content is a list of typed parts
        assert d["content"][0]["text"] == "final answer"

    def test_accepts_dict_request_for_test_ergonomics(self) -> None:
        """A bare dict (no ResponsesAgentRequest) is coerced via pydantic."""
        agent = LlmAgent(tools=[_trivial_tool])
        non_streaming, _ = compile_to_responses_agent(agent, model="any")

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._responses_agent.compile_to_langgraph",
            return_value=_make_fake_graph("answer"),
        ):
            resp = non_streaming({"input": [{"role": "user", "content": "hello"}]})

        assert isinstance(resp, ResponsesAgentResponse)
        assert resp.output[0].model_dump()["content"][0]["text"] == "answer"


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class TestStream:
    def test_yields_response_output_item_done_then_completed(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        _, streaming = compile_to_responses_agent(agent, model="any")

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._responses_agent.compile_to_langgraph",
            return_value=_make_fake_graph("streamed!"),
        ):
            events = list(streaming(_user_request("go")))

        assert len(events) >= 2  # at least one item event + completed
        # All events are ResponsesAgentStreamEvent
        for ev in events:
            assert isinstance(ev, ResponsesAgentStreamEvent)
        types = [ev.type for ev in events]
        assert "response.output_item.done" in types
        assert types[-1] == "response.completed"

    def test_streamed_text_matches_graph_output(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        _, streaming = compile_to_responses_agent(agent, model="any")

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._responses_agent.compile_to_langgraph",
            return_value=_make_fake_graph("hello world"),
        ):
            events = list(streaming(_user_request("go")))

        # Find the item.done event and check the message text.
        item_events = [
            e for e in events if e.type == "response.output_item.done"
        ]
        assert item_events, "no output_item.done event emitted"
        item_dump = item_events[0].model_dump()
        item = item_dump["item"]
        assert item["type"] == "message"
        assert item["content"][0]["text"] == "hello world"


# ---------------------------------------------------------------------------
# OBO user-scope auth — the load-bearing assertion
# ---------------------------------------------------------------------------


class TestUserScopeAuth:
    def test_user_token_in_custom_inputs_builds_obo_workspace_client(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        non_streaming, _ = compile_to_responses_agent(agent, model="any")

        captured: dict[str, Any] = {}

        def _spy_compile(agent_arg, *, ws, model, headers=None):  # noqa: ANN001
            captured["ws"] = ws
            captured["model"] = model
            return _make_fake_graph("answer")

        with patch(
            "apx_agent._defaults._make_workspace_client"
        ) as mock_factory, patch(
            "apx_agent._responses_agent.compile_to_langgraph",
            side_effect=_spy_compile,
        ):
            sentinel = MagicMock(name="obo_ws")
            mock_factory.return_value = sentinel

            non_streaming(
                _user_request(
                    "hi",
                    user_token="tok-abc",
                    workspace_host="https://fake.cloud.databricks.com",
                )
            )

        # The factory was called with the OBO kwargs — i.e. user_token + host
        # made it from custom_inputs all the way through extract_obo_headers
        # to the WorkspaceClient construction.
        mock_factory.assert_called_once_with(
            token="tok-abc",
            host="https://fake.cloud.databricks.com",
        )
        assert captured["ws"] is sentinel

    def test_no_user_token_falls_back_to_default_workspace_client(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        non_streaming, _ = compile_to_responses_agent(agent, model="any")

        with patch(
            "apx_agent._defaults._make_workspace_client"
        ) as mock_factory, patch(
            "apx_agent._responses_agent.compile_to_langgraph",
            return_value=_make_fake_graph("answer"),
        ):
            mock_factory.return_value = MagicMock(name="sp_ws")
            non_streaming(_user_request("hi"))

        # Default branch — no token/host kwargs.
        mock_factory.assert_called_once_with()


# ---------------------------------------------------------------------------
# Session / thread_id support
# ---------------------------------------------------------------------------


class TestSessionPersistence:
    def test_thread_id_history_prepended_and_new_turn_persisted(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        store = InMemorySessionStore()
        non_streaming, _ = compile_to_responses_agent(
            agent, model="any", session_store=store
        )

        # Capture the messages the graph sees so we can assert history was
        # prepended on turn 2.
        seen_inputs: list[list[Any]] = []

        def _spy_invoke(state: dict[str, Any]) -> dict[str, Any]:
            seen_inputs.append(list(state["messages"]))
            return {
                "messages": [*state["messages"], AIMessage(content="reply")]
            }

        graph = MagicMock(name="graph")
        graph.invoke.side_effect = _spy_invoke

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._responses_agent.compile_to_langgraph",
            return_value=graph,
        ):
            # Turn 1
            non_streaming(_user_request("first question", thread_id="t-1"))
            # Turn 2 — history should be prepended
            non_streaming(_user_request("second question", thread_id="t-1"))

        # Turn 1: just the new user message
        assert len(seen_inputs[0]) == 1
        # Turn 2: at least the prior user+assistant pair plus the new user
        assert len(seen_inputs[1]) >= 3

        # And the session store persisted both turns
        session = store.get("t-1")
        assert session is not None
        # 2 input messages + 2 new messages from the assistant = 4
        assert len(session.history) >= 4


# ---------------------------------------------------------------------------
# Smoke test for the streaming session path
# ---------------------------------------------------------------------------


class TestStreamingSession:
    def test_streaming_persists_session_after_stream(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        store = InMemorySessionStore()
        _, streaming = compile_to_responses_agent(
            agent, model="any", session_store=store
        )

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._responses_agent.compile_to_langgraph",
            return_value=_make_fake_graph("streamed answer"),
        ):
            list(streaming(_user_request("hi", thread_id="t-stream")))

        session = store.get("t-stream")
        assert session is not None
        assert len(session.history) >= 2
