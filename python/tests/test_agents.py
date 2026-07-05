"""Tests for _agents.py — all agent types and orchestration patterns."""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter

from apx_agent import LlmAgent, AgentConfig, AgentTool, Message
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
# LlmAgent (LlmAgent)
# ---------------------------------------------------------------------------


class TestLlmAgent:
    def test_alias(self):
        assert LlmAgent is LlmAgent

    def test_tools_optional(self):
        """#449: an orchestrator whose only capabilities are config-declared
        sub_agents needs no local tools — Agent(instructions=...) constructs."""
        from apx_agent import Agent

        agent = Agent(instructions="Route every request to a sub-agent.")
        assert agent._tool_fns == []
        assert agent.collect_tools() == []

    def test_omitted_tools_lists_are_not_shared(self):
        """None → a fresh list per instance, never a shared mutable default."""
        first, second = LlmAgent(), LlmAgent()
        first._tool_fns.append(get_weather)
        assert second._tool_fns == []

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
        agent = LlmAgent(tools=[structured_tool])
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

        agent = LlmAgent(tools=[get_weather], input_guardrails=[reject_all])
        request = MagicMock()
        result = await agent.run([Message(role="user", content="test")], request)
        assert result == "Blocked by guardrail"

    @pytest.mark.asyncio
    async def test_input_guardrail_pass(self):
        def allow_all(messages):
            return None

        agent = LlmAgent(tools=[get_weather], input_guardrails=[allow_all])
        result = await agent._apply_input_guardrails([Message(role="user", content="test")])
        assert result is None

    @pytest.mark.asyncio
    async def test_output_guardrail_replacement(self):
        def replace_output(text):
            return "Sanitized output"

        agent = LlmAgent(tools=[get_weather], output_guardrails=[replace_output])
        result = await agent._apply_output_guardrails("some text")
        assert result == "Sanitized output"

    @pytest.mark.asyncio
    async def test_output_guardrail_pass(self):
        def pass_through(text):
            return None

        agent = LlmAgent(tools=[get_weather], output_guardrails=[pass_through])
        result = await agent._apply_output_guardrails("some text")
        assert result is None

    @pytest.mark.asyncio
    async def test_async_guardrails(self):
        async def async_reject(messages):
            return "Async blocked"

        agent = LlmAgent(tools=[get_weather], input_guardrails=[async_reject])
        result = await agent._apply_input_guardrails([Message(role="user", content="test")])
        assert result == "Async blocked"

    # ADK alignment: description, instruction alias, named callbacks

    def test_description_stored(self):
        agent = LlmAgent(tools=[get_weather], description="Checks the weather forecast.")
        assert agent._description == "Checks the weather forecast."

    def test_description_defaults_empty(self):
        agent = LlmAgent(tools=[get_weather])
        assert agent._description == ""

    def test_instruction_alias_for_instructions(self):
        agent = LlmAgent(tools=[get_weather], instruction="Be concise.")
        assert agent._instructions == "Be concise."

    def test_instructions_takes_precedence_over_instruction(self):
        agent = LlmAgent(tools=[get_weather], instructions="Primary.", instruction="Secondary.")
        assert agent._instructions == "Primary."

    def test_before_model_callback_alias(self):
        hook = MagicMock()
        agent = LlmAgent(tools=[get_weather], before_model_callback=hook)
        assert agent._before_model is hook

    def test_after_model_callback_alias(self):
        hook = MagicMock()
        agent = LlmAgent(tools=[get_weather], after_model_callback=hook)
        assert agent._after_model is hook

    def test_before_tool_callback_alias(self):
        hook = MagicMock()
        agent = LlmAgent(tools=[get_weather], before_tool_callback=hook)
        assert agent._before_tool is hook

    def test_after_tool_callback_alias(self):
        hook = MagicMock()
        agent = LlmAgent(tools=[get_weather], after_tool_callback=hook)
        assert agent._after_tool is hook

    def test_callback_alias_preferred_over_legacy(self):
        legacy = MagicMock()
        adk = MagicMock()
        agent = LlmAgent(tools=[get_weather], before_model=legacy, before_model_callback=adk)
        assert agent._before_model is adk

    @pytest.mark.asyncio
    async def test_before_agent_callback_invoked(self):
        called_with = []

        def on_before(messages):
            called_with.append(messages)

        agent = LlmAgent(
            tools=[get_weather],
            before_agent_callback=on_before,
            input_guardrails=[lambda _: "blocked"],
        )
        msgs = [Message(role="user", content="hi")]
        await agent.run(msgs, MagicMock())
        assert called_with == [msgs]

    @pytest.mark.asyncio
    async def test_after_agent_callback_invoked(self):
        called_with = []

        def on_after(text):
            called_with.append(text)

        agent = LlmAgent(
            tools=[get_weather],
            after_agent_callback=on_after,
            input_guardrails=[lambda _: "the answer"],
        )
        await agent.run([Message(role="user", content="hi")], MagicMock())
        # input guardrail short-circuits before run_via_compile; after_agent not called
        assert called_with == []

    @pytest.mark.asyncio
    async def test_async_before_agent_callback(self):
        called = []

        async def async_before(messages):
            called.append(True)

        agent = LlmAgent(
            tools=[get_weather],
            before_agent_callback=async_before,
            input_guardrails=[lambda _: "blocked"],
        )
        await agent.run([Message(role="user", content="hi")], MagicMock())
        assert called == [True]

    def test_on_model_error_callback_stored(self):
        cb = MagicMock()
        agent = LlmAgent(tools=[get_weather], on_model_error_callback=cb)
        assert agent._on_model_error_callback is cb

    def test_on_tool_error_callback_stored(self):
        cb = MagicMock()
        agent = LlmAgent(tools=[get_weather], on_tool_error_callback=cb)
        assert agent._on_tool_error_callback is cb


