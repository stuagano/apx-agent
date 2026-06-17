"""In-process trace ring buffer (``apx_agent._trace_store``).

The dev-UI Trace detail (``/_apx/traces/{id}``) hangs on FEVM/private-link
because ``mlflow.get_trace`` falls through to downloading span artifacts from
blocked blob storage. The ring buffer snapshots each trace at root-span-end
(reading the still-live trace from MLflow's in-memory manager via an OTel
SpanProcessor — no tracking-store / blob round-trip) so the route can serve
recent traces from memory instead.
"""

from __future__ import annotations

import pytest


def test_trace_store_put_get_and_bound():
    from apx_agent import _trace_store as ts
    ts.reset()
    ts.put("tr-1", [{"name": "a"}])
    assert ts.get("tr-1") == [{"name": "a"}]
    assert ts.get("nope") is None
    for i in range(ts.MAX_TRACES + 10):
        ts.put(f"x-{i}", [{"name": str(i)}])
    assert len(ts._STORE) <= ts.MAX_TRACES          # bounded (oldest evicted)
    assert ts.get("tr-1") is None                    # tr-1 evicted


# ---------------------------------------------------------------------------
# GATING TEST (the crux): a real agent run with autolog + a real LangGraph must
# leave the ring buffer holding the trace the dev UI shows — WITH the tool span.
#
# Mechanism under test: at root-span-end an OTel SpanProcessor reads the live,
# complete trace from MLflow's InMemoryTraceManager (NOT mlflow.get_trace, which
# this MLflow routes straight to the tracking store / blocked blob). We drive
# the REAL registration site (install_capture_processor_at_startup, as the app
# lifespan does) — no manual add_span_processor — so a green here proves the
# production path populates the buffer.
#
# NOTE / known limitation: a local file:// tracking backend lets get_trace
# succeed regardless of the in-memory path, so this test proves "spans are
# complete and capturable at root-span-end" — it does NOT, and structurally
# cannot, reproduce the FEVM blob-read hang. It is not evidence the buffer was
# served WITHOUT a blob read on private-link.
# ---------------------------------------------------------------------------

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("mlflow")
pytest.importorskip("mlflow.types.responses")


