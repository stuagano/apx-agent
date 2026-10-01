"""Recorded tools exercise the actual compiler, with only model transport faked."""

import asyncio
import copy
import json
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from apx_agent import Agent, Dependencies, SequentialAgent, _compile
from apx_agent._eval_fixtures import evaluate_fixtures


class Finding(BaseModel):
    value: int


class Model(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def read(value: int, ws: Dependencies.UserClient) -> dict:
    """Read one value."""
    raise AssertionError("live tool executed")


def setup(monkeypatch, middle=1, final=2, *, policy="raise"):
    replies = [AIMessage(content="", tool_calls=[{"id": "t1", "name": "read", "args": {"value": 1}}]),
               AIMessage(content=json.dumps({"value": middle})), AIMessage(content=json.dumps({"value": final}))]
    model = Model(messages=iter(replies))
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **kw: model)
    monkeypatch.setattr(_compile, "_resolve_deps_for_fn", lambda *a: pytest.fail("resolved live dependency"))
    root = SequentialAgent([Agent(name="first", tools=[read], output_schema=Finding),
                            Agent(name="last", output_schema=Finding)], name="chain", on_failure=policy)
    case = {"id": "one", "request": "go", "expected_outcome": "correct", "steps": [
        {"path": ["chain", "first"], "tools": [{"name": "read", "args": {"value": 1}, "output": {"value": 1}}], "expected_output": {"value": 1}},
        {"path": ["chain", "last"], "tools": [], "expected_output": {"value": 2}}]}
    return root, case


def test_real_compiler_replays_without_tool_or_dependency_execution(monkeypatch):
    root, case = setup(monkeypatch)
    report = evaluate_fixtures(root, model="fake", fixtures=[case])
    assert report.outcome_counts == {"correct": 1, "escalated_with_evidence": 0, "wrong": 0}
    assert all(s.passed for s in report.cases[0].steps)
    assert root._on_failure == "raise"


def test_wrong_middle_cannot_be_hidden_by_correct_final(monkeypatch):
    root, case = setup(monkeypatch, middle=999)
    report = evaluate_fixtures(root, model="fake", fixtures=[case])
    assert report.cases[0].outcome == "wrong"
    assert report.cases[0].steps[-1].passed
    assert not report.cases[0].steps[0].passed


@pytest.mark.parametrize("mutation", ["missing", "extra", "args"])
def test_tool_mismatches_fail_closed(monkeypatch, mutation):
    root, case = setup(monkeypatch)
    records = case["steps"][0]["tools"]
    if mutation == "missing":
        records.clear()
    elif mutation == "extra":
        records.append(copy.deepcopy(records[0]))
    else:
        records[0]["args"]["value"] = 2
    assert evaluate_fixtures(root, model="fake", fixtures=[case]).cases[0].outcome == "wrong"


def test_all_cases_validated_before_model_call(monkeypatch):
    root, case = setup(monkeypatch)
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **kw: pytest.fail("model built before validation"))
    invalid = copy.deepcopy(case)
    invalid["id"] = "invalid"
    invalid["steps"][1]["expected_output"] = {"value": "bad"}
    with pytest.raises(ValueError, match="fixture"):
        evaluate_fixtures(root, model="fake", fixtures=[case, invalid])


def test_expected_escalation_has_exact_prior_evidence(monkeypatch):
    root, case = setup(monkeypatch, final="rejected-secret", policy="escalate")
    case["expected_outcome"] = "escalated_with_evidence"
    case["expected_failure"] = {"step": ["chain", "last"], "reason": "schema_miss"}
    del case["steps"][1]["expected_output"]
    report = evaluate_fixtures(root, model="fake", fixtures=[case])
    assert report.cases[0].outcome == "escalated_with_evidence"
    assert "rejected-secret" not in repr(report)


def test_caller_cancellation_is_preserved(monkeypatch):
    root, case = setup(monkeypatch)
    def cancelled(_):
        raise asyncio.CancelledError()
    root._agents[0]._before_agent_callback = cancelled
    with pytest.raises(asyncio.CancelledError):
        evaluate_fixtures(root, model="fake", fixtures=[case])


def test_repeated_calls_consume_successive_records_and_cases_are_isolated(monkeypatch):
    root, case = setup(monkeypatch)
    case["steps"][0]["tools"].append({"name": "read", "args": {"value": 1}, "output": {"value": 3}})
    first = AIMessage(content="", tool_calls=[{"id": "a", "name": "read", "args": {"value": 1}}])
    second = AIMessage(content="", tool_calls=[{"id": "b", "name": "read", "args": {"value": 1}}])
    replies = [first, second, AIMessage(content='{"value":1}'), AIMessage(content='{"value":2}')]
    model = Model(messages=iter(replies * 2))
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **kw: model)
    other = copy.deepcopy(case)
    other["id"] = "two"
    report = evaluate_fixtures(root, model="fake", fixtures=[case, other])
    assert [c.outcome for c in report.cases] == ["correct", "correct"]
    assert all(c.tool_calls == ("read", "read") for c in report.cases)


