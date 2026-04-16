"""Tests for _agents.py — all agent types and orchestration patterns."""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter

from apx_agent import Agent, AgentConfig, AgentTool, Message
from apx_agent._agents import (
    BaseAgent,
    HandoffAgent,
    LlmAgent,
    LoopAgent,
    ParallelAgent,
    RouterAgent,
    SequentialAgent,
)

from .conftest import (
    FakeWorkspaceDep,
    StructuredOutput,
    get_weather,
    no_args,
    query_genie,
    structured_tool,
)


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------


class TestBaseAgent:
    @pytest.mark.asyncio
    async def test_run_raises_not_implemented(self):
        agent = BaseAgent()
        with pytest.raises(NotImplementedError):
            await agent.run([], MagicMock())

    @pytest.mark.asyncio
    async def test_stream_default_yields_run_result(self):
        agent = BaseAgent()
        agent.run = AsyncMock(return_value="hello")
        chunks = [c async for c in agent.stream([], MagicMock())]
        assert chunks == ["hello"]

    def test_get_tool_routers_empty(self):
        assert BaseAgent().get_tool_routers() == []

    def test_collect_tools_empty(self):
        assert BaseAgent().collect_tools() == []

    @pytest.mark.asyncio
    async def test_fetch_remote_tools_empty(self):
        assert await BaseAgent().fetch_remote_tools() == []


# ---------------------------------------------------------------------------
# LlmAgent (Agent)
# ---------------------------------------------------------------------------


class TestLlmAgent:
    def test_alias(self):
        assert Agent is LlmAgent

    def test_collect_tools(self, basic_agent):
        tools = basic_agent.collect_tools()
        assert len(tools) == 2
        names = {t.name for t in tools}
        assert names == {"get_weather", "query_genie"}

    def test_tool_descriptions(self, basic_agent):
        tools = basic_agent.collect_tools()
        weather = next(t for t in tools if t.name == "get_weather")
        assert "weather" in weather.description.lower()

    def test_tool_schema_excludes_deps(self, basic_agent):
        tools = basic_agent.collect_tools()
        genie = next(t for t in tools if t.name == "query_genie")
        assert "ws" not in genie.input_schema.get("properties", {})
        assert "question" in genie.input_schema["properties"]

    def test_tool_schema_includes_defaults(self, basic_agent):
        tools = basic_agent.collect_tools()
        weather = next(t for t in tools if t.name == "get_weather")
        props = weather.input_schema["properties"]
        assert "city" in props
        assert "country_code" in props

    def test_structured_output_schema(self):
        agent = Agent(tools=[structured_tool])
        tools = agent.collect_tools()
        assert len(tools) == 1
        schema = tools[0].output_schema
        assert "properties" in schema
        assert "answer" in schema["properties"]

    def test_build_router(self, basic_agent):
        router = basic_agent.build_router()
        paths = [r.path for r in router.routes]
        assert "/tools/get_weather" in paths
        assert "/tools/query_genie" in paths

    def test_router_handler_signatures(self, basic_agent):
        router = basic_agent.build_router()
        gw_route = next(r for r in router.routes if r.path == "/tools/get_weather")
        sig = inspect.signature(gw_route.endpoint)
        assert "body" in sig.parameters
        assert "ws" not in sig.parameters

        qg_route = next(r for r in router.routes if r.path == "/tools/query_genie")
        sig = inspect.signature(qg_route.endpoint)
        assert "body" in sig.parameters
        assert "ws" in sig.parameters

    def test_get_tool_routers(self, basic_agent):
        routers = basic_agent.get_tool_routers()
        assert len(routers) == 1
        assert isinstance(routers[0], APIRouter)

    @pytest.mark.asyncio
    async def test_input_guardrail_rejection(self):
        def reject_all(messages):
            return "Blocked by guardrail"

        agent = Agent(tools=[get_weather], input_guardrails=[reject_all])
        request = MagicMock()
        result = await agent.run([Message(role="user", content="test")], request)
        assert result == "Blocked by guardrail"

    @pytest.mark.asyncio
    async def test_input_guardrail_pass(self):
        def allow_all(messages):
            return None

        agent = Agent(tools=[get_weather], input_guardrails=[allow_all])
        result = await agent._apply_input_guardrails([Message(role="user", content="test")])
        assert result is None

    @pytest.mark.asyncio
    async def test_output_guardrail_replacement(self):
        def replace_output(text):
            return "Sanitized output"

        agent = Agent(tools=[get_weather], output_guardrails=[replace_output])
        result = await agent._apply_output_guardrails("some text")
        assert result == "Sanitized output"

    @pytest.mark.asyncio
    async def test_output_guardrail_pass(self):
        def pass_through(text):
            return None

        agent = Agent(tools=[get_weather], output_guardrails=[pass_through])
        result = await agent._apply_output_guardrails("some text")
        assert result is None

    @pytest.mark.asyncio
    async def test_async_guardrails(self):
        async def async_reject(messages):
            return "Async blocked"

        agent = Agent(tools=[get_weather], input_guardrails=[async_reject])
        result = await agent._apply_input_guardrails([Message(role="user", content="test")])
        assert result == "Async blocked"


