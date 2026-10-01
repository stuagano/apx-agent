"""Evaluate current models with strictly recorded, invocation-local tool results."""

from __future__ import annotations

import json
import math
import threading
import time
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from ._agents import BaseAgent, LlmAgent, SequentialAgent
from ._inspection import _EmptyToolInput, _inspect_tool_fn, _make_input_model, _state_param_name

if TYPE_CHECKING:
    from ._eval_chain import ChainEvalReport


def _json(value: Any) -> str:
    """Canonical JSON without Python-only coercions or nonfinite numbers."""
    if value is None or type(value) in {str, bool, int}:
        pass
    elif type(value) is float and math.isfinite(value):
        pass
    elif type(value) is list:
        for item in value:
            _json(item)
    elif type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json(item)
    else:
        raise ValueError("Invalid fixture: expected JSON data")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _leaves(agent: BaseAgent, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], LlmAgent]:
    if type(agent) not in {LlmAgent, SequentialAgent}:
        raise ValueError("Invalid fixture graph: only local sequential leaves are supported")
    assert isinstance(agent, (LlmAgent, SequentialAgent))
    if getattr(agent, "_apx_remote_leaf_bindings", None):
        raise ValueError("Invalid fixture graph: remote bindings are unsupported")
    name = agent._name
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Invalid fixture graph: every agent must have a name")
    path = (*prefix, name)
    if isinstance(agent, SequentialAgent):
        names = [getattr(child, "_name", None) for child in agent._agents]
        if len(set(names)) != len(names):
            raise ValueError("Invalid fixture graph: duplicate sibling names")
        result = {}
        for child in agent._agents:
            result.update(_leaves(child, path))
        return result
    if agent._sub_agent_urls or agent._tool_loading != "eager":
        raise ValueError("Invalid fixture graph: remote or deferred tools are unsupported")
    if agent._output_schema is None:
        raise ValueError("Invalid fixture graph: leaves require output_schema")
    names = [fn.__name__ for fn in agent._tool_fns]
    if len(set(names)) != len(names):
        raise ValueError("Invalid fixture graph: duplicate tool names")
    if any(_state_param_name(fn) is not None for fn in agent._tool_fns):
        raise ValueError("Invalid fixture graph: state-dependent tools cannot replay state mutations")
    return {path: agent}


