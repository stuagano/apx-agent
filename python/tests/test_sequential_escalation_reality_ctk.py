"""#841: opted-in failures stop the chain without publishing rejected output."""

import asyncio
import json
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, field_serializer, field_validator

from apx_agent import Agent, Dependencies, Message, OutputValidationError, SequentialAgent, compile_to_langgraph
from apx_agent import _compile


class Finding(BaseModel):
    record_id: int


class _Model(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def _model(monkeypatch, replies):
    model = _Model(messages=iter(replies))
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **kw: model)


def _request():
    request = MagicMock()
    request.headers = {}
    request.app.state.agent_context.config.model = "fake"
    return request


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "30"])
def test_timeout_requires_positive_finite_number(timeout):
    with pytest.raises((TypeError, ValueError), match="timeout_s"):
        Agent(timeout_s=timeout)


def test_sequence_policy_validation():
    with pytest.raises(ValueError, match="on_failure"):
        SequentialAgent([Agent()], on_failure="retry")


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["compiled", "sync", "run", "stream"])
async def test_schema_miss_is_terminal_with_prior_validated_evidence(monkeypatch, entry):
    seen = []
    _model(monkeypatch, [AIMessage(content='{"record_id": 42}'), AIMessage(content="private rejected output")])
    root = SequentialAgent([
        Agent(name="gather", output_schema=Finding, output_key="finding"),
        Agent(name="inspect", output_schema=Finding, output_key="rejected", after_agent_callback=seen.append),
        Agent(name="guess", before_agent_callback=seen.append),
    ], name="review", on_failure="escalate")
    if entry in {"compiled", "sync"}:
        graph = compile_to_langgraph(root, ws=None, model="fake")
        initial = {"messages": [HumanMessage(content="go")]}
        result = await graph.ainvoke(initial) if entry == "compiled" else await asyncio.to_thread(graph.invoke, initial)
        packet = result["messages"][-1].additional_kwargs["apx_data_parts"][0]["data"]
        assert result["state"] == {"finding": {"record_id": 42}}
    elif entry == "run":
        packet = json.loads(await root.run([Message(role="user", content="go")], _request()))
    else:
        chunks = [c async for c in root.stream([Message(role="user", content="go")], _request())]
        assert not any("private rejected" in c for c in chunks)
        packet = json.loads(chunks[-1])
    assert packet["availability"] == "unavailable"
    assert packet["reason"] == "schema_miss"
    assert packet["failed_step"] == ["review", "inspect"]
    assert packet["evidence"] == [{"step": ["review", "gather"], "output_key": "finding", "data": {"record_id": 42}}]
    assert "private rejected" not in json.dumps(packet)
    assert seen == []


@pytest.mark.asyncio
async def test_unavailability_stops_before_another_model_turn(monkeypatch):
    called = []

    def unavailable() -> dict:
        """Retrieve evidence."""
        called.append("tool")
        return {"availability": "unavailable", "capability": "genie", "error": "private upstream error"}

    _model(monkeypatch, [AIMessage(content="", tool_calls=[{"id": "c1", "name": "unavailable", "args": {}}])])
    root = SequentialAgent([
        Agent(name="inspect", tools=[unavailable], after_agent_callback=lambda _: called.append("success")),
        Agent(name="guess", before_agent_callback=lambda _: called.append("downstream")),
    ], on_failure="escalate")
    result = await compile_to_langgraph(root, ws=None, model="fake").ainvoke({"messages": [HumanMessage(content="go")]})
    packet = result["messages"][-1].additional_kwargs["apx_data_parts"][0]["data"]
    assert packet["reason"] == "unavailable"
    assert packet["capability"] == "genie"
    assert "private upstream" not in json.dumps(packet)
    assert called == ["tool"]


@pytest.mark.asyncio
async def test_timeout_and_nested_failure_stop_outer_siblings(monkeypatch):
    seen = []

    async def hang(messages):
        await asyncio.sleep(10)

    _model(monkeypatch, [])
    root = SequentialAgent([
        SequentialAgent([Agent(name="slow", timeout_s=0.02, before_agent_callback=hang)], name="inner"),
        Agent(before_agent_callback=seen.append),
    ], name="outer", on_failure="escalate")
    result = await asyncio.wait_for(compile_to_langgraph(root, ws=None, model="fake").ainvoke({"messages": [HumanMessage(content="go")]}), 1)
    packet = result["messages"][-1].additional_kwargs["apx_data_parts"][0]["data"]
    assert packet["reason"] == "timeout"
    assert packet["failed_step"] == ["outer", "inner", "slow"]
    assert seen == []