# ---------------------------------------------------------------------------
# SequentialAgent
# ---------------------------------------------------------------------------


class TestSequentialAgent:
    def test_requires_agents(self):
        with pytest.raises(ValueError, match="at least one"):
            SequentialAgent(agents=[])

    def test_collect_tools_merges(self):
        a1 = Agent(tools=[get_weather])
        a2 = Agent(tools=[structured_tool])
        seq = SequentialAgent(agents=[a1, a2])
        tools = seq.collect_tools()
        names = {t.name for t in tools}
        assert "get_weather" in names
        assert "structured_tool" in names

    def test_get_tool_routers_merges(self):
        a1 = Agent(tools=[get_weather])
        a2 = Agent(tools=[structured_tool])
        seq = SequentialAgent(agents=[a1, a2])
        routers = seq.get_tool_routers()
        assert len(routers) == 2

    @pytest.mark.asyncio
    async def test_run_chains_output(self):
        a1 = MagicMock(spec=BaseAgent)
        a1.run = AsyncMock(return_value="step 1 result")
        a2 = MagicMock(spec=BaseAgent)
        a2.run = AsyncMock(return_value="final result")

        seq = SequentialAgent(agents=[a1, a2])
        request = MagicMock()
        result = await seq.run([Message(role="user", content="start")], request)
        assert result == "final result"
        # Second agent should receive the first agent's output
        second_call_messages = a2.run.call_args[0][0]
        assert any(m.content == "step 1 result" for m in second_call_messages)

    @pytest.mark.asyncio
    async def test_instructions_prepended(self):
        a1 = MagicMock(spec=BaseAgent)
        a1.run = AsyncMock(return_value="done")

        seq = SequentialAgent(agents=[a1], instructions="Be helpful")
        await seq.run([Message(role="user", content="hi")], MagicMock())
        call_messages = a1.run.call_args[0][0]
        assert call_messages[0].role == "system"
        assert call_messages[0].content == "Be helpful"


# ---------------------------------------------------------------------------
# ParallelAgent
# ---------------------------------------------------------------------------


class TestParallelAgent:
    def test_requires_agents(self):
        with pytest.raises(ValueError, match="at least one"):
            ParallelAgent(agents=[])

    @pytest.mark.asyncio
    async def test_run_merges_results(self):
        a1 = MagicMock(spec=BaseAgent)
        a1.run = AsyncMock(return_value="result A")
        a2 = MagicMock(spec=BaseAgent)
        a2.run = AsyncMock(return_value="result B")

        par = ParallelAgent(agents=[a1, a2])
        result = await par.run([Message(role="user", content="go")], MagicMock())
        assert "result A" in result
        assert "result B" in result

    def test_collect_tools_merges(self):
        a1 = Agent(tools=[get_weather])
        a2 = Agent(tools=[structured_tool])
        par = ParallelAgent(agents=[a1, a2])
        tools = par.collect_tools()
        assert len(tools) == 2