def test_unknown_tool_cannot_be_hidden_by_correct_final(monkeypatch):
    root, case = setup(monkeypatch)
    case["steps"][0]["tools"] = []
    replies = [AIMessage(content="", tool_calls=[{"id": "a", "name": "unknown", "args": {}}]),
               AIMessage(content='{"value":1}'), AIMessage(content='{"value":2}')]
    model = Model(messages=iter(replies))
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **kw: model)
    assert evaluate_fixtures(root, model="fake", fixtures=[case]).cases[0].outcome == "wrong"


@pytest.mark.parametrize("reason", ["timeout", "unavailable", "unavailable_string", "unavailable_block"])
def test_expected_terminal_failures_stop_after_completed_evidence(monkeypatch, reason):
    root, case = setup(monkeypatch, policy="escalate")
    case["expected_outcome"] = "escalated_with_evidence"
    case["expected_failure"] = {"step": ["chain", "last"], "reason": "timeout" if reason == "timeout" else "unavailable"}
    del case["steps"][1]["expected_output"]
    called = []
    root._agents.append(Agent(name="downstream", output_schema=Finding, before_agent_callback=called.append))
    if reason == "timeout":
        async def wait(_):
            await asyncio.sleep(1)
        root._agents[1]._before_agent_callback = wait
        root._agents[1]._timeout_s = 0.01
    else:
        root._agents[1]._tool_fns = [read]
        case["steps"][1]["tools"] = [{"name": "read", "args": {"value": 2}, "output": {"availability": "unavailable"}}]
        if reason == "unavailable_string":
            case["steps"][1]["tools"][0]["output"] = '{"availability":"unavailable"}'
        elif reason == "unavailable_block":
            case["steps"][1]["tools"][0]["output"] = [{"type": "text", "text": '{"availability":"unavailable"}'}]
        replies = [AIMessage(content="", tool_calls=[{"id": "a", "name": "read", "args": {"value": 1}}]),
                   AIMessage(content='{"value":1}'),
                   AIMessage(content="", tool_calls=[{"id": "b", "name": "read", "args": {"value": 2}}])]
        model = Model(messages=iter(replies))
        monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **kw: model)
    assert evaluate_fixtures(root, model="fake", fixtures=[case]).cases[0].outcome == "escalated_with_evidence"
    assert called == []


def test_first_step_failure_has_no_evidence_and_cannot_pass(monkeypatch):
    root, case = setup(monkeypatch, middle="invalid", policy="escalate")
    case["steps"] = case["steps"][:1]
    del case["steps"][0]["expected_output"]
    case["expected_outcome"] = "escalated_with_evidence"
    case["expected_failure"] = {"step": ["chain", "first"], "reason": "schema_miss"}
    assert evaluate_fixtures(root, model="fake", fixtures=[case]).cases[0].outcome == "wrong"


def test_invalid_escalation_metadata_fails_closed(monkeypatch):
    root, case = setup(monkeypatch, final="invalid", policy="escalate")
    case["expected_outcome"] = "escalated_with_evidence"
    case["expected_failure"] = {"step": ["chain", "last"], "reason": "schema_miss"}
    del case["steps"][1]["expected_output"]
    compile_any = _compile._compile_any
    def compile_corrupted(agent, ctx):
        graph = compile_any(agent, ctx)
        if agent is root:
            original = graph.invoke
            def invoke(*args, **kwargs):
                result = original(*args, **kwargs)
                result["_apx_escalation"]["evidence"] = [{"step": ["invented"], "output_key": None, "data": {"value": 99}}]
                return result
            graph.invoke = invoke
        return graph
    monkeypatch.setattr(_compile, "_compile_any", compile_corrupted)
    assert evaluate_fixtures(root, model="fake", fixtures=[case]).cases[0].outcome == "wrong"


@pytest.mark.parametrize("mutation", ["unknown", "duplicate", "unnamed", "deferred", "state", "remote", "parallel", "nonjson", "schema"])
def test_invalid_datasets_and_graphs_never_build_a_model(monkeypatch, mutation):
    from apx_agent import ParallelAgent

    root, case = setup(monkeypatch)
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **kw: pytest.fail("unexpected model build"))
    if mutation == "unknown":
        case["steps"][0]["path"][-1] = "unknown"
    elif mutation == "duplicate":
        root._agents[1]._name = "first"
    elif mutation == "unnamed":
        root._agents[0]._name = None
    elif mutation == "deferred":
        root._agents[0]._tool_loading = "deferred"
    elif mutation == "state":
        def mutate(state: Dependencies.State) -> dict:
            return {}
        root._agents[0]._tool_fns = [mutate]
    elif mutation == "remote":
        root._apx_remote_leaf_bindings = {"first": "unused"}
    elif mutation == "parallel":
        root._agents[0] = ParallelAgent([Agent(name="inner")])
    elif mutation == "nonjson":
        case["steps"][0]["tools"][0]["output"] = float("nan")
    else:
        case["steps"][0]["tools"][0]["args"] = {"value": "1"}
    with pytest.raises(ValueError, match="fixture"):
        evaluate_fixtures(root, model="fake", fixtures=[case])


