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
  5. Session/thread_id support: with a conversation_store + thread_id, history is
     prepended and the new turn is persisted.
  6. Best-effort import: NotImplementedError if mlflow Responses types absent
     (we don't directly test the raise path since mlflow IS installed in CI;
     the message text is asserted via inspection of the module string).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
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
from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_core.messages import SystemMessage  # noqa: E402
from langchain_core.messages import ToolMessage  # noqa: E402

from apx_agent import (  # noqa: E402
    InMemoryConversationStore,
    LlmAgent,
    compile_to_responses_agent,
)
from apx_agent._executor import (  # noqa: E402
    ExecutorError,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    TurnComplete,
)
from apx_agent._responses_agent import (  # noqa: E402
    _conv_items_to_lc_messages,
    _executor_events_to_items,
    _lc_to_openai_messages,
    _persist_conv_turn,
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

    def _stream(state: dict[str, Any], stream_mode: Any = "updates"):
        # Combined stream-mode (#287): "messages" yields LLM token chunks (sharing
        # one message id with the final assembled message), then "updates" yields
        # the completed step. Mirrors langgraph's (mode, data) tuple contract.
        from langchain_core.messages import AIMessageChunk

        words = final_text.split(" ")
        for i, word in enumerate(words):
            piece = word if i == 0 else " " + word
            yield ("messages", (AIMessageChunk(content=piece, id="ai-1"), {"langgraph_node": "agent"}))
        yield ("updates", {"agent": {"messages": [AIMessage(content=final_text, id="ai-1")]}})

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

    def test_emits_output_text_deltas_for_tokens(self) -> None:
        """#287: token chunks from the "messages" stream-mode surface as
        response.output_text.delta events, in addition to the per-step
        output_item.done — true word-by-word streaming."""
        agent = LlmAgent(tools=[_trivial_tool])
        _, streaming = compile_to_responses_agent(agent, model="any")

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._responses_agent.compile_to_langgraph",
            return_value=_make_fake_graph("hello world from apx"),
        ):
            events = list(streaming(_user_request("go")))

        deltas = [e for e in events if e.type == "response.output_text.delta"]
        assert len(deltas) >= 2, "expected token-by-token deltas, not one block"
        # The deltas reassemble the final answer text.
        assert "".join(e.model_dump()["delta"] for e in deltas) == "hello world from apx"
        # Each delta correlates to its output_item.done via a shared item_id.
        done = next(e for e in events if e.type == "response.output_item.done")
        done_item_id = done.model_dump()["item"]["id"]
        assert all(e.model_dump()["item_id"] == done_item_id for e in deltas)
        # The terminal completed event still arrives last.
        assert events[-1].type == "response.completed"

    def test_non_text_chunks_do_not_emit_deltas(self) -> None:
        """A tool-call argument chunk (empty content) must not emit a text delta;
        only visible token text streams."""
        from langchain_core.messages import AIMessage, AIMessageChunk

        agent = LlmAgent(tools=[_trivial_tool])
        _, streaming = compile_to_responses_agent(agent, model="any")

        def _stream(state: dict[str, Any], stream_mode: Any = "updates"):
            # empty-content chunk (e.g. tool-call arg delta) → no text delta
            yield ("messages", (AIMessageChunk(content="", id="ai-9"), {}))
            yield ("updates", {"agent": {"messages": [AIMessage(content="final", id="ai-9")]}})

        graph = MagicMock(name="graph")
        graph.stream.side_effect = _stream

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._responses_agent.compile_to_langgraph",
            return_value=graph,
        ):
            events = list(streaming(_user_request("go")))

        assert not [e for e in events if e.type == "response.output_text.delta"]
        assert [e for e in events if e.type == "response.output_item.done"]

    def test_idless_token_chunk_correlates_via_index_fallback(self) -> None:
        """When a token chunk carries no id, its delta must still correlate to
        the output_item.done — both fall back to ``msg-{output_index}``."""
        from langchain_core.messages import AIMessage, AIMessageChunk

        agent = LlmAgent(tools=[_trivial_tool])
        _, streaming = compile_to_responses_agent(agent, model="any")

        def _stream(state: dict[str, Any], stream_mode: Any = "updates"):
            # token chunk with no id → delta must fall back to the index, not ""
            yield ("messages", (AIMessageChunk(content="hi", id=None), {}))
            yield ("updates", {"agent": {"messages": [AIMessage(content="hi", id=None)]}})

        graph = MagicMock(name="graph")
        graph.stream.side_effect = _stream

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._responses_agent.compile_to_langgraph",
            return_value=graph,
        ):
            events = list(streaming(_user_request("go")))

        deltas = [e for e in events if e.type == "response.output_text.delta"]
        done = next(e for e in events if e.type == "response.output_item.done")
        done_item_id = done.model_dump()["item"]["id"]
        assert done_item_id == "msg-0"
        assert deltas and all(e.model_dump()["item_id"] == done_item_id for e in deltas)

    def test_human_message_echo_does_not_crash_stream(self) -> None:
        """Regression: workflow agents (RouterAgent) inject HumanMessages into
        the graph state when handing off to a sub-agent. The streaming path
        used to forward those as ``role: user`` items, which
        ``ResponsesAgentStreamEvent`` rejects ("Invalid role: user. Must be
        'assistant'."). They must be filtered out, not crash the stream."""
        from langchain_core.messages import HumanMessage

        agent = LlmAgent(tools=[_trivial_tool])
        _, streaming = compile_to_responses_agent(agent, model="any")

        def _stream_with_human(state: dict[str, Any], stream_mode: Any = "updates"):
            # router re-states the query to the sub-agent (role=user) ...
            yield ("updates", {"router": {"messages": [HumanMessage(content="routed query")]}})
            # ... then the sub-agent answers (role=assistant)
            yield ("updates", {"sub": {"messages": [AIMessage(content="the answer")]}})

        graph = MagicMock(name="router_graph")
        graph.stream.side_effect = _stream_with_human

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._responses_agent.compile_to_langgraph",
            return_value=graph,
        ):
            events = list(streaming(_user_request("go")))

        item_events = [e for e in events if e.type == "response.output_item.done"]
        # The HumanMessage echo is dropped; only the assistant answer is emitted.
        assert len(item_events) == 1
        item = item_events[0].model_dump()["item"]
        assert item["role"] == "assistant"
        assert item["content"][0]["text"] == "the answer"
        assert events[-1].type == "response.completed"


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
        store = InMemoryConversationStore()
        non_streaming, _ = compile_to_responses_agent(
            agent, model="any", conversation_store=store
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

        # The conversation store persisted items for both turns
        conv = store.get_conversation("t-1")
        assert conv is not None
        items = store.list_items("t-1").data
        # 2 input messages + 2 new messages from the assistant = 4
        assert len(items) >= 4


# ---------------------------------------------------------------------------
# Smoke test for the streaming session path
# ---------------------------------------------------------------------------


class TestStreamingSession:
    def test_streaming_persists_session_after_stream(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        store = InMemoryConversationStore()
        _, streaming = compile_to_responses_agent(
            agent, model="any", conversation_store=store
        )

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._responses_agent.compile_to_langgraph",
            return_value=_make_fake_graph("streamed answer"),
        ):
            list(streaming(_user_request("hi", thread_id="t-stream")))

        conv = store.get_conversation("t-stream")
        assert conv is not None
        items = store.list_items("t-stream").data
        assert len(items) >= 2


# ---------------------------------------------------------------------------
# Phase C — executor="claude-sdk" helpers and serving path
# ---------------------------------------------------------------------------


# --- Fake async streaming client (no real credentials required) ---


def _ns_text_chunk(text: str) -> SimpleNamespace:
    """Fake OpenAI streaming chunk carrying a text delta.

    :param text: The text content of the delta.
    :returns: A ``SimpleNamespace`` shaped like a ``ChatCompletionChunk``.
    """
    delta = SimpleNamespace(content=text, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=None)
    return SimpleNamespace(choices=[choice], usage=None)


def _ns_finish_chunk() -> SimpleNamespace:
    """Terminal empty streaming chunk with no delta content.

    :returns: A ``SimpleNamespace`` with empty choices list.
    """
    return SimpleNamespace(choices=[], usage=None)


class _FakeCompletions:
    """Fake ``AsyncCompletions`` resource consumed FIFO per ``create()`` call.

    :param call_chunks: List of per-call chunk sequences.
    """

    def __init__(self, call_chunks: list[list[Any]]) -> None:
        """Initialise the fake completions with a call queue.

        :param call_chunks: Ordered list of chunk sequences, one per call.
        """
        self._queue: list[list[Any]] = list(call_chunks)

    async def create(self, **kwargs: Any) -> Any:
        """Return an async generator over the next queued chunk sequence.

        :param kwargs: Ignored call kwargs.
        :returns: Async generator yielding the next queued chunks.
        """
        chunks = self._queue.pop(0)

        async def _gen() -> Any:
            for c in chunks:
                yield c

        return _gen()


def _make_sdk_fake_client(call_chunks: list[list[Any]]) -> SimpleNamespace:
    """Build a fake top-level client whose ``.chat.completions.create()`` is faked.

    :param call_chunks: Per-call chunk sequences forwarded to ``_FakeCompletions``.
    :returns: A ``SimpleNamespace`` shaped like ``AsyncDatabricksOpenAI``.
    """
    completions = _FakeCompletions(call_chunks)
    chat = SimpleNamespace(completions=completions)
    return SimpleNamespace(chat=chat)


class TestLcToOpenaiMessages:
    def test_human_message_becomes_user_role(self) -> None:
        result = _lc_to_openai_messages([HumanMessage(content="hi")])
        assert result == [{"role": "user", "content": "hi"}]

    def test_ai_message_no_tool_calls_becomes_assistant(self) -> None:
        result = _lc_to_openai_messages([AIMessage(content="hello")])
        assert result == [{"role": "assistant", "content": "hello"}]

    def test_ai_message_with_tool_calls_includes_tool_calls_key(self) -> None:
        msg = AIMessage(
            content="",
            tool_calls=[{"id": "c1", "name": "search", "args": {"q": "foo"}}],
        )
        result = _lc_to_openai_messages([msg])
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        tc = result[0]["tool_calls"][0]
        assert tc["id"] == "c1"
        assert tc["function"]["name"] == "search"
        assert json.loads(tc["function"]["arguments"]) == {"q": "foo"}

    def test_tool_message_becomes_tool_role(self) -> None:
        msg = ToolMessage(content="result", tool_call_id="c1")
        result = _lc_to_openai_messages([msg])
        assert result == [{"role": "tool", "tool_call_id": "c1", "content": "result"}]

    def test_system_message_becomes_system_role(self) -> None:
        msg = SystemMessage(content="You are helpful.")
        result = _lc_to_openai_messages([msg])
        assert result == [{"role": "system", "content": "You are helpful."}]

    def test_mixed_history_preserves_order(self) -> None:
        msgs = [HumanMessage(content="ask"), AIMessage(content="answer")]
        result = _lc_to_openai_messages(msgs)
        assert result[0] == {"role": "user", "content": "ask"}
        assert result[1] == {"role": "assistant", "content": "answer"}


class TestExecutorEventsToItems:
    def test_text_chunks_plus_turn_complete_emits_single_message(self) -> None:
        events: list[Any] = [
            TextChunk(text="Hello"),
            TextChunk(text=" world"),
            TurnComplete(response="Hello world"),
        ]
        items = _executor_events_to_items(events)
        assert len(items) == 1
        assert items[0]["type"] == "message"
        assert items[0]["role"] == "assistant"
        # Text must be the concatenation of the two chunks
        assert items[0]["content"][0]["text"] == "Hello world"

    def test_turn_complete_fallback_when_no_text_chunks(self) -> None:
        """TurnComplete.response is used when no TextChunk events preceded it."""
        events: list[Any] = [TurnComplete(response="direct text")]
        items = _executor_events_to_items(events)
        assert len(items) == 1
        assert items[0]["content"][0]["text"] == "direct text"

    def test_tool_call_produces_correct_item_sequence(self) -> None:
        events: list[Any] = [
            ToolCallRequest(name="search", args={"q": "foo"}, call_id="c1"),
            ToolCallComplete(name="search", call_id="c1", result="found it"),
            TextChunk(text="Done"),
            TurnComplete(response="Done"),
        ]
        items = _executor_events_to_items(events)
        types = [i["type"] for i in items]
        # function_call, function_call_output, final message
        assert types == ["function_call", "function_call_output", "message"]
        assert items[0]["call_id"] == "c1"
        assert items[0]["name"] == "search"
        assert items[1]["call_id"] == "c1"
        assert items[1]["output"] == "found it"
        assert items[2]["content"][0]["text"] == "Done"

    def test_text_before_tool_call_emitted_first(self) -> None:
        events: list[Any] = [
            TextChunk(text="Thinking..."),
            ToolCallRequest(name="look", args={}, call_id="cx"),
            ToolCallComplete(name="look", call_id="cx", result="data"),
            TextChunk(text="Done"),
            TurnComplete(response="Done"),
        ]
        items = _executor_events_to_items(events)
        assert items[0]["type"] == "message"
        assert items[0]["content"][0]["text"] == "Thinking..."
        assert items[1]["type"] == "function_call"
        assert items[2]["type"] == "function_call_output"
        assert items[3]["type"] == "message"
        assert items[3]["content"][0]["text"] == "Done"

    def test_executor_error_raises_runtime_error(self) -> None:
        events: list[Any] = [ExecutorError(message="boom")]
        with pytest.raises(RuntimeError, match="boom"):
            _executor_events_to_items(events)


class TestClaudeSDKPath:
    """Integration tests for compile_to_responses_agent with executor='claude-sdk'."""

    def test_non_streaming_returns_response_with_correct_text(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        non_streaming, _ = compile_to_responses_agent(
            agent, model="databricks-claude-sonnet-4-6", executor="claude-sdk"
        )
        fake_client = _make_sdk_fake_client([
            [_ns_text_chunk("hello from sdk"), _ns_finish_chunk()]
        ])

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._claude_sdk_executor._make_client",
            return_value=fake_client,
        ):
            resp = non_streaming(_user_request("hi"))

        assert isinstance(resp, ResponsesAgentResponse)
        assert len(resp.output) == 1
        d = resp.output[0].model_dump()
        assert d["type"] == "message"
        assert d["role"] == "assistant"
        # Verify the text from the fake streaming chunks reached the response
        assert d["content"][0]["text"] == "hello from sdk"

    def test_streaming_yields_item_done_then_completed(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        _, streaming = compile_to_responses_agent(
            agent, model="databricks-claude-sonnet-4-6", executor="claude-sdk"
        )
        fake_client = _make_sdk_fake_client([
            [_ns_text_chunk("streamed via sdk"), _ns_finish_chunk()]
        ])

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._claude_sdk_executor._make_client",
            return_value=fake_client,
        ):
            events = list(streaming(_user_request("go")))

        assert len(events) >= 2
        for ev in events:
            assert isinstance(ev, ResponsesAgentStreamEvent)
        event_types = [ev.type for ev in events]
        assert "response.output_item.done" in event_types
        # Terminal event must be last
        assert event_types[-1] == "response.completed"
        # The item event must carry the streamed text
        item_ev = next(ev for ev in events if ev.type == "response.output_item.done")
        item_dict = item_ev.item if isinstance(item_ev.item, dict) else item_ev.item.model_dump()
        assert item_dict["content"][0]["text"] == "streamed via sdk"

    def test_fallback_to_langgraph_when_executor_claude_sdk_but_not_llm_agent(
        self,
    ) -> None:
        """Non-LlmAgent with executor='claude-sdk' silently falls back to langgraph."""
        from apx_agent._agents import SequentialAgent

        agent = SequentialAgent(agents=[LlmAgent(tools=[])])
        non_streaming, _ = compile_to_responses_agent(
            agent, model="any", executor="claude-sdk"
        )

        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._responses_agent.compile_to_langgraph",
            return_value=_make_fake_graph("fallback answer"),
        ):
            resp = non_streaming(_user_request("hi"))

        assert isinstance(resp, ResponsesAgentResponse)
        assert resp.output[0].model_dump()["content"][0]["text"] == "fallback answer"