def _validate(agent: BaseAgent, fixtures: Any) -> list[dict[str, Any]]:
    leaves = _leaves(agent)
    if not isinstance(agent, SequentialAgent):
        raise ValueError("Invalid fixture graph: a named SequentialAgent is required")
    _json(fixtures)
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("Invalid fixture: expected a nonempty list of cases")
    cases = deepcopy(fixtures)
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or not {"id", "request", "steps", "expected_outcome"} <= case.keys() or case.keys() - {"id", "request", "steps", "expected_outcome", "expected_failure"}:
            raise ValueError("Invalid fixture: case fields")
        if not isinstance(case["id"], str) or not case["id"] or case["id"] in ids:
            raise ValueError("Invalid fixture: unique nonempty case id required")
        ids.add(case["id"])
        if not isinstance(case["request"], str) or not case["request"].strip():
            raise ValueError("Invalid fixture: nonempty request required")
        outcome = case["expected_outcome"]
        if not isinstance(outcome, str) or outcome not in {"correct", "escalated_with_evidence"}:
            raise ValueError("Invalid fixture: expected_outcome")
        failure = case.get("expected_failure")
        if outcome == "correct" and "expected_failure" in case:
            raise ValueError("Invalid fixture: correct case cannot expect failure")
        if outcome == "escalated_with_evidence":
            if not isinstance(failure, dict) or set(failure) != {"step", "reason"} or not isinstance(failure["reason"], str) or failure["reason"] not in {"timeout", "schema_miss", "unavailable"}:
                raise ValueError("Invalid fixture: expected_failure")
            if not isinstance(failure["step"], list) or not all(isinstance(v, str) for v in failure["step"]) or tuple(failure["step"]) not in leaves:
                raise ValueError("Invalid fixture: failure path")
        steps = case["steps"]
        if not isinstance(steps, list) or not steps:
            raise ValueError("Invalid fixture: nonempty steps required")
        paths = []
        for step in steps:
            if not isinstance(step, dict) or not {"path", "tools"} <= step.keys() or step.keys() - {"path", "tools", "expected_output"}:
                raise ValueError("Invalid fixture: step fields")
            if not isinstance(step["path"], list) or not all(isinstance(v, str) for v in step["path"]):
                raise ValueError("Invalid fixture: step path")
            path = tuple(step["path"])
            if path not in leaves or path in paths:
                raise ValueError("Invalid fixture: unknown or repeated step path")
            paths.append(path)
            failed = failure is not None and step["path"] == failure["step"]
            if failed == ("expected_output" in step):
                raise ValueError("Invalid fixture: expected_output must describe successful steps only")
            leaf = leaves[path]
            if not failed:
                try:
                    schema = leaf._output_schema
                    assert schema is not None
                    validated = schema.model_validate_json(_json(step["expected_output"]), strict=True)
                    if _json(validated.model_dump(mode="json", by_alias=True)) != _json(step["expected_output"]):
                        raise ValueError("Output changed during schema validation")
                except Exception:
                    raise ValueError("Invalid fixture: expected_output must satisfy the exact output schema") from None
            if not isinstance(step["tools"], list):
                raise ValueError("Invalid fixture: tools must be a list")
            tool_names = {fn.__name__ for fn in leaf._tool_fns}
            for record in step["tools"]:
                if not isinstance(record, dict) or set(record) != {"name", "args", "output"} or not isinstance(record["name"], str) or record["name"] not in tool_names or not isinstance(record["args"], dict):
                    raise ValueError("Invalid fixture: tool record fields or unknown tool")
                fn = next(fn for fn in leaf._tool_fns if fn.__name__ == record["name"])
                signature = _inspect_tool_fn(fn)
                input_schema = _make_input_model(fn, signature.plain_params) or _EmptyToolInput
                try:
                    if record["args"].keys() - signature.plain_params.keys():
                        raise ValueError("Unexpected tool argument")
                    input_schema.model_validate_json(_json(record["args"]), strict=True)
                except Exception:
                    raise ValueError("Invalid fixture: tool arguments must satisfy the input schema") from None
        expected_paths = list(leaves)
        if failure is not None:
            expected_paths = expected_paths[:expected_paths.index(tuple(failure["step"])) + 1]
        if paths != expected_paths:
            raise ValueError("Invalid fixture: steps must cover the exact execution prefix")
    return cases


class _FixtureReplay:
    """Schema-only tools and frozen observations for exactly one case."""

    def __init__(self, case: dict[str, Any]) -> None:
        self.pending = {tuple(step["path"]): deepcopy(step["tools"]) for step in case["steps"]}
        self.outputs: dict[tuple[str, ...], Any] = {}
        self.errors: dict[tuple[str, ...], list[str]] = {}
        self.calls: list[str] = []
        self._closed = False
        self._lock = threading.Lock()

    def tool(self, fn: Any, path: tuple[str, ...]) -> Any:
        from langchain_core.tools import StructuredTool

        schema = _make_input_model(fn, _inspect_tool_fn(fn).plain_params) or _EmptyToolInput

        def forbidden(**kwargs: Any) -> Any:
            raise RuntimeError("Recorded tool replay cannot execute a tool")

        return StructuredTool(name=fn.__name__, description=fn.__doc__ or fn.__name__, args_schema=schema, func=forbidden)

    def middleware(self, path: tuple[str, ...]) -> Any:
        from langchain.agents.middleware import AgentMiddleware
        from langchain_core.tools.base import _format_output

        def replay(request: Any) -> Any:
            call = request.tool_call
            with self._lock:
                if self._closed:
                    raise RuntimeError("Recorded tool invocation is closed")
                self.calls.append(call["name"])
                records = self.pending.get(path, [])
                for index, record in enumerate(records):
                    if record["name"] == call["name"] and _json(record["args"]) == _json(call["args"]):
                        records.pop(index)
                        # Match StructuredTool's native strings, content blocks, and JSON serialization.
                        return _format_output(record["output"], None, call["id"], call["name"], "success")
                self.errors.setdefault(path, []).append("Tool call did not match an unconsumed fixture record")
                raise RuntimeError("Tool call did not match recorded fixture")

        class _ReplayMiddleware(AgentMiddleware):
            def wrap_tool_call(self, request: Any, handler: Any) -> Any:
                return replay(request)

            async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
                return replay(request)

        return _ReplayMiddleware()

    def record(self, path: tuple[str, ...], payload: Any) -> None:
        with self._lock:
            if not self._closed:
                if path in self.outputs:
                    self.errors.setdefault(path, []).append("Step completed more than once")
                self.outputs[path] = json.loads(_json(payload))

    def close(self) -> None:
        with self._lock:
            self._closed = True