# ---------------------------------------------------------------------------
# LoopAgent
# ---------------------------------------------------------------------------


class TestLoopAgent:
    def test_collect_tools_includes_finish_loop(self):
        inner = Agent(tools=[get_weather])
        loop = LoopAgent(agent=inner, max_iterations=3)
        tools = loop.collect_tools()
        names = {t.name for t in tools}
        assert "finish_loop" in names
        assert "get_weather" in names

    def test_get_tool_routers_includes_finish(self):
        inner = Agent(tools=[get_weather])
        loop = LoopAgent(agent=inner, max_iterations=3)
        routers = loop.get_tool_routers()
        all_paths = []
        for r in routers:
            all_paths.extend(route.path for route in r.routes)
        assert "/tools/finish_loop" in all_paths


# ---------------------------------------------------------------------------
# RouterAgent
# ---------------------------------------------------------------------------


class TestRouterAgent:
    def test_requires_agents(self):
        with pytest.raises(ValueError, match="at least one"):
            RouterAgent(agents=[])

    def test_transfer_tool_schemas(self):
        a1 = Agent(tools=[get_weather])
        a2 = Agent(tools=[structured_tool])
        router = RouterAgent(agents=[
            ("weather", "Weather agent", a1),
            ("data", "Data agent", a2),
        ])
        schemas = router._transfer_tool_schemas()
        assert len(schemas) == 2
        names = {s["function"]["name"] for s in schemas}
        assert "transfer_to_weather" in names
        assert "transfer_to_data" in names

    def test_collect_tools_from_sub_agents(self):
        a1 = Agent(tools=[get_weather])
        a2 = Agent(tools=[structured_tool])
        router = RouterAgent(agents=[
            ("weather", "Weather agent", a1),
            ("data", "Data agent", a2),
        ])
        tools = router.collect_tools()
        names = {t.name for t in tools}
        assert "get_weather" in names
        assert "structured_tool" in names


# ---------------------------------------------------------------------------
# HandoffAgent
# ---------------------------------------------------------------------------


class TestHandoffAgent:
    def test_invalid_start(self):
        a1 = Agent(tools=[get_weather])
        with pytest.raises(ValueError, match="not found"):
            HandoffAgent(agents={"a": a1}, start="nonexistent")

    def test_transfer_tools_exclude_self(self):
        a1 = Agent(tools=[get_weather])
        a2 = Agent(tools=[structured_tool])
        handoff = HandoffAgent(agents={"a": a1, "b": a2}, start="a")
        transfer_tools = handoff._transfer_tools_for("a")
        names = {t.name for t in transfer_tools}
        assert "transfer_to_b" in names
        assert "transfer_to_a" not in names

    def test_collect_tools_from_all(self):
        a1 = Agent(tools=[get_weather])
        a2 = Agent(tools=[structured_tool])
        handoff = HandoffAgent(agents={"a": a1, "b": a2}, start="a")
        tools = handoff.collect_tools()
        names = {t.name for t in tools}
        assert "get_weather" in names
        assert "structured_tool" in names

    def test_get_tool_routers_includes_transfers(self):
        a1 = Agent(tools=[get_weather])
        a2 = Agent(tools=[structured_tool])
        handoff = HandoffAgent(agents={"a": a1, "b": a2}, start="a")
        routers = handoff.get_tool_routers()
        all_paths = []
        for r in routers:
            all_paths.extend(route.path for route in r.routes)
        assert "/tools/transfer_to_a" in all_paths
        assert "/tools/transfer_to_b" in all_paths