# ---------------------------------------------------------------------------
# Round-trip: write tool-call turn to store, read back, verify tool_calls survive
# ---------------------------------------------------------------------------


def test_conv_turn_tool_call_round_trip_preserves_tool_calls() -> None:
    """Write→store→read→convert must not lose tool call data.

    The risky invariant: _resp_item_to_new_conv_items emits FunctionCallData
    items; _conv_items_to_lc_messages coalesces them into AIMessage.tool_calls.
    A write/read asymmetry here would silently orphan the follow-on ToolMessage
    on turn 2, causing the model to see an unmatched tool result.
    """
    store = InMemoryConversationStore()
    store.create_conversation(id="trip-1")

    user_msg = {"type": "message", "role": "user", "content": "call the tool"}
    func_call = {
        "type": "function_call",
        "name": "do_lookup",
        "call_id": "call-99",
        "arguments": '{"q": "hello"}',
    }
    func_output = {
        "type": "function_call_output",
        "call_id": "call-99",
        "output": "result-data",
    }
    assistant_reply = {"type": "message", "role": "assistant", "content": "all done"}

    _persist_conv_turn(
        store,
        "trip-1",
        input_items=[user_msg],
        output_items=[func_call, func_output, assistant_reply],
        model="any",
        response_id="r-trip-1",
    )

    items = store.list_items("trip-1").data
    assert len(items) == 4, f"Expected 4 items, got {len(items)}: {[i.type for i in items]}"

    lc = _conv_items_to_lc_messages(items)

    tool_call_msgs = [m for m in lc if isinstance(m, AIMessage) and m.tool_calls]
    assert tool_call_msgs, "No AIMessage with tool_calls found after round-trip"
    tc = tool_call_msgs[0].tool_calls[0]
    assert tc["name"] == "do_lookup"
    assert tc["id"] == "call-99"
    assert tc["args"] == {"q": "hello"}

    tool_msgs = [m for m in lc if isinstance(m, ToolMessage)]
    assert tool_msgs, "No ToolMessage found after round-trip"
    assert tool_msgs[0].tool_call_id == "call-99"
    assert tool_msgs[0].content == "result-data"


