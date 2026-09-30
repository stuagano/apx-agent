"""#835: typed steps publish validated data or stop before their consumer."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator

from apx_agent import Agent, Message, SequentialAgent, compile_to_langgraph
from apx_agent import _compile


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: int
    status: Literal["supported", "needs_review"]
    evidence: list[str] = Field(min_length=1)


PAYLOAD = {"record_id": 42, "status": "supported", "evidence": ["record 42"]}
RAW = json.dumps(PAYLOAD, indent=2)


class _Model(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def _model(monkeypatch: pytest.MonkeyPatch, replies: list[AIMessage]) -> None:
    model = _Model(messages=iter(replies))
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **kw: model)


def _request() -> MagicMock:
    request = MagicMock()
    request.headers = {}
    request.app.state.agent_context.config.model = "fake"
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["compiled", "sync", "run", "stream"])
async def test_typed_payload_reaches_consumer_as_json(monkeypatch, entry):
    seen = []
    accepted = []
    _model(monkeypatch, [AIMessage(content=RAW), AIMessage(content="summary")])
    producer = Agent(
        name="extract", output_schema=Finding, output_key="finding",
        after_agent_callback=accepted.append,
    )
    consumer = Agent(instructions="Finding: {finding}", before_model=lambda m: seen.append(m))
    pipeline = SequentialAgent([producer, consumer])
    if entry in {"compiled", "sync"}:
        graph = compile_to_langgraph(pipeline, ws=None, model="fake")
        initial = {"messages": [HumanMessage(content="find")]}
        result = await graph.ainvoke(initial) if entry == "compiled" else await asyncio.to_thread(graph.invoke, initial)
        assert result["state"]["finding"] == PAYLOAD
        assert result["messages"][-1].content == "summary"
    elif entry == "run":
        assert await pipeline.run([Message(role="user", content="find")], _request()) == "summary"
    else:
        chunks = [c async for c in pipeline.stream([Message(role="user", content="find")], _request())]
        assert chunks[-1] == "summary"
    assert len(accepted) == 1
    assert json.loads(accepted[0]) == PAYLOAD
    prompts = [m.content for call in seen for batch in call for m in batch if m.type == "system"]
    assert json.loads(next(p.removeprefix("Finding: ") for p in prompts if p.startswith("Finding: "))) == PAYLOAD


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "prose", "```json\n{}\n```", "{}", RAW.replace("42,", '"42",'), RAW.replace('[\n    "record 42"\n  ]', "[]")])
async def test_invalid_output_stops_before_consumer_and_success_callback(monkeypatch, bad):
    from apx_agent import OutputValidationError

    accepted = []
    downstream = []
    _model(monkeypatch, [AIMessage(content=bad), AIMessage(content="must not run")])
    pipeline = SequentialAgent([
        Agent(name="extract", output_schema=Finding, output_key="finding", after_agent_callback=accepted.append),
        Agent(before_agent_callback=downstream.append),
    ])
    graph = compile_to_langgraph(pipeline, ws=None, model="fake")
    initial = {"messages": [HumanMessage(content="find")], "state": {"finding": {"stale": True}}}
    with pytest.raises(OutputValidationError) as error:
        await graph.ainvoke(initial)
    assert error.value.agent_name == "extract"
    assert error.value.output_key == "finding"
    assert accepted == []
    assert downstream == []
    assert initial["state"]["finding"] == {"stale": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["run", "stream"])
async def test_direct_failure_is_typed_and_emits_no_rejected_text(monkeypatch, entry):
    from apx_agent import OutputValidationError

    secret = "private model output"
    _model(monkeypatch, [AIMessage(content=secret)])
    agent = Agent(name="extract", output_schema=Finding)
    chunks = []
    with pytest.raises(OutputValidationError) as error:
        if entry == "run":
            await agent.run([Message(role="user", content="find")], _request())
        else:
            async for chunk in agent.stream([Message(role="user", content="find")], _request()):
                chunks.append(chunk)
    assert chunks == []
    assert secret not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["input", "output"])
@pytest.mark.parametrize("entry", ["compiled", "run", "stream"])
async def test_guardrail_rejection_is_not_a_typed_success(monkeypatch, guard, entry):
    from apx_agent import OutputValidationError

    _model(monkeypatch, [AIMessage(content=RAW)])
    accepted = []
    agent = Agent(
        output_schema=Finding, output_key="finding", after_agent_callback=accepted.append,
        **{f"{guard}_guardrails": [lambda _: "blocked"]},
    )
    with pytest.raises(OutputValidationError, match="guardrail"):
        if entry == "compiled":
            graph = compile_to_langgraph(agent, ws=None, model="fake")
            await graph.ainvoke({"messages": [HumanMessage(content="find")]})
        elif entry == "run":
            await agent.run([Message(role="user", content="find")], _request())
        else:
            async for _ in agent.stream([Message(role="user", content="find")], _request()):
                pytest.fail("guardrail rejection was emitted as typed output")
    assert accepted == []


@pytest.mark.asyncio
async def test_standalone_typed_stream_returns_one_validated_json_message(monkeypatch):
    prompts = []
    accepted = []
    _model(monkeypatch, [AIMessage(content=RAW)])
    agent = Agent(output_schema=Finding, before_model=prompts.append, after_agent_callback=accepted.append)
    chunks = [c async for c in agent.stream([Message(role="user", content="find")], _request())]
    assert len(chunks) == 1
    assert json.loads(chunks[0]) == PAYLOAD
    assert accepted == chunks
    assert any('"record_id"' in str(m.content) for call in prompts for batch in call for m in batch if m.type == "system")


@pytest.mark.parametrize("schema", [dict, {}, Finding(record_id=42, status="supported", evidence=["x"])])
def test_schema_must_be_a_model_class(schema):
    with pytest.raises(TypeError, match="output_schema.*BaseModel"):
        Agent(output_schema=schema)


@pytest.mark.asyncio
async def test_served_token_stream_does_not_leak_rejected_output(monkeypatch):
    from apx_agent import OutputValidationError

    _model(monkeypatch, [AIMessage(content="rejected private answer")])
    graph = compile_to_langgraph(Agent(output_schema=Finding), ws=None, model="fake")
    events = []
    with pytest.raises(OutputValidationError):
        async for event in graph.astream(
            {"messages": [HumanMessage(content="find")]}, stream_mode=["updates", "messages"],
        ):
            events.append(event)
    assert events == []


@pytest.mark.asyncio
async def test_invalid_result_does_not_replay_tools_or_run_next_step(monkeypatch):
    from apx_agent import OutputValidationError

    writes = []
    downstream = []

    def save(value: str) -> str:
        """Record the value."""
        writes.append(value)
        return "saved"

    _model(monkeypatch, [
        AIMessage(content="", tool_calls=[{"id": "save-1", "name": "save", "args": {"value": "once"}}]),
        AIMessage(content="invalid output"),
    ])
    pipeline = SequentialAgent([
        Agent(tools=[save], output_schema=Finding),
        Agent(before_agent_callback=downstream.append),
    ])
    with pytest.raises(OutputValidationError):
        await pipeline.run([Message(role="user", content="find")], _request())
    assert writes == ["once"]
    assert downstream == []


def test_control_flow_composites_reject_unsupported_typed_contracts():
    from apx_agent import HandoffAgent, LoopAgent, ParallelAgent

    producer = Agent(name="extract", output_schema=Finding)
    for composite in [LoopAgent(producer), HandoffAgent(agents={"extract": producer}, start="extract"), ParallelAgent([producer])]:
        with pytest.raises(ValueError, match="output_schema is not supported"):
            compile_to_langgraph(composite, ws=None, model="fake")


@pytest.mark.asyncio
async def test_parent_and_schema_instructions_use_one_system_message(monkeypatch):
    prompts = []
    _model(monkeypatch, [AIMessage(content=RAW)])
    pipeline = SequentialAgent([
        Agent(instructions="Extract evidence.", output_schema=Finding, before_model=prompts.append),
    ], instructions="Only use supplied records.")
    assert json.loads(await pipeline.run([Message(role="user", content="find")], _request())) == PAYLOAD
    system = [m for call in prompts for batch in call for m in batch if m.type == "system"]
    assert len(system) == 1
    assert "Only use supplied records." in system[0].content
    assert "Extract evidence." in system[0].content
    assert '"record_id"' in system[0].content


@pytest.mark.asyncio
async def test_non_json_result_is_a_typed_failure(monkeypatch):
    from apx_agent import OutputValidationError

    class Reading(BaseModel):
        value: float

    _model(monkeypatch, [AIMessage(content='{"value": NaN}')])
    with pytest.raises(OutputValidationError):
        await Agent(output_schema=Reading).run([Message(role="user", content="read")], _request())


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["run", "stream"])
async def test_nested_sequence_instructions_are_scoped_and_preserve_caller_system(monkeypatch, entry):
    first = []
    second = []
    _model(monkeypatch, [AIMessage(content=RAW), AIMessage(content="summary")])
    pipeline = SequentialAgent([
        SequentialAgent([
            Agent(output_schema=Finding, output_key="finding", before_model=first.append),
        ], instructions="INNER REQUIRED"),
        Agent(instructions="Summarize {finding}", before_model=second.append),
    ], instructions="OUTER REQUIRED")
    messages = [Message(role="system", content="CALLER REQUIRED"), Message(role="user", content="find")]
    if entry == "run":
        assert await pipeline.run(messages, _request()) == "summary"
    else:
        assert [c async for c in pipeline.stream(messages, _request())][-1] == "summary"
    first_system = "\n".join(str(m.content) for call in first for batch in call for m in batch if m.type == "system")
    second_system = "\n".join(str(m.content) for call in second for batch in call for m in batch if m.type == "system")
    for instruction in ["OUTER REQUIRED", "CALLER REQUIRED"]:
        assert instruction in first_system
        assert instruction in second_system
    assert "INNER REQUIRED" in first_system
    assert "INNER REQUIRED" not in second_system


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["run", "stream"])
@pytest.mark.parametrize("failure", ["validator", "guardrail"])
async def test_typed_completion_exception_cannot_trigger_executor_replay(monkeypatch, entry, failure):
    from apx_agent import OutputValidationError

    writes = []

    def save(value: str) -> str:
        """Save the value."""
        writes.append(value)
        return "saved"

    def broken(value):
        raise TypeError("private validator details")

    class BrokenFinding(Finding):
        @field_validator("record_id")
        @classmethod
        def validate_record(cls, value: int) -> int:
            return broken(value)

    tool_call = AIMessage(content="", tool_calls=[{"id": "save-1", "name": "save", "args": {"value": "once"}}])
    _model(monkeypatch, [tool_call, AIMessage(content=RAW), tool_call, AIMessage(content=RAW)])
    agent = Agent(
        tools=[save],
        output_schema=BrokenFinding if failure == "validator" else Finding,
        output_guardrails=[broken] if failure == "guardrail" else [],
    )
    expected = OutputValidationError if failure == "validator" else TypeError
    with pytest.raises(expected) as error:
        if entry == "run":
            await agent.run([Message(role="user", content="save")], _request())
        else:
            async for _ in agent.stream([Message(role="user", content="save")], _request()):
                pytest.fail("failed completion was emitted")
    assert writes == ["once"]
    if failure == "validator":
        assert "private validator details" not in str(error.value)


@pytest.mark.asyncio
async def test_root_json_with_type_field_is_not_decoded_as_provider_content(monkeypatch):
    class Findings(RootModel[list[dict[str, str]]]):
        pass

    data = [{"type": "finding", "value": "42"}]
    _model(monkeypatch, [AIMessage(content=json.dumps(data))])
    text = await Agent(output_schema=Findings).run([Message(role="user", content="find")], _request())
    assert json.loads(text) == data
