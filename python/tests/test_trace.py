"""Tests for _trace.py and the /_apx/traces dev UI routes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from apx_agent import (
    Agent,
    AgentConfig,
    AgentContext,
    Message,
    _trace,
    setup_agent,
)
from apx_agent._agents import BaseAgent
from apx_agent._dev import build_dev_ui_router
from apx_agent._models import A2ASkill, AgentCard
from apx_agent._runner import run_via_sdk, stream_via_sdk


@pytest.fixture(autouse=True)
def _reset_trace_buffer():
    """Each test gets a fresh ring buffer."""
    _trace._buffer.clear()
    yield
    _trace._buffer.clear()


class TestTraceModel:
    def test_create_and_end_trace(self):
        t = _trace.create_trace("agent-x")
        assert t.agent_name == "agent-x"
        assert t.status == "in_progress"
        assert t.id.startswith("tr-")

        s = _trace.add_span(t, type="request", name="POST /responses", input={"msg": "hi"})
        assert s.start_time > 0
        _trace.end_span(s, output="ok")
        assert s.duration_ms is not None

        _trace.end_trace(t)
        assert t.status == "completed"
        assert t.duration_ms is not None

    def test_ring_buffer_caps_at_max(self):
        for i in range(_trace.MAX_TRACES + 5):
            t = _trace.create_trace(f"agent-{i}")
            _trace.end_trace(t)
        assert len(_trace._buffer) == _trace.MAX_TRACES

    def test_get_trace_by_id(self):
        t = _trace.create_trace("agent-y")
        _trace.end_trace(t)
        assert _trace.get_trace(t.id) is t
        assert _trace.get_trace("nonexistent") is None

    def test_get_traces_newest_first(self):
        ids = []
        for i in range(3):
            t = _trace.create_trace(f"agent-{i}")
            _trace.end_trace(t)
            ids.append(t.id)
        listed = _trace.get_traces()
        assert [t.id for t in listed] == list(reversed(ids))

    def test_truncate(self):
        assert _trace.truncate("abc", 10) == "abc"
        assert _trace.truncate("a" * 50, 10) == "a" * 10 + "..."
        assert _trace.truncate({"k": "v"}, 100) == '{"k": "v"}'


class TestTraceRoutes:
    @pytest.mark.asyncio
    async def test_traces_list_empty(self):
        app = FastAPI()
        app.include_router(build_dev_ui_router())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/_apx/traces")
        assert r.status_code == 200
        assert "Agent Traces" in r.text
        assert "No traces yet" in r.text

    @pytest.mark.asyncio
    async def test_traces_list_with_traces(self):
        t = _trace.create_trace("test-agent")
        _trace.add_span(t, type="request", name="POST /responses", input={"q": "hello"})
        _trace.end_trace(t)

        app = FastAPI()
        app.include_router(build_dev_ui_router())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/_apx/traces")
        assert r.status_code == 200
        assert "test-agent" in r.text
        assert t.id in r.text
        assert "completed" in r.text

    @pytest.mark.asyncio
    async def test_trace_detail_renders_all_span_types(self):
        t = _trace.create_trace("multi-span-agent")
        for span_type in ("request", "llm", "tool", "agent_call", "response"):
            s = _trace.add_span(t, type=span_type, name=f"{span_type}-name", input={"a": 1}, output="ok")
            _trace.end_span(s)
        _trace.end_trace(t)

        app = FastAPI()
        app.include_router(build_dev_ui_router())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get(f"/_apx/traces/{t.id}")
        assert r.status_code == 200
        assert "multi-span-agent" in r.text
        assert "Caller" in r.text
        assert "Called tool" in r.text
        assert "Called agent" in r.text
        assert "Agent responded" in r.text

    @pytest.mark.asyncio
    async def test_trace_detail_404(self):
        app = FastAPI()
        app.include_router(build_dev_ui_router())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/_apx/traces/tr-does-not-exist")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_responses_call_creates_trace(self):
        """Hit POST /responses and verify a trace shows up in the buffer + UI."""

        class StubAgent(BaseAgent):
            async def run(self, messages: list[Message], request: Request) -> str:
                # Simulate a tool span being recorded mid-run
                trace = getattr(request.state, "trace", None)
                if trace is not None:
                    span = _trace.add_span(
                        trace, type="tool", name="echo", input={"q": messages[-1].content}
                    )
                    _trace.end_span(span, output="echoed")
                return f"reply: {messages[-1].content}"

            async def stream(
                self, messages: list[Message], request: Request
            ) -> AsyncGenerator[str, None]:
                yield f"reply: {messages[-1].content}"

        app = FastAPI()
        agent = StubAgent()
        config = AgentConfig(name="stub-agent", description="Stub for tracing")
        await setup_agent(app, agent, config)
        app.include_router(build_dev_ui_router())

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/responses",
                json={"input": [{"role": "user", "content": "hello"}]},
            )
            assert resp.status_code == 200

            traces = _trace.get_traces()
            assert len(traces) == 1
            t = traces[0]
            assert t.agent_name == "stub-agent"
            assert t.status == "completed"
            span_types = [s.type for s in t.spans]
            assert "request" in span_types
            assert "tool" in span_types
            assert "response" in span_types

            list_resp = await ac.get("/_apx/traces")
            assert list_resp.status_code == 200
            assert t.id in list_resp.text
            assert "stub-agent" in list_resp.text

            detail_resp = await ac.get(f"/_apx/traces/{t.id}")
            assert detail_resp.status_code == 200
            assert "Called tool" in detail_resp.text
            assert "echo" in detail_resp.text

    @pytest.mark.asyncio
    async def test_error_span_rendered(self):
        t = _trace.create_trace("error-agent")
        s = _trace.add_span(t, type="error", name="ValueError", output="bad input")
        _trace.end_span(s)
        _trace.end_trace(t, status="error")

        app = FastAPI()
        app.include_router(build_dev_ui_router())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get(f"/_apx/traces/{t.id}")
        assert r.status_code == 200
        assert "Error" in r.text
        assert "bad input" in r.text


def _make_runner_request() -> MagicMock:
    """Mock Request with everything run_via_sdk/stream_via_sdk reach for."""
    req = MagicMock()
    req.headers = {}
    req.state = SimpleNamespace()

    config = AgentConfig(name="runner-agent", model="claude-sonnet-4-5")
    card = AgentCard(name="runner-agent", description="", skills=[])
    ctx = AgentContext(config=config, tools=[], card=card, agent=Agent(tools=[]))
    req.app.state.agent_context = ctx
    return req


class TestRunnerSpans:
    """Exercise the LLM-span code path in _runner.py with Runner mocked."""

    @pytest.mark.asyncio
    async def test_run_via_sdk_emits_llm_span(self):
        request = _make_runner_request()
        trace = _trace.create_trace(request.app.state.agent_context.config.name)
        request.state.trace = trace

        run_result = SimpleNamespace(final_output="42", new_items=[])
        with patch("agents.Runner.run", new=AsyncMock(return_value=run_result)), \
             patch("databricks_openai.AsyncDatabricksOpenAI", return_value=MagicMock()), \
             patch("agents.set_default_openai_client"), \
             patch("agents.set_default_openai_api"):
            text = await run_via_sdk(
                [Message(role="user", content="what is 6*7?")],
                request,
            )

        assert text == "42"
        llm_spans = [s for s in trace.spans if s.type == "llm"]
        assert len(llm_spans) == 1
        assert llm_spans[0].name == "claude-sonnet-4-5"
        assert llm_spans[0].duration_ms is not None
        assert llm_spans[0].metadata.get("streaming") is False

    @pytest.mark.asyncio
    async def test_run_via_sdk_emits_error_metadata_on_exception(self):
        request = _make_runner_request()
        trace = _trace.create_trace(request.app.state.agent_context.config.name)
        request.state.trace = trace

        with patch("agents.Runner.run", new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("databricks_openai.AsyncDatabricksOpenAI", return_value=MagicMock()), \
             patch("agents.set_default_openai_client"), \
             patch("agents.set_default_openai_api"):
            with pytest.raises(RuntimeError, match="boom"):
                await run_via_sdk(
                    [Message(role="user", content="hi")],
                    request,
                )

        llm_spans = [s for s in trace.spans if s.type == "llm"]
        assert len(llm_spans) == 1
        assert llm_spans[0].metadata.get("error") is True
        assert llm_spans[0].duration_ms is not None

    @pytest.mark.asyncio
    async def test_stream_via_sdk_emits_llm_span(self):
        request = _make_runner_request()
        trace = _trace.create_trace(request.app.state.agent_context.config.name)
        request.state.trace = trace

        async def fake_events():
            yield SimpleNamespace(data=SimpleNamespace(delta="hello "), type="raw")
            yield SimpleNamespace(data=SimpleNamespace(delta="world"), type="raw")

        streamed_result = MagicMock()
        streamed_result.stream_events = fake_events

        with patch("agents.Runner.run_streamed", return_value=streamed_result), \
             patch("databricks_openai.AsyncDatabricksOpenAI", return_value=MagicMock()), \
             patch("agents.set_default_openai_client"), \
             patch("agents.set_default_openai_api"):
            chunks: list[str] = []
            async for c in stream_via_sdk(
                [Message(role="user", content="stream me")],
                request,
            ):
                chunks.append(c)

        assert "".join(chunks) == "hello world"
        llm_spans = [s for s in trace.spans if s.type == "llm"]
        assert len(llm_spans) == 1
        assert llm_spans[0].metadata.get("streaming") is True
        assert llm_spans[0].duration_ms is not None
        assert "hello world" in str(llm_spans[0].output)

    @pytest.mark.asyncio
    async def test_runner_works_when_no_trace_in_state(self):
        """Runner must not blow up when called outside a traced request."""
        request = _make_runner_request()
        # No request.state.trace assigned

        run_result = SimpleNamespace(final_output="ok", new_items=[])
        with patch("agents.Runner.run", new=AsyncMock(return_value=run_result)), \
             patch("databricks_openai.AsyncDatabricksOpenAI", return_value=MagicMock()), \
             patch("agents.set_default_openai_client"), \
             patch("agents.set_default_openai_api"):
            text = await run_via_sdk(
                [Message(role="user", content="hi")],
                request,
            )
        assert text == "ok"


class TestLiveSpanStream:
    """SSE must surface span.start events live during slow agent steps."""

    @pytest.mark.asyncio
    async def test_span_start_emits_before_text_delta_during_slow_llm(self):
        """When the agent's stream pauses (e.g. waiting on the LLM),
        span.start events must reach the client before any
        output_text.delta. Proves the queue-based merge actually
        delivers live span events instead of degrading to
        between-chunk polling.
        """

        class SlowAgent(BaseAgent):
            async def run(self, messages: list[Message], request: Request) -> str:
                return ""

            async def stream(
                self, messages: list[Message], request: Request
            ) -> AsyncGenerator[str, None]:
                trace = getattr(request.state, "trace", None)
                # Simulate the agent calling the LLM: a span starts, the
                # model takes time, then text arrives.
                if trace is not None:
                    span = _trace.add_span(trace, type="llm", name="claude-fake")
                    await asyncio.sleep(0.05)
                    _trace.end_span(span, output="hi")
                yield "hi"

        app = FastAPI()
        agent = SlowAgent()
        config = AgentConfig(name="slow-agent", description="Slow stub")
        await setup_agent(app, agent, config)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream(
                "POST",
                "/responses",
                json={"input": [{"role": "user", "content": "hi"}], "stream": True},
            ) as resp:
                assert resp.status_code == 200
                event_order: list[tuple[str, str]] = []
                event_type = ""
                async for line in resp.aiter_lines():
                    if line.startswith("event: "):
                        event_type = line[7:].strip()
                    elif line.startswith("data: ") and event_type:
                        try:
                            payload = json.loads(line[6:])
                        except json.JSONDecodeError:
                            payload = None
                        kind = (
                            payload.get("type")
                            if isinstance(payload, dict)
                            else None
                        )
                        event_order.append((event_type, kind or ""))
                        event_type = ""

        relevant = [
            e for e in event_order
            if e[0] in ("span.start", "span.end", "output_text.delta")
        ]
        first_text = next(
            (i for i, e in enumerate(relevant) if e[0] == "output_text.delta"), -1
        )
        assert first_text > 0, f"No output_text.delta seen in {relevant}"
        before_text = relevant[:first_text]
        starts = [e[1] for e in before_text if e[0] == "span.start"]
        assert "request" in starts, (
            f"request span.start missing before text delta: {before_text}"
        )
        assert "llm" in starts, (
            f"llm span.start missing before text delta: {before_text}"
        )