def test_resolve_ws_rejects_tokenless_request_in_app(monkeypatch):
    """G2 wiring: the responses auth chokepoint fails closed in the Apps runtime
    when no OBO token is present (no app-SP fallback unless opted in)."""
    import pytest

    from apx_agent._obo import ApxIdentityError
    from apx_agent._responses_agent import _resolve_ws_and_headers_for_request

    monkeypatch.setenv("DATABRICKS_APP_NAME", "my-app")
    monkeypatch.delenv("APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK", raising=False)
    with pytest.raises(ApxIdentityError):
        _resolve_ws_and_headers_for_request(custom_inputs=None)


def test_token_without_user_id_still_builds_forwarding_headers(monkeypatch):
    """#467: a user_token but no user_id must still yield headers carrying the
    token, else the A2A sub-agent hop launders the caller's privilege away."""
    from apx_agent._responses_agent import _resolve_ws_and_headers_for_request

    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
    monkeypatch.delenv("DATABRICKS_APP_URL", raising=False)
    with patch("apx_agent._defaults._make_workspace_client", return_value=MagicMock()):
        headers = _resolve_ws_and_headers_for_request(
            custom_inputs={"user_token": "tok-xyz"}
        ).headers
    assert headers is not None
    assert headers.token is not None
    assert headers.token.get_secret_value() == "tok-xyz"