def _fake_tool_then_answer():
    """A BaseChatModel that emits one tool call, then a final answer once the
    tool result is present. Lets a REAL LangGraph run end-to-end (and autolog
    emit a real TOOL span) without any network/LLM."""
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class _FakeToolThenAnswer(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "fake-tool-then-answer"

        def bind_tools(self, tools, **kwargs):  # type: ignore[override]
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
            has_tool_result = any(getattr(m, "type", "") == "tool" for m in messages)
            if has_tool_result:
                msg = AIMessage(content="the time is 12:00")
            else:
                msg = AIMessage(
                    content="",
                    tool_calls=[{"name": "get_time", "args": {}, "id": "call_1"}],
                )
            return ChatResult(generations=[ChatGeneration(message=msg)])

    return _FakeToolThenAnswer()


def test_capture_after_real_agent_run(tmp_path):
    from unittest.mock import MagicMock, patch

    import mlflow

    from apx_agent import LlmAgent, compile_to_responses_agent
    from apx_agent import _trace_store as ts
    from mlflow.types.responses import ResponsesAgentRequest

    # Real MLflow tracing against a LOCAL file backend (never the blocked
    # Databricks blob backend).
    mlflow.set_tracking_uri(f"file://{tmp_path}")
    exp_id = mlflow.create_experiment(f"apx-capture-{tmp_path.name}")
    mlflow.set_experiment(experiment_id=exp_id)
    mlflow.langchain.autolog()

    # Drive the REAL registration site the lifespan uses (warm-up + install).
    assert ts.install_capture_processor_at_startup() is True

    def get_time() -> str:
        """Return the current time."""
        return "12:00"

    agent = LlmAgent(tools=[get_time], instructions="You tell the time.")
    non_streaming, _ = compile_to_responses_agent(agent, model="fake-endpoint")

    ts.reset()
    with patch("apx_agent._llm.get_llm", return_value=_fake_tool_then_answer()), patch(
        "apx_agent._defaults._make_workspace_client", return_value=MagicMock(name="ws")
    ):
        non_streaming(
            ResponsesAgentRequest(input=[{"role": "user", "content": "what time is it"}])
        )

    # The trace the dev UI's finalizeTrace fetches is the last active trace.
    trace_id = mlflow.get_last_active_trace_id()
    assert trace_id, "no active trace id after run"

    spans = ts.get(trace_id)
    assert spans is not None, (
        f"buffer is empty for {trace_id} — the capture point is wrong "
        f"(processor not firing on root-span-end, or trace already evicted "
        f"from the in-memory manager)"
    )
    names = [s["name"] for s in spans]
    assert "get_time" in names, (
        f"captured trace is missing the TOOL span; got spans={names}"
    )
    # The captured entry must be plain serialized dicts (materialized, not lazy).
    tool_span = next(s for s in spans if s["name"] == "get_time")
    assert tool_span["span_type"] == "TOOL"
    assert isinstance(tool_span["events"], list)


def _setup_local_tracing(tmp_path):
    import mlflow

    mlflow.set_tracking_uri(f"file://{tmp_path}")
    exp_id = mlflow.create_experiment(f"apx-capture-{tmp_path.name}")
    mlflow.set_experiment(experiment_id=exp_id)
    mlflow.langchain.autolog()


def test_capture_after_real_streaming_responses_run(tmp_path):
    """The dev UI streams (x-return-trace-id on the stream), so the STREAMING
    path is the production capture path. The root safe_span closes when the
    generator is exhausted — verify capture fires there too, with the tool
    span, while the trace is still in the in-memory manager."""
    from unittest.mock import MagicMock, patch

    import mlflow

    from apx_agent import LlmAgent, compile_to_responses_agent
    from apx_agent import _trace_store as ts
    from mlflow.types.responses import ResponsesAgentRequest

    _setup_local_tracing(tmp_path)
    assert ts.install_capture_processor_at_startup() is True

    def get_time() -> str:
        """Return the current time."""
        return "12:00"

    agent = LlmAgent(tools=[get_time], instructions="You tell the time.")
    _, streaming = compile_to_responses_agent(agent, model="fake-endpoint")

    ts.reset()
    with patch("apx_agent._llm.get_llm", return_value=_fake_tool_then_answer()), patch(
        "apx_agent._defaults._make_workspace_client", return_value=MagicMock(name="ws")
    ):
        # Exhaust the generator — capture only runs once the stream completes.
        list(streaming(ResponsesAgentRequest(input=[{"role": "user", "content": "time?"}])))

    trace_id = mlflow.get_last_active_trace_id()
    spans = ts.get(trace_id)
    assert spans is not None, f"streaming run left buffer empty for {trace_id}"
    assert "get_time" in [s["name"] for s in spans]


def test_capture_after_real_streaming_chat_run(tmp_path):
    """Same as above for the ChatAgent predict_stream path (dev loop / apx-agent run)."""
    from unittest.mock import MagicMock, patch

    import mlflow

    from apx_agent import LlmAgent, chat_agent_for
    from apx_agent import _trace_store as ts
    from mlflow.types.agent import ChatAgentMessage

    _setup_local_tracing(tmp_path)
    assert ts.install_capture_processor_at_startup() is True

    def get_time() -> str:
        """Return the current time."""
        return "12:00"

    agent = LlmAgent(tools=[get_time], instructions="You tell the time.")
    wrapped = chat_agent_for(agent, model="fake-endpoint")

    ts.reset()
    with patch("apx_agent._llm.get_llm", return_value=_fake_tool_then_answer()), patch(
        "apx_agent._defaults._make_workspace_client", return_value=MagicMock(name="ws")
    ):
        list(wrapped.predict_stream(messages=[ChatAgentMessage(role="user", content="time?")]))

    trace_id = mlflow.get_last_active_trace_id()
    spans = ts.get(trace_id)
    assert spans is not None, f"chat streaming run left buffer empty for {trace_id}"
    assert "get_time" in [s["name"] for s in spans]
