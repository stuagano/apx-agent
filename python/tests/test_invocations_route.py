"""Tests for _invocations.py — the MLflow ChatAgent ``/invocations`` route.

Verifies:

  1. ``create_app`` mounts POST ``/invocations`` when optional deps are present.
  2. Non-streaming requests return a ``ChatAgentResponse``-shaped JSON body.
  3. Streaming requests return SSE chunks of ``ChatAgentChunk`` JSON.
  4. **The Databricks Apps OBO header bridge works**: when the request carries
     ``X-Forwarded-Access-Token``, it lands in ``custom_inputs["user_token"]``
     of the underlying ChatAgent.predict call. This is the enterprise-critical
     assertion — user-scope auth survives the route layer.
  5. Existing legacy endpoints (``/health``, ``/.well-known/agent.json``) still
     work alongside the new route.
  6. **Dual-shape acceptance (#438)**: a Responses-shape body (``input``)
     executes the agent and gets a Responses-shape reply; a body with neither
     ``messages`` nor ``input`` is a 422 naming both accepted shapes.

Skips if optional extras are missing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("mlflow")

from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

from apx_agent import (  # noqa: E402
    LlmAgent,
    AgentConfig,
    create_app,
)


# ---------------------------------------------------------------------------
# Tool + agent fixtures
# ---------------------------------------------------------------------------


def _trivial_tool(query: str) -> str:
    """Return the query, echoing back."""
    return f"got: {query}"


@pytest.fixture
def app_and_chat_agent():
    """Build a create_app() instance and capture the ChatAgent it constructed
    so tests can mock its predict / predict_stream methods."""
    agent = LlmAgent(tools=[_trivial_tool])
    config = AgentConfig(name="test-agent", model="databricks-claude-sonnet-4-6")

    captured: dict[str, Any] = {}

    # chat_agent_for is imported lazily inside mount_invocations_route, so
    # we patch the *source* module (_chat_agent), not _invocations.
    from apx_agent import _chat_agent as _ca_module

    original_factory = _ca_module.chat_agent_for

    def _spy_factory(agent_arg, *, model, conversation_store=None, agent_id=None, checkpointer=None):
        ca = original_factory(agent_arg, model=model)
        # create_app builds a chat_agent per protocol route (now /invocations AND
        # the A2A surface). The /invocations mount is first, so capture that one —
        # the instance these tests stub predict/predict_stream on.
        captured.setdefault("chat_agent", ca)
        ca.predict = MagicMock(name="mock_predict")
        ca.predict_stream = MagicMock(name="mock_predict_stream")
        return ca

    # _wiring imports _make_workspace_client at module load — patch the
    # rebinding there, not the source. (Same lazy-import gotcha as above.)
    with patch.object(_ca_module, "chat_agent_for", side_effect=_spy_factory), patch(
        "apx_agent._wiring._make_workspace_client"
    ) as mock_ws_factory:
        mock_ws_factory.return_value = MagicMock(name="sp_ws")
        app = create_app(agent, config=config)
        # TestClient triggers lifespan, which is when the route mounts.
        with TestClient(app) as client:
            yield client, captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRouteIsMounted:
    def test_invocations_route_exists_after_create_app(
        self, app_and_chat_agent
    ) -> None:
        client, _ = app_and_chat_agent
        # An empty body should be parseable; predict will be called with []
        from mlflow.types.agent import ChatAgentResponse

        ca = client.app.state.agent_context
        # The route is present — try invoking it with a stub response.
        # We patched predict on the chat_agent at construction; configure it now.
        captured_chat_agent = client.app  # placeholder; real one in fixture

        # Find the chat_agent stash from the fixture's captured dict
        # (passed via TestClient closure — we can't access it here, so we
        # just check the route is registered.)
        route_paths = {r.path for r in client.app.routes}
        assert "/invocations" in route_paths

    def test_legacy_routes_still_work(self, app_and_chat_agent) -> None:
        client, _ = app_and_chat_agent
        # /health and /.well-known/agent.json still respond.
        assert client.get("/health").status_code == 200
        assert client.get("/.well-known/agent.json").status_code == 200


class TestNonStreamingProtocol:
    def test_returns_chat_agent_response_json(self, app_and_chat_agent) -> None:
        client, captured = app_and_chat_agent
        from mlflow.types.agent import ChatAgentMessage, ChatAgentResponse

        captured["chat_agent"].predict.return_value = ChatAgentResponse(
            messages=[ChatAgentMessage(role="assistant", content="hi back", id="m1")]
        )

        resp = client.post(
            "/invocations",
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "messages" in body
        assert body["messages"][0]["role"] == "assistant"
        assert body["messages"][0]["content"] == "hi back"


class TestStreamingProtocol:
    def test_returns_sse_with_chunk_per_event(self, app_and_chat_agent) -> None:
        client, captured = app_and_chat_agent
        from mlflow.types.agent import ChatAgentChunk, ChatAgentMessage

        captured["chat_agent"].predict_stream.return_value = iter([
            ChatAgentChunk(
                delta=ChatAgentMessage(role="assistant", content="hel", id="m1")
            ),
            ChatAgentChunk(
                delta=ChatAgentMessage(role="assistant", content="lo", id="m1")
            ),
        ])

        resp = client.post(
            "/invocations",
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        body_text = resp.text
        # Two SSE data events, separated by blank line.
        assert body_text.count("data: ") == 2
        assert '"content":"hel"' in body_text
        assert '"content":"lo"' in body_text


class TestObOHeaderBridge:
    """THE enterprise-critical test: X-Forwarded-Access-Token → custom_inputs.user_token."""

    def test_obo_header_lands_in_custom_inputs(self, app_and_chat_agent) -> None:
        client, captured = app_and_chat_agent
        from mlflow.types.agent import ChatAgentMessage, ChatAgentResponse

        captured["chat_agent"].predict.return_value = ChatAgentResponse(messages=[])

        client.post(
            "/invocations",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Forwarded-Access-Token": "secret-obo-tok"},
        )

        # The mock was called with custom_inputs["user_token"] populated from
        # the X-Forwarded-Access-Token header.
        call_args = captured["chat_agent"].predict.call_args
        custom_inputs = call_args.kwargs["custom_inputs"]
        assert custom_inputs is not None
        assert custom_inputs["user_token"] == "secret-obo-tok"

    def test_caller_provided_user_token_wins_over_header(
        self, app_and_chat_agent
    ) -> None:
        """If the body already has user_token, the header doesn't clobber it."""
        client, captured = app_and_chat_agent
        from mlflow.types.agent import ChatAgentResponse

        captured["chat_agent"].predict.return_value = ChatAgentResponse(messages=[])

        client.post(
            "/invocations",
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "custom_inputs": {"user_token": "explicit-tok"},
            },
            headers={"X-Forwarded-Access-Token": "header-tok"},
        )

        call_args = captured["chat_agent"].predict.call_args
        assert call_args.kwargs["custom_inputs"]["user_token"] == "explicit-tok"

    def test_no_header_no_user_token(self, app_and_chat_agent) -> None:
        """Without the OBO header, custom_inputs.user_token is absent —
        downstream falls back to default SP/CLI auth chain."""
        client, captured = app_and_chat_agent
        from mlflow.types.agent import ChatAgentResponse

        captured["chat_agent"].predict.return_value = ChatAgentResponse(messages=[])

        client.post(
            "/invocations",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

        call_args = captured["chat_agent"].predict.call_args
        custom_inputs = call_args.kwargs["custom_inputs"]
        # custom_inputs may be None (no body custom_inputs, no header) — fine.
        assert custom_inputs is None or "user_token" not in custom_inputs


class TestInputShapeOnInvocations:
    """#438: the Responses shape a RemoteDatabricksAgent posts must actually
    execute the agent — not be silently accepted as an empty conversation."""

    def test_input_shape_executes_agent_and_returns_responses_reply(
        self, app_and_chat_agent
    ) -> None:
        client, captured = app_and_chat_agent
        from mlflow.types.agent import ChatAgentMessage, ChatAgentResponse

        captured["chat_agent"].predict.return_value = ChatAgentResponse(
            messages=[
                ChatAgentMessage(role="assistant", content="the answer", id="m1")
            ]
        )

        resp = client.post(
            "/invocations",
            json={"input": [{"role": "user", "content": "what is the answer?"}]},
        )

        assert resp.status_code == 200
        # The agent REALLY saw the user turn — no more empty-conversation
        # "I'm ready to help" non-answer.
        sent = captured["chat_agent"].predict.call_args.args[0]
        assert [m.content for m in sent] == ["what is the answer?"]
        # And the reply is the shape the Responses-speaking poster parses.
        body = resp.json()
        assert body["output"][0]["content"][0]["text"] == "the answer"

    def test_input_content_parts_are_flattened_to_text(
        self, app_and_chat_agent
    ) -> None:
        client, captured = app_and_chat_agent
        from mlflow.types.agent import ChatAgentResponse

        captured["chat_agent"].predict.return_value = ChatAgentResponse(messages=[])

        client.post(
            "/invocations",
            json={
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "part one"}],
                    }
                ]
            },
        )

        sent = captured["chat_agent"].predict.call_args.args[0]
        assert [m.content for m in sent] == ["part one"]

    def test_bare_string_input_is_one_user_turn(self, app_and_chat_agent) -> None:
        client, captured = app_and_chat_agent
        from mlflow.types.agent import ChatAgentResponse

        captured["chat_agent"].predict.return_value = ChatAgentResponse(messages=[])

        client.post("/invocations", json={"input": "hello there"})

        sent = captured["chat_agent"].predict.call_args.args[0]
        assert [(m.role, m.content) for m in sent] == [("user", "hello there")]

    def test_neither_messages_nor_input_is_422_naming_both_shapes(
        self, app_and_chat_agent
    ) -> None:
        client, captured = app_and_chat_agent

        resp = client.post("/invocations", json={"prompt": "hi"})

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "messages" in detail
        assert "input" in detail
        # And the agent was never run on a fabricated empty conversation.
        assert not captured["chat_agent"].predict.called