@pytest.mark.asyncio
async def test_default_policy_still_raises_schema_error(monkeypatch):
    _model(monkeypatch, [AIMessage(content="invalid")])
    root = SequentialAgent([Agent(output_schema=Finding)])
    with pytest.raises(OutputValidationError):
        await compile_to_langgraph(root, ws=None, model="fake").ainvoke({"messages": [HumanMessage(content="go")]})


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["input", "output"])
async def test_guardrail_rejection_is_not_schema_escalation(monkeypatch, guard):
    _model(monkeypatch, [AIMessage(content='{"record_id": 42}')])
    root = SequentialAgent([Agent(output_schema=Finding, **{f"{guard}_guardrails": [lambda _: "blocked"]})], on_failure="escalate")
    with pytest.raises(OutputValidationError, match="guardrail"):
        await compile_to_langgraph(root, ws=None, model="fake").ainvoke({"messages": [HumanMessage(content="go")]})


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_timed_out_sync_tool_cannot_publish_or_mutate_prior_state(monkeypatch, sync):
    release = threading.Event()
    finished = threading.Event()

    def slow(record_id: int, state: Dependencies.State) -> str:
        """Read evidence slowly."""
        try:
            release.wait(3)
            state["prior"]["ids"].append(record_id)
            state["late"] = "must not publish"
            return "late success"
        finally:
            finished.set()

    _model(monkeypatch, [AIMessage(content="", tool_calls=[{"id": "c1", "name": "slow", "args": {"record_id": 99}}])])
    root = SequentialAgent([Agent(name="slow", tools=[slow], timeout_s=0.1)], on_failure="escalate")
    initial = {"messages": [HumanMessage(content="go")], "state": {"prior": {"ids": [42]}}}
    graph = compile_to_langgraph(root, ws=None, model="fake")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(graph.invoke, initial) if sync else graph.ainvoke(initial), 1)
        packet = result["messages"][-1].additional_kwargs["apx_data_parts"][0]["data"]
        assert packet["reason"] == "timeout"
        assert not finished.is_set(), "Response waited for the original worker"
        assert result["state"] == {"prior": {"ids": [42]}}
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
    assert initial["state"] == {"prior": {"ids": [42]}}
    assert result["state"] == {"prior": {"ids": [42]}}


@pytest.mark.asyncio
async def test_default_unavailability_remains_a_tool_result(monkeypatch):
    def unavailable() -> dict:
        """Get optional evidence."""
        return {"availability": "unavailable"}

    _model(monkeypatch, [AIMessage(content="", tool_calls=[{"id": "c1", "name": "unavailable", "args": {}}]), AIMessage(content="No evidence")])
    result = await compile_to_langgraph(SequentialAgent([Agent(tools=[unavailable])]), ws=None, model="fake").ainvoke({"messages": [HumanMessage(content="go")]})
    assert result["messages"][-1].content == "No evidence"
    assert not result.get("_apx_escalation")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("bug"), TimeoutError("upstream timeout"), asyncio.CancelledError()])
async def test_unexpected_errors_and_external_cancellation_do_not_escalate(monkeypatch, error):
    async def fail(messages):
        raise error

    _model(monkeypatch, [])
    root = SequentialAgent([Agent(timeout_s=1, before_agent_callback=fail)], on_failure="escalate")
    with pytest.raises(type(error)) as raised:
        await compile_to_langgraph(root, ws=None, model="fake").ainvoke({"messages": [HumanMessage(content="go")]})
    assert raised.value is error


@pytest.mark.asyncio
async def test_fresh_invocation_clears_prior_escalation_and_evidence(monkeypatch):
    _model(monkeypatch, [AIMessage(content='{"record_id": 42}'), AIMessage(content="invalid"), AIMessage(content="invalid")])
    root = SequentialAgent([
        Agent(name="one", output_schema=Finding),
        Agent(name="two", output_schema=Finding),
    ], on_failure="escalate")
    graph = compile_to_langgraph(root, ws=None, model="fake")
    first = await graph.ainvoke({"messages": [HumanMessage(content="go")]})
    assert len(first["_apx_escalation"]["evidence"]) == 1
    second = await graph.ainvoke(first)
    assert second["_apx_escalation"]["failed_step"] == ["sequence", "one"]
    assert second["_apx_escalation"]["evidence"] == []