# ---------------------------------------------------------------------------
# SequentialAgent
# ---------------------------------------------------------------------------


class TestSequentialAgent:
    def test_requires_agents(self):
        with pytest.raises(ValueError, match="at least one"):
            SequentialAgent(agents=[])

    def test_collect_tools_merges(self):
        a1 = LlmAgent(tools=[get_weather])
        a2 = LlmAgent(tools=[structured_tool])
        seq = SequentialAgent(agents=[a1, a2])
        tools = seq.collect_tools()
        names = {t.name for t in tools}
        assert "get_weather" in names
        assert "structured_tool" in names

    def test_get_tool_routers_merges(self):
        a1 = LlmAgent(tools=[get_weather])
        a2 = LlmAgent(tools=[structured_tool])
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
        a1 = LlmAgent(tools=[get_weather])
        a2 = LlmAgent(tools=[structured_tool])
        par = ParallelAgent(agents=[a1, a2])
        tools = par.collect_tools()
        assert len(tools) == 2


# ---------------------------------------------------------------------------
# LoopAgent
# ---------------------------------------------------------------------------


class TestLoopAgent:
    def test_collect_tools_includes_finish_loop(self):
        inner = LlmAgent(tools=[get_weather])
        loop = LoopAgent(agent=inner, max_iterations=3)
        tools = loop.collect_tools()
        names = {t.name for t in tools}
        assert "finish_loop" in names
        assert "get_weather" in names

    def test_get_tool_routers_includes_finish(self):
        inner = LlmAgent(tools=[get_weather])
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
        a1 = LlmAgent(tools=[get_weather])
        a2 = LlmAgent(tools=[structured_tool])
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
        a1 = LlmAgent(tools=[get_weather])
        a2 = LlmAgent(tools=[structured_tool])
        router = RouterAgent(agents=[
            ("weather", "Weather agent", a1),
            ("data", "Data agent", a2),
        ])
        tools = router.collect_tools()
        names = {t.name for t in tools}
        assert "get_weather" in names
        assert "structured_tool" in names

    # Description-driven (ADK-style) form

    def test_description_driven_form(self):
        a1 = LlmAgent(tools=[get_weather], name="weather", description="Handles weather queries.")
        a2 = LlmAgent(tools=[structured_tool], name="data", description="Handles data queries.")
        router = RouterAgent(agents=[a1, a2])
        assert len(router._routes) == 2
        names = {r[0] for r in router._routes}
        assert names == {"weather", "data"}

    def test_description_driven_uses_agent_description(self):
        a1 = LlmAgent(tools=[get_weather], name="weather", description="Weather specialist.")
        router = RouterAgent(agents=[a1])
        assert router._routes[0][1] == "Weather specialist."

    def test_description_driven_fallback_description(self):
        a1 = LlmAgent(tools=[get_weather], name="weather")
        router = RouterAgent(agents=[a1])
        assert "weather" in router._routes[0][1]

    def test_description_driven_transfer_schemas(self):
        a1 = LlmAgent(tools=[get_weather], name="weather", description="Weather specialist.")
        a2 = LlmAgent(tools=[structured_tool], name="data", description="Data specialist.")
        router = RouterAgent(agents=[a1, a2])
        schemas = router._transfer_tool_schemas()
        weather_schema = next(s for s in schemas if s["function"]["name"] == "transfer_to_weather")
        assert weather_schema["function"]["description"] == "Weather specialist."

    def test_description_driven_collect_tools(self):
        a1 = LlmAgent(tools=[get_weather], name="weather", description="Weather.")
        a2 = LlmAgent(tools=[structured_tool], name="data", description="Data.")
        router = RouterAgent(agents=[a1, a2])
        tools = router.collect_tools()
        names = {t.name for t in tools}
        assert "get_weather" in names
        assert "structured_tool" in names

    def test_description_driven_requires_name(self):
        a1 = LlmAgent(tools=[get_weather])  # no name
        with pytest.raises(ValueError, match="name="):
            RouterAgent(agents=[a1])