def evaluate_fixtures(agent: BaseAgent, *, model: str, fixtures: Any) -> ChainEvalReport:
    """Validate the whole dataset, then run real models with recorded tools."""
    from langchain_core.messages import HumanMessage

    from ._compile import CompileContext, _compile_any
    from ._eval_chain import ChainCaseResult, ChainEvalReport, StepEvalResult
    from ._step_contract import Escalation, get_escalation

    cases = _validate(agent, fixtures)
    leaves = _leaves(agent)
    results = []
    counts = {"correct": 0, "escalated_with_evidence": 0, "wrong": 0}
    for case in cases:
        replay = _FixtureReplay(case)
        start = time.monotonic()
        errors: list[str] = []
        result: dict[str, Any] = {}
        packet = None
        response = ""
        try:
            graph = _compile_any(agent, CompileContext(service_ws=None, user_ws=None, model=model, fixture_replay=replay))
            result = graph.invoke({"messages": [HumanMessage(content=case["request"])]})
        except Exception:
            errors.append("Chain execution failed")
        finally:
            replay.close()
        try:
            if result.get("_apx_escalation") is not None:
                packet = Escalation.model_validate(result["_apx_escalation"]).model_dump(mode="json")
                if get_escalation(result["messages"][-1]) != packet:
                    raise ValueError("Escalation metadata mismatch")
            elif result.get("messages") and get_escalation(result["messages"][-1]) is not None:
                raise ValueError("Unexpected escalation metadata")
        except Exception:
            errors.append("Invalid escalation metadata")
            packet = None
        failure = case.get("expected_failure")
        if failure is None:
            if packet is not None:
                errors.append("Unexpected escalation")
        else:
            expected_evidence = [{"step": list(path), "output_key": leaves[path]._output_key, "data": output} for path, output in replay.outputs.items()]
            if packet is None or packet["failed_step"] != failure["step"] or packet["reason"] != failure["reason"] or not expected_evidence or _json(packet["evidence"]) != _json(expected_evidence):
                errors.append("Escalation did not match expected failure and completed evidence")
        expected_completed = [tuple(step["path"]) for step in case["steps"] if "expected_output" in step]
        if list(replay.outputs) != expected_completed:
            errors.append("Completed steps did not match expected execution order")
        steps = []
        for step in case["steps"]:
            path = tuple(step["path"])
            step_errors = list(replay.errors.get(path, []))
            if replay.pending[path]:
                step_errors.append("Recorded tool calls were not consumed")
            if "expected_output" in step:
                if path not in replay.outputs:
                    step_errors.append("Expected step did not complete")
                elif _json(replay.outputs[path]) != _json(step["expected_output"]):
                    step_errors.append("Validated output did not match expected output")
            elif packet is None or failure is None or packet["failed_step"] != list(path) or packet["reason"] != failure["reason"]:
                step_errors.append("Expected terminal failure was not observed")
            steps.append(StepEvalResult(step=path, expected_output=step.get("expected_output"), actual_output=replay.outputs.get(path), passed=not step_errors, errors=tuple(step_errors)))
        if any(path not in {tuple(s["path"]) for s in case["steps"]} for path in replay.errors):
            errors.append("Unexpected step executed a tool")
        outcome = case["expected_outcome"] if not errors and all(step.passed for step in steps) else "wrong"
        counts[outcome] += 1
        if packet is not None:
            response = _json(packet)
        elif result and replay.outputs:
            response = _json(next(reversed(replay.outputs.values())))
        results.append(ChainCaseResult(request=case["request"], response=response, sub_agents_invoked=(), tool_calls=tuple(replay.calls), duration_ms=int((time.monotonic() - start) * 1000), case_id=case["id"], steps=tuple(steps), outcome=outcome, errors=tuple(errors)))
    return ChainEvalReport(cases=tuple(results), outcome_counts=counts)