@pytest.mark.asyncio
async def test_routed_nested_escalation_stops_outer_sequence(monkeypatch):
    from apx_agent import KeywordRouter

    seen = []
    _model(monkeypatch, [AIMessage(content="invalid")])
    inner = SequentialAgent([Agent(name="typed", output_schema=Finding)], name="inner", on_failure="escalate")
    router = KeywordRouter(branches=[("investigate", inner, ["go"])], default=Agent())
    root = SequentialAgent([router, Agent(before_agent_callback=seen.append)])
    result = await compile_to_langgraph(root, ws=None, model="fake").ainvoke({"messages": [HumanMessage(content="go")]})
    assert result["_apx_escalation"]["reason"] == "schema_miss"
    assert seen == []


def test_sdk_executor_cannot_silently_drop_timeout():
    from apx_agent import AgentConfig
    from apx_agent._executor_factory import create_executor
    from apx_agent._langgraph_executor import LangGraphExecutor

    executor = create_executor(Agent(timeout_s=1), AgentConfig(name="timed", executor="claude-sdk"))
    assert isinstance(executor, LangGraphExecutor)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["validate", "serialize"])
async def test_timeout_does_not_wait_for_blocking_schema_hooks(monkeypatch, phase):
    release = threading.Event()
    finished = threading.Event()

    def block(value):
        try:
            release.wait(3)
            return value
        finally:
            finished.set()

    class SlowFinding(BaseModel):
        record_id: int

        @field_validator("record_id")
        @classmethod
        def validate_id(cls, value):
            return block(value) if phase == "validate" else value

        @field_serializer("record_id")
        def serialize_id(self, value):
            return block(value) if phase == "serialize" else value

    _model(monkeypatch, [AIMessage(content='{"record_id": 42}')])
    root = SequentialAgent([Agent(output_schema=SlowFinding, timeout_s=0.15)], on_failure="escalate")
    try:
        result = await compile_to_langgraph(root, ws=None, model="fake").ainvoke({"messages": [HumanMessage(content="go")]})
        assert not finished.is_set(), "Deadline waited for the schema hook to complete"
        assert result["_apx_escalation"]["reason"] == "timeout"
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)


@pytest.mark.asyncio
async def test_later_state_edits_cannot_rewrite_validated_evidence(monkeypatch):
    class Ids(BaseModel):
        ids: list[int]

    def edit(value: str, state: Dependencies.State) -> str:
        """Edit working state."""
        state["prior"]["ids"].append(value)
        state["edited"] = True
        return "edited"

    _model(monkeypatch, [
        AIMessage(content='{"ids": [1]}'),
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "edit", "args": {"value": "bad"}}]),
        AIMessage(content="done"), AIMessage(content="invalid"),
    ])
    root = SequentialAgent([
        Agent(name="first", output_schema=Ids, output_key="prior"),
        Agent(name="edit", tools=[edit]),
        Agent(name="last", output_schema=Ids),
    ], on_failure="escalate")
    result = await compile_to_langgraph(root, ws=None, model="fake").ainvoke({"messages": [HumanMessage(content="go")]})
    assert result["_apx_escalation"]["evidence"][0]["data"] == {"ids": [1]}
    first_data = next(m for m in result["messages"] if isinstance(m, AIMessage) and m.additional_kwargs.get("apx_data_parts"))
    assert first_data.additional_kwargs["apx_data_parts"][0]["data"] == {"ids": [1]}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_standalone_deadline_remains_timeout_error(monkeypatch, stream):
    async def slow(messages):
        await asyncio.sleep(10)

    _model(monkeypatch, [])
    agent = Agent(timeout_s=0.02, before_agent_callback=slow)
    with pytest.raises(TimeoutError):
        if stream:
            async for _ in agent.stream([Message(role="user", content="go")], _request()):
                pytest.fail("Timed-out output was published")
        else:
            await agent.run([Message(role="user", content="go")], _request())


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_opted_in_run_does_not_replay_after_success_hook_error(monkeypatch, stream):
    effects = []

    def write() -> str:
        """Perform an external effect."""
        effects.append("write")
        return "written"

    def broken_callback(answer):
        raise TypeError("callback failed after write")

    _model(monkeypatch, [AIMessage(content="", tool_calls=[{"id": "c1", "name": "write", "args": {}}]), AIMessage(content="done")])
    root = SequentialAgent([Agent(tools=[write], after_agent_callback=broken_callback)], on_failure="escalate")
    with pytest.raises(TypeError, match="callback failed"):
        if stream:
            async for _ in root.stream([Message(role="user", content="go")], _request()):
                pytest.fail("Failed-step output was published")
        else:
            await root.run([Message(role="user", content="go")], _request())
    assert effects == ["write"]