# ---------------------------------------------------------------------------
# HandoffAgent
# ---------------------------------------------------------------------------


class TestHandoffAgent:
    def test_invalid_start(self):
        a1 = LlmAgent(tools=[get_weather])
        with pytest.raises(ValueError, match="not found"):
            HandoffAgent(agents={"a": a1}, start="nonexistent")

    def test_transfer_tools_exclude_self(self):
        a1 = LlmAgent(tools=[get_weather])
        a2 = LlmAgent(tools=[structured_tool])
        handoff = HandoffAgent(agents={"a": a1, "b": a2}, start="a")
        transfer_tools = handoff._transfer_tools_for("a")
        names = {t.name for t in transfer_tools}
        assert "transfer_to_b" in names
        assert "transfer_to_a" not in names

    def test_collect_tools_from_all(self):
        a1 = LlmAgent(tools=[get_weather])
        a2 = LlmAgent(tools=[structured_tool])
        handoff = HandoffAgent(agents={"a": a1, "b": a2}, start="a")
        tools = handoff.collect_tools()
        names = {t.name for t in tools}
        assert "get_weather" in names
        assert "structured_tool" in names

    def test_get_tool_routers_includes_transfers(self):
        a1 = LlmAgent(tools=[get_weather])
        a2 = LlmAgent(tools=[structured_tool])
        handoff = HandoffAgent(agents={"a": a1, "b": a2}, start="a")
        routers = handoff.get_tool_routers()
        all_paths = []
        for r in routers:
            all_paths.extend(route.path for route in r.routes)
        assert "/tools/transfer_to_a" in all_paths
        assert "/tools/transfer_to_b" in all_paths

    # List form (ADK-style)

    def test_list_form_normalizes_to_dict(self):
        a1 = LlmAgent(tools=[get_weather], name="weather")
        a2 = LlmAgent(tools=[structured_tool], name="data")
        handoff = HandoffAgent(agents=[a1, a2], start="weather")
        assert set(handoff._agents.keys()) == {"weather", "data"}

    def test_list_form_default_start_is_first(self):
        a1 = LlmAgent(tools=[get_weather], name="triage")
        a2 = LlmAgent(tools=[structured_tool], name="data")
        handoff = HandoffAgent(agents=[a1, a2])
        assert handoff._start == "triage"

    def test_list_form_requires_name(self):
        a1 = LlmAgent(tools=[get_weather])  # no name
        with pytest.raises(ValueError, match="name="):
            HandoffAgent(agents=[a1])

    def test_list_form_transfer_uses_description(self):
        a1 = LlmAgent(tools=[get_weather], name="weather", description="Weather expert.")
        a2 = LlmAgent(tools=[structured_tool], name="data", description="Data analyst.")
        handoff = HandoffAgent(agents=[a1, a2])
        transfer_tools = handoff._transfer_tools_for("weather")
        data_tool = next(t for t in transfer_tools if t.name == "transfer_to_data")
        assert data_tool.description == "Data analyst."