class TestErrorHandling:
    def test_malformed_json_returns_400(self, app_and_chat_agent) -> None:
        client, _ = app_and_chat_agent
        resp = client.post(
            "/invocations",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    def test_invalid_message_shape_returns_400(self, app_and_chat_agent) -> None:
        client, _ = app_and_chat_agent
        resp = client.post(
            "/invocations",
            json={"messages": [{"role": "user"}]},  # missing required 'content'
        )
        # ChatAgentMessage requires content — either coerce or 400. We chose 400.
        assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# /responses route (ResponsesAgent protocol)
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_responses():
    """create_app() fixture with compile_to_responses_agent mocked out.

    We mock the compile step so the test doesn't need a live LangGraph compile
    and real MLflow ResponsesAgent types — just the route plumbing.
    """
    from unittest.mock import MagicMock, patch

    agent = LlmAgent(tools=[_trivial_tool])
    config = AgentConfig(name="test-agent", model="databricks-claude-sonnet-4-6")

    # A fake ResponsesAgentResponse-like object with model_dump().
    fake_response = MagicMock()
    fake_response.model_dump.return_value = {
        "output": [{"role": "assistant", "content": "pong"}],
    }
    fake_invoke = MagicMock(return_value=fake_response)

    # A fake streaming generator yielding two events.
    fake_event = MagicMock()
    fake_event.model_dump_json.return_value = '{"type":"response.output_item.done"}'
    fake_stream = MagicMock(return_value=iter([fake_event, fake_event]))

    captured: dict[str, Any] = {}

    def _fake_compile(agent_arg, *, model, conversation_store=None, executor="langgraph", agent_id=None, checkpointer=None):
        captured["invoke"] = fake_invoke
        captured["stream"] = fake_stream
        return fake_invoke, fake_stream

    with patch(
        "apx_agent._responses_agent.compile_to_responses_agent",
        side_effect=_fake_compile,
    ), patch("apx_agent._wiring._make_workspace_client") as mock_ws_factory:
        mock_ws_factory.return_value = MagicMock(name="sp_ws")
        # Also patch chat_agent_for to prevent it from compiling real LangGraph
        from apx_agent import _chat_agent as _ca_module

        with patch.object(_ca_module, "chat_agent_for", side_effect=lambda a, **kw: MagicMock()):
            app = create_app(agent, config=config)
            with TestClient(app) as client:
                yield client, captured


class TestResponsesRoute:
    def test_responses_route_mounted(self, app_with_responses) -> None:
        client, _ = app_with_responses
        route_paths = {r.path for r in client.app.routes}
        assert "/responses" in route_paths

    def test_both_routes_mounted(self, app_with_responses) -> None:
        client, _ = app_with_responses
        route_paths = {r.path for r in client.app.routes}
        assert "/invocations" in route_paths
        assert "/responses" in route_paths

    def test_non_streaming_returns_response_json(self, app_with_responses) -> None:
        client, captured = app_with_responses
        resp = client.post(
            "/responses",
            json={"input": [{"role": "user", "content": "ping"}], "stream": False},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "output" in body
        assert body["output"][0]["role"] == "assistant"

    def test_streaming_returns_sse(self, app_with_responses) -> None:
        client, captured = app_with_responses
        resp = client.post(
            "/responses",
            json={"input": [{"role": "user", "content": "ping"}], "stream": True},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.text.count("data: ") == 2

    def test_obo_header_forwarded_to_custom_inputs(self, app_with_responses) -> None:
        client, captured = app_with_responses
        resp = client.post(
            "/responses",
            json={"input": [{"role": "user", "content": "hi"}]},
            headers={"X-Forwarded-Access-Token": "obo-tok"},
        )
        assert resp.status_code == 200
        assert captured["invoke"].called
        # The OBO token must land in custom_inputs on the ResponsesAgentRequest
        # passed to invoke — this is the enterprise-critical contract.
        req_arg = captured["invoke"].call_args[0][0]
        assert req_arg.custom_inputs["user_token"] == "obo-tok"

# ---------------------------------------------------------------------------
# Cross-agent trace correlation (#443) — inbound traceparent / x-apx-caller
# ---------------------------------------------------------------------------


TP = f"00-{'ab' * 16}-{'cd' * 8}-01"


class TestCrossAgentCorrelation:
    """Inbound traceparent / x-apx-caller headers are stamped on the served
    trace (span attrs + trace tags), so B's trace joins A's on
    apx.outbound.trace_id. Absent headers → zero behavior change (#443)."""

    def _stub_predict(self, captured) -> None:
        from mlflow.types.agent import ChatAgentMessage, ChatAgentResponse

        captured["chat_agent"].predict.return_value = ChatAgentResponse(
            messages=[ChatAgentMessage(role="assistant", content="ok", id="m1")]
        )

    def test_invocations_stamps_caller_correlation_tags(
        self, app_and_chat_agent
    ) -> None:
        client, captured = app_and_chat_agent
        self._stub_predict(captured)
        with patch("apx_agent._audit.set_trace_tags") as tags_mock:
            resp = client.post(
                "/invocations",
                json={"messages": [{"role": "user", "content": "hi"}]},
                headers={"traceparent": TP, "x-apx-caller": "orchestrator"},
            )
        assert resp.status_code == 200
        tags_mock.assert_called_once_with(
            {
                "apx.traceparent": TP,
                "apx.outbound.trace_id": "ab" * 16,
                "apx.caller": "orchestrator",
            }
        )

    def test_invocations_without_headers_stamps_nothing(
        self, app_and_chat_agent
    ) -> None:
        client, captured = app_and_chat_agent
        self._stub_predict(captured)
        with patch("apx_agent._audit.set_trace_tags") as tags_mock:
            resp = client.post(
                "/invocations",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
        assert resp.status_code == 200
        tags_mock.assert_not_called()

    def test_responses_stamps_caller_correlation_tags(
        self, app_with_responses
    ) -> None:
        client, _ = app_with_responses
        with patch("apx_agent._audit.set_trace_tags") as tags_mock:
            resp = client.post(
                "/responses",
                json={"input": [{"role": "user", "content": "hi"}]},
                headers={"traceparent": TP, "x-apx-caller": "orchestrator"},
            )
        assert resp.status_code == 200
        tags_mock.assert_called_once_with(
            {
                "apx.traceparent": TP,
                "apx.outbound.trace_id": "ab" * 16,
                "apx.caller": "orchestrator",
            }
        )

    def test_responses_without_headers_stamps_nothing(
        self, app_with_responses
    ) -> None:
        client, _ = app_with_responses
        with patch("apx_agent._audit.set_trace_tags") as tags_mock:
            resp = client.post(
                "/responses",
                json={"input": [{"role": "user", "content": "hi"}]},
            )
        assert resp.status_code == 200
        tags_mock.assert_not_called()