@pytest.mark.asyncio
async def test_replay_sync_async_and_late_observations_are_frozen():
    from types import SimpleNamespace
    from apx_agent._eval_fixtures import _FixtureReplay

    path = ("root", "leaf")
    case = {"steps": [{"path": list(path), "tools": [
        {"name": "read", "args": {}, "output": 1},
        {"name": "read", "args": {}, "output": 2}]}]}
    replay = _FixtureReplay(case)
    middleware = replay.middleware(path)
    request = SimpleNamespace(tool_call={"name": "read", "args": {}, "id": "a"})
    def forbidden(_):
        pytest.fail("called real tool handler")
    assert middleware.wrap_tool_call(request, forbidden).content == "1"
    assert (await middleware.awrap_tool_call(request, forbidden)).content == "2"
    value = {"value": [1]}
    replay.record(path, value)
    value["value"].append(2)
    replay.close()
    replay.record(path, {"value": "late"})
    with pytest.raises(RuntimeError, match="closed"):
        middleware.wrap_tool_call(request, forbidden)
    assert replay.outputs[path] == {"value": [1]}
    assert replay.calls == ["read", "read"]


def test_shipped_synthetic_example_runs_through_public_api(monkeypatch):
    import runpy
    from pathlib import Path

    example = runpy.run_path(str(Path(__file__).parents[1] / "examples/data-triage-agent/eval/replay_example.py"))
    replies = [AIMessage(content='{"customer_id":"EXAMPLE-001"}'),
               AIMessage(content="", tool_calls=[{"id": "a", "name": "lookup_customer", "args": {"customer_id": "EXAMPLE-001"}}]),
               AIMessage(content='{"customer_id":"EXAMPLE-001","exists":true}'),
               AIMessage(content='{"customer_id":"EXAMPLE-001","status":"found"}'),
               AIMessage(content='{"customer_id":"EXAMPLE-002"}'),
               AIMessage(content="", tool_calls=[{"id": "b", "name": "lookup_customer", "args": {"customer_id": "EXAMPLE-002"}}])]
    model = Model(messages=iter(replies))
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **kw: model)
    report = example["evaluate_chain"](example["build_agent"](), model="fake", fixtures=example["load_fixtures"]())
    assert report.outcome_counts == {"correct": 1, "escalated_with_evidence": 1, "wrong": 0}


def test_nested_paths_and_alias_outputs(monkeypatch):
    from pydantic import Field

    class Aliased(BaseModel):
        value: int = Field(alias="recordValue")

    root = SequentialAgent([SequentialAgent([Agent(name="leaf", output_schema=Aliased)], name="inner")], name="outer")
    case = {"id": "nested", "request": "go", "expected_outcome": "correct", "steps": [
        {"path": ["outer", "inner", "leaf"], "tools": [], "expected_output": {"recordValue": 1}}]}
    model = Model(messages=iter([AIMessage(content='{"recordValue":1}')]))
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **kw: model)
    assert evaluate_fixtures(root, model="fake", fixtures=[case]).cases[0].outcome == "correct"


def test_runtime_exception_is_safe_and_does_not_contaminate_next_case(monkeypatch):
    root, case = setup(monkeypatch)
    other = copy.deepcopy(case)
    other["id"] = "second"
    first = True
    def fail_once(_):
        nonlocal first
        if first:
            first = False
            raise RuntimeError("secret failure details")
    root._agents[0]._before_agent_callback = fail_once
    report = evaluate_fixtures(root, model="fake", fixtures=[case, other])
    assert [c.outcome for c in report.cases] == ["wrong", "correct"]
    assert report.cases[0].response == ""
    assert "secret failure details" not in repr(report)


@pytest.mark.parametrize("output", [
    "plain text",
    '{"availability":"unavailable","capability":"source"}',
    [{"type": "text", "text": '{"availability":"unavailable","capability":"source"}'}],
    ["first", {"type": "text", "text": "second"}],
    {"value": 1},
    [1, 2],
])
def test_replay_preserves_native_tool_content_and_unavailability(output):
    from types import SimpleNamespace
    from langchain_core.tools import StructuredTool
    from apx_agent._eval_fixtures import _FixtureReplay
    from apx_agent._step_contract import StepUnavailableError, unavailable_middleware

    def safe_tool() -> Any:
        """Return a synthetic safe value for native serialization comparison."""
        return output

    call = {"name": "safe_tool", "args": {}, "id": "a", "type": "tool_call"}
    native = StructuredTool.from_function(safe_tool).invoke(call)
    replay = _FixtureReplay({"steps": [{"path": ["root", "leaf"], "tools": [
        {"name": "safe_tool", "args": {}, "output": output}]}]})
    request = SimpleNamespace(tool_call=call)
    recorded = replay.middleware(("root", "leaf")).wrap_tool_call(request, None)
    assert recorded.content == native.content
    middleware = unavailable_middleware()
    try:
        middleware.wrap_tool_call(request, lambda _: native)
    except StepUnavailableError:
        with pytest.raises(StepUnavailableError):
            middleware.wrap_tool_call(request, lambda _: recorded)
    else:
        assert middleware.wrap_tool_call(request, lambda _: recorded) == recorded
