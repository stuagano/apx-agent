"""Native typed output is executed evidence with exact JSON identity."""

import asyncio
import json
import uuid
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from databricks_agentkit.runtime.auth import RequestAuthContext
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field

from langchain_core.outputs import ChatGeneration, ChatResult

from apx_agent import AgentConfig, Dependencies, LlmAgent, SequentialAgent
from apx_agent._compile import _wrap_agent_node
from apx_agent._native_input import native_data_part
from apx_agent._native_remote import _NATIVE_INVOCATION
from apx_agent._state_proxy import StateProxy
from tests.test_runtime_targets import ToolModel, _write_native_pyproject, execution  # noqa: F401
from tests.test_native_invocation_contract import headers

ARTIFACTS = "apx_native_artifacts"


class Packet(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    amount: int = Field(alias="value")
    day: date = date(2026, 1, 1)


def run_leaf(payload, *, native=True, part=None, tool=False, messages=None, history=None):
    def record() -> dict:
        """Return governed evidence."""
        return payload

    agent = LlmAgent(name="intelligence", output_schema=Packet, output_key="signal",
                     tools=[record], **({"output_tool": "record"} if tool else {}))
    final = AIMessage(content=json.dumps(payload), additional_kwargs={"apx_data_parts": [part]} if part else {})
    emitted = []

    async def invoke(state):
        return {**state, "messages": [*state["messages"], *(messages or []), final]}

    runnable = _wrap_agent_node(agent, AsyncMock(ainvoke=invoke), templated=False)
    token = _NATIVE_INVOCATION.set({"id": "0b2540da-dbe8-438a-b3a3-dad3250da177", "sender": "fleet", "calls": {}, "emit": emitted.append} if native else None)
    try:
        result = asyncio.run(runnable.ainvoke({"messages": [*(history or []), HumanMessage(content="run")], "state": {}}))
        return SimpleNamespace(result=result, events=emitted)
    finally:
        _NATIVE_INVOCATION.reset(token)


def test_native_local_stamps_and_retains_original_json_without_defaults_or_alias_normalization():
    payload = {"amount": 4}
    run = run_leaf(payload)
    part = run.result["messages"][-1].additional_kwargs["apx_data_parts"][0]
    assert part == native_data_part(Packet, payload)
    assert run.result["state"]["signal"] == payload
    assert run.result["state"][ARTIFACTS]["signal"] == part
    assert run.events[-1]["type"] == "agent.step.completed"
    assert run.events[-1]["location"] == "local"
    assert "child_invocation_id" not in run.events[-1]
    run.result["state"]["signal"]["amount"] = 999
    assert run.result["state"][ARTIFACTS]["signal"] == part
    assert part["data"] == {"amount": 4}


def test_native_keeps_verified_custom_identity_and_unmodified_json():
    part = native_data_part(Packet, {"amount": 4})
    part["metadata"]["artifact_id"] = "custom-signal:4"
    run = run_leaf(part["data"], part=part)
    assert run.result["messages"][-1].additional_kwargs["apx_data_parts"] == [part]


@pytest.mark.parametrize("change", ["digest", "missing_digest", "extra_metadata", "content", "extra_part"])
def test_native_rejects_supplied_bad_identity(change):
    part = native_data_part(Packet, {"amount": 4})
    if change == "digest":
        part["metadata"]["sha256"] = "0" * 64
    elif change == "missing_digest":
        del part["metadata"]["sha256"]
    elif change == "extra_metadata":
        part["metadata"]["credential"] = "untrusted"
    elif change == "content":
        part["data"]["amount"] = 5
    else:
        part["extra"] = "untrusted"
    with pytest.raises(ValueError):
        run_leaf(part["data"], part=part)


def test_general_a2a_metadata_keeps_legacy_compatibility():
    part = {"kind": "data", "data": {"amount": 4}, "metadata": {"custom": "allowed"}}
    run = run_leaf(part["data"], native=False, part=part)
    assert run.result["messages"][-1].additional_kwargs["apx_data_parts"][0]["metadata"] == part["metadata"]
    assert run.events == []


def test_normalized_content_digest_cannot_replace_original_identity():
    normalized = native_data_part(Packet, {"value": 4, "day": "2026-01-01"})
    normalized["data"] = {"amount": 4}
    with pytest.raises(ValueError):
        run_leaf(normalized["data"], part=normalized)


@pytest.mark.parametrize("slot", ["input_key", "output_key"])
def test_framework_artifact_map_cannot_be_declared_as_domain_slot(slot):
    args = {slot: ARTIFACTS}
    if slot == "input_key":
        args["input_schema"] = Packet
    with pytest.raises(ValueError, match="reserved"):
        LlmAgent(**args)


def test_state_tools_cannot_overwrite_or_mutate_verified_map():
    source = {ARTIFACTS: {"signal": native_data_part(Packet, {"amount": 4})}}
    expected = deepcopy(source)
    proxy = StateProxy(source)
    proxy[ARTIFACTS]["signal"]["data"]["amount"] = 900
    assert source == expected
    with pytest.raises(ValueError, match="reserved"):
        proxy[ARTIFACTS] = {}
    with pytest.raises(ValueError, match="reserved"):
        del proxy[ARTIFACTS]


def executed(amount=4, call_id="actual"):
    return [AIMessage(content="", tool_calls=[{"name": "record", "args": {}, "id": call_id}]),
            ToolMessage(content=json.dumps({"amount": amount}), name="record", tool_call_id=call_id)]


def test_authoritative_output_uses_executed_tool_not_schema_valid_model_rewrite():
    run = run_leaf({"amount": 900}, tool=True, messages=executed())
    assert run.result["state"]["signal"] == {"amount": 4}
    assert run.result["messages"][-1].additional_kwargs["apx_data_parts"][0] == native_data_part(Packet, {"amount": 4})


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
@pytest.mark.parametrize("mixed", [False, True])
def test_authoritative_completion_needs_one_model_call_and_preserves_tool_state(mode, mixed, monkeypatch):
    from apx_agent import compile_to_langgraph
    from langchain_core.callbacks import BaseCallbackHandler

    calls, ended, tool_started, tool_ended, completed = [], [], [], [], []

    class CallCounter(BaseCallbackHandler):
        def on_chat_model_start(self, serialized, messages, **kwargs):
            calls.append(kwargs["run_id"])

        def on_llm_end(self, response, **kwargs):
            ended.append(kwargs["run_id"])

        def on_tool_start(self, serialized, input_str, **kwargs):
            tool_started.append(kwargs["run_id"])

        def on_tool_end(self, output, **kwargs):
            tool_ended.append(kwargs["run_id"])

    def record(value: str, state: Dependencies.State) -> str:
        """Record exact governed evidence."""
        state["written"] = value
        return '{"amount":4}'

    effects = []

    def helper() -> str:
        """Finish an accompanying operation."""
        effects.append("helper")
        return "done"

    class BatchModel(ToolModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            reply = result.generations[0].message
            if mixed and reply.tool_calls:
                reply.tool_calls.append({"name": "helper", "args": {}, "id": "helper-call", "type": "tool_call"})
            return result

    monkeypatch.setattr("apx_agent._compile._build_chat_databricks", lambda *a, **k: BatchModel())
    agent = LlmAgent(name="proof", tools=[record, helper], output_schema=Packet,
                     output_key="signal", output_tool="record", max_iterations=1,
                     after_agent_callback=completed.append)
    graph = compile_to_langgraph(agent, ws=None, model="offline")
    inputs = {"messages": [*executed(900, "prior"), HumanMessage(content="record")], "state": {}}
    config = {"callbacks": [CallCounter()]}

    async def invoke():
        if mode == "stream":
            results = [value async for value in graph.astream(inputs, config, stream_mode="values")]
            return results[-1]
        return await graph.ainvoke(inputs, config)

    result = graph.invoke(inputs, config) if mode == "sync" else asyncio.run(invoke())
    assert len(calls) == 1
    assert ended == calls
    assert len(tool_started) == (2 if mixed else 1)
    assert set(tool_ended) == set(tool_started)
    assert completed == ['{"value": 4, "day": "2026-01-01"}']
    assert effects == (["helper"] if mixed else [])
    assert result["state"]["written"] == "proof"
    assert result["state"]["signal"] == {"value": 4, "day": "2026-01-01"}
    final = result["messages"][-1]
    assert isinstance(final, AIMessage) and not final.tool_calls
    assert final.usage_metadata is None
    assert final.response_metadata == {}
    assert final.id != result["messages"][3].id
    assert sum(message.usage_metadata["total_tokens"] for message in result["messages"]
               if isinstance(message, AIMessage) and message.usage_metadata) == 2


@pytest.mark.parametrize("failure", ["missing", "duplicate", "error", "schema", "guardrail", "denied", "content_blocks"])
def test_authoritative_failure_never_completes_or_calls_model_again(failure, monkeypatch):
    from apx_agent import compile_to_langgraph
    from apx_agent._errors import ToolError

    calls, completed, tool_effects = [], [], []

    def record(value: str) -> str | list[dict[str, str]]:
        """Return exact evidence or an explicit tool error."""
        tool_effects.append(value)
        if failure == "error":
            raise ToolError("unavailable")
        if failure == "content_blocks":
            return [{"type": "text", "text": '{"amount":4}'}]
        return '{"amount":"invalid"}' if failure == "schema" else '{"amount":4}'

    class FailureModel(ToolModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            calls.append("model")
            if failure == "missing":
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content='{"amount":900}'))])
            reply = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            if failure == "duplicate" and reply.generations[0].message.tool_calls:
                reply.generations[0].message.tool_calls.append({"name": "record", "args": {"value": "duplicate"}, "id": "duplicate", "type": "tool_call"})
            return reply

    def deny(*args):
        raise PermissionError("denied")

    monkeypatch.setattr("apx_agent._compile._build_chat_databricks", lambda *a, **k: FailureModel())
    agent = LlmAgent(name="proof", tools=[record], output_schema=Packet, output_key="signal",
                     output_tool="record", after_agent_callback=completed.append,
                     before_tool=deny if failure == "denied" else None,
                     output_guardrails=[lambda text: "blocked"] if failure == "guardrail" else [])
    graph = compile_to_langgraph(agent, ws=None, model="offline")
    with pytest.raises(ValueError):
        asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="record")]}))
    assert calls == ["model"]
    assert completed == []
    if failure in {"missing", "denied"}:
        assert tool_effects == []


def test_normal_tool_agent_still_generates_final_model_answer(monkeypatch):
    from apx_agent import compile_to_langgraph

    def record(value: str) -> str:
        """Record a value."""
        return value

    monkeypatch.setattr("apx_agent._compile._build_chat_databricks", lambda *a, **k: ToolModel())
    graph = compile_to_langgraph(LlmAgent(tools=[record]), ws=None, model="offline")
    result = graph.invoke({"messages": [HumanMessage(content="record")]})
    assert result["messages"][-1].content == "Recorded proof"
    assert len([message for message in result["messages"] if isinstance(message, AIMessage)]) == 2


def test_authoritative_gate_allows_helper_rounds_and_isolates_concurrent_invocations(monkeypatch):
    from apx_agent import compile_to_langgraph

    calls = []

    def helper() -> str:
        """Prepare the output tool."""
        return "ready"

    async def record(state: Dependencies.State) -> str:
        """Return the invocation's evidence after yielding to another invocation."""
        await asyncio.sleep(0)
        return json.dumps({"amount": state["amount"]})

    class PreparingModel(ToolModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            calls.append("model")
            if isinstance(messages[-1], ToolMessage):
                assert messages[-1].name == "helper", "No generation after authoritative output"
                name = "record"
            else:
                name = "helper"
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content="", tool_calls=[{"name": name, "args": {}, "id": str(uuid.uuid4())}],
            ))])

    monkeypatch.setattr("apx_agent._compile._build_chat_databricks", lambda *a, **k: PreparingModel())
    graph = compile_to_langgraph(LlmAgent(tools=[helper, record], output_schema=Packet,
        output_key="signal", output_tool="record", max_iterations=2), ws=None, model="offline")

    async def run():
        return await asyncio.gather(*(graph.ainvoke({
            "messages": [HumanMessage(content="record")], "state": {"amount": amount},
        }) for amount in (4, 8)))

    results = asyncio.run(run())
    assert [result["state"]["signal"] for result in results] == [
        {"value": 4, "day": "2026-01-01"}, {"value": 8, "day": "2026-01-01"},
    ]
    assert calls == ["model"] * 4


def test_prior_leaf_tool_history_cannot_satisfy_current_execution():
    with pytest.raises(ValueError):
        run_leaf({"amount": 900}, tool=True, history=executed())


@pytest.mark.parametrize("messages", [[], [ToolMessage(content='{"amount":4}', tool_call_id="stale")],
                                      executed() + executed(5, "conflict"),
                                      [*executed()[:-1], ToolMessage(content='{"amount":4}', tool_call_id="actual", status="error")]])
def test_authoritative_output_requires_one_successful_current_execution(messages):
    with pytest.raises(ValueError):
        run_leaf({"amount": 900}, tool=True, messages=messages)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: int
    status: str
    result: dict | None = None


@pytest.mark.parametrize("location", ["local", "remote"])
@pytest.mark.parametrize("path,payload,expected", [
    (None, {"status": "completed", "result": {"status": "not_ready"}}, "completed"),
    (("result", "status"), {"status": "completed", "result": {"status": "not_ready"}}, "not_ready"),
    (("decision", "recommendation", "status"), {"decision": {"recommendation": {"status": "evidence_gap"}}}, "evidence_gap"),
    (("result", "status"), {"status": "completed"}, None),
    (("result", "status"), {"result": None}, None),
    (("result", "status"), {"result": {"status": "unsafe status"}}, None),
])
def test_declared_nested_domain_status_is_bounded_and_does_not_change_artifact(location, path, payload, expected):
    from apx_agent._native_remote import emit_native_step

    agent = LlmAgent(name="step", output_schema=Evidence, output_status_path=path)
    part = {"kind": "data", "data": payload, "metadata": {"artifact_id": "actual", "sha256": "a" * 64}}
    original = deepcopy(part)
    events = []
    child = {"id": "actual-child", "parent_invocation_id": "actual-parent",
             "child_invocation_url": "https://child.example/api/invocations/actual-child"} if location == "remote" else None
    token = _NATIVE_INVOCATION.set({"id": "actual-parent", "sender": "fleet", "emit": events.append})
    try:
        emit_native_step(agent, part, None, child, "predecessor")
    finally:
        _NATIVE_INVOCATION.reset(token)
    assert events[0]["domain_status"] == expected
    assert events[0]["location"] == location
    assert events[0]["transport_status"] == "completed"
    assert part == original


@pytest.mark.parametrize("path", [(), [], ("",), ("unsafe.name",), ("x" * 65,), (1,), ("status",) * 9])
def test_output_status_path_rejects_invalid_declarations(path):
    with pytest.raises(ValueError, match="output_status_path"):
        LlmAgent(output_schema=Evidence, output_status_path=path)


def test_output_status_path_requires_typed_output():
    with pytest.raises(ValueError, match="output_status_path"):
        LlmAgent(output_status_path=("status",))


class HopInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: dict
    predecessor: dict


@pytest.fixture
def chain(execution, monkeypatch, tmp_path):  # noqa: F811
    from apx_agent._durable_agent import compile_to_durable_agent_server
    from apx_agent._wiring import _apply_remote_leaf_bindings

    controls = {}
    consumed, principals, prepared, sent = [], [], [], []

    class RewritingModel(ToolModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if controls.get("skip") or isinstance(messages[-1], ToolMessage):
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content='{"amount":900,"status":"ready"}'))])
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    monkeypatch.setattr("apx_agent._compile._build_chat_databricks", lambda *a, **k: RewritingModel())

    def user_client(auth, mode):
        assert mode == "user"
        client = MagicMock()
        client.current_user.me.return_value.id = auth.namespace("principal", "verified")
        client.config.authenticate.return_value = {"Authorization": f"Bearer {auth._token}"}
        return client

    monkeypatch.setattr(RequestAuthContext, "client_for", user_client)
    _write_native_pyproject(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")

    def leaf(name, schema, key):
        def record(value: str, state: Dependencies.State, principal: Dependencies.Principal) -> str:
            """Consume governed input and produce exact evidence."""
            packet = state[key]
            consumed.append((name, deepcopy(packet)))
            principals.append(principal)
            amount = packet["amount"] if name == "intelligence" else packet["predecessor"]["data"]["amount"]
            payload = {"amount": amount + 1, "status": "not_ready"}
            if controls.get("nested_status"):
                payload["result"] = {"status": "evidence_gap"}
            return json.dumps(payload)

        return LlmAgent(name=name, tools=[record], input_schema=schema, input_key=key,
                        output_schema=Evidence, output_key=name, output_tool="record")

    intelligence = leaf("intelligence", Packet, "request")
    calibrate = leaf("calibrate", HopInput, "calibrate_input")
    compound = leaf("compound", HopInput, "compound_input")
    apps = {agent._name: compile_to_durable_agent_server(agent, model="test", service_ws=execution.ws, require_user=True)
            for agent in (calibrate, compound)}

    def prepare(predecessor):
        def next_input(state):
            prepared.append(deepcopy(state))
            part = state[ARTIFACTS][predecessor]
            assert part == native_data_part(Evidence, part["data"], part)
            payload = {"request": state["request"], "predecessor": part}
            state[ARTIFACTS] = {"forged": {}}
            return payload
        return next_input

    bound_calibrate = LlmAgent(name="calibrate", input_schema=HopInput, input_key="calibrate_input", output_schema=Evidence,
                              output_key="calibrate", input_preparation=prepare("intelligence"))
    bound_compound = LlmAgent(name="compound", input_schema=HopInput, input_key="compound_input", output_schema=Evidence,
                             output_key="compound", input_preparation=prepare("calibrate"))
    root = SequentialAgent([intelligence, bound_calibrate, bound_compound], name="fleet")
    config = AgentConfig(name="fleet", bindings={name: {"transport": "native", "url": f"https://{name}.example"}
                                                for name in apps})
    _apply_remote_leaf_bindings(root, config.bindings)
    apps["fleet"] = compile_to_durable_agent_server(root, model="test", service_ws=execution.ws, require_user=True)
    monkeypatch.delenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL")
    monkeypatch.setenv("DATABRICKS_APP_NAME", "native-contract")
    original_client = httpx.AsyncClient
    with ExitStack() as stack:
        clients = {name: stack.enter_context(TestClient(app)) for name, app in apps.items()}

        async def route(request):
            sent.append(request)
            name = request.url.host.split(".")[0]
            if controls.get("readback_fail") and request.method == "GET":
                return httpx.Response(503)
            if controls.get("timeout"):
                await asyncio.sleep(1)
            response = await asyncio.to_thread(clients[name].request, request.method, request.url.path,
                                              content=request.content, headers={**headers(), "Content-Type": "application/json"})
            if response.status_code == 200 and not request.url.path.endswith("events") and controls.get("tamper"):
                record = response.json()
                part = record["output"]["messages"][-1]["additional_kwargs"]["apx_data_parts"][0]
                controls["tamper"](part)
                return httpx.Response(200, json=record)
            return httpx.Response(response.status_code, content=response.content, headers=response.headers)

        monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: original_client(**{**kwargs, "transport": httpx.MockTransport(route)}))
        yield SimpleNamespace(clients=clients, apps=apps, root=root, consumed=consumed, principals=principals,
                              prepared=prepared, sent=sent, controls=controls)


def chain_request():
    return {"id": str(uuid.uuid4()), "input": {"messages": [{"role": "user", "content": "record"}],
            "apx_data_parts": [native_data_part(Packet, {"amount": 4})]}}


def chain_events(chain, identifier):
    response = chain.clients["fleet"].get(f"/api/invocations/{identifier}/events", headers=headers())
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_three_leaf_chain_executes_exact_tools_prepares_wrappers_and_reads_both_real_children(chain):
    body = chain_request()
    started = datetime.now(timezone.utc)
    response = chain.clients["fleet"].post("/api/invocations", json=body, headers=headers())
    assert response.status_code == 200, response.text
    assert [name for name, _ in chain.consumed] == ["intelligence", "calibrate", "compound"]
    assert len(chain.principals) == 3 and len(set(chain.principals)) == 1
    assert len(chain.prepared) == 2
    assert chain.prepared[0]["request"] == {"amount": 4}
    assert chain.prepared[0][ARTIFACTS] == {"intelligence": native_data_part(Evidence, {"amount": 5, "status": "not_ready"})}
    assert set(chain.prepared[1][ARTIFACTS]) == {"intelligence", "calibrate"}
    posts = [json.loads(request.content) for request in chain.sent if request.method == "POST"]
    assert len(posts) == 2 and posts[0]["id"] != posts[1]["id"]
    for index, (child, predecessor) in enumerate(zip(posts, ("intelligence", "calibrate"), strict=True)):
        assert child["input"]["correlation"] == {"parent_invocation_id": body["id"], "transport_sender": "fleet", "logical_predecessor": predecessor}
        assert child["input"]["apx_data_parts"][0]["data"] == chain.consumed[index + 1][1]
        assert child["input"]["apx_data_parts"][0]["data"]["predecessor"] == chain.prepared[index][ARTIFACTS][predecessor]
        assert any(request.method == "GET" and request.url.path == f"/api/invocations/{child['id']}" for request in chain.sent)
        assert any(request.method == "GET" and request.url.path == f"/api/invocations/{child['id']}/events" for request in chain.sent)
    steps = [event for event in chain_events(chain, body["id"]) if event["type"] == "agent.step.completed"]
    assert [step["step"] for step in steps] == ["intelligence", "calibrate", "compound"]
    assert [step["location"] for step in steps] == ["local", "remote", "remote"]
    assert [step["logical_predecessor"] for step in steps] == [None, "intelligence", "calibrate"]
    assert all(step["domain_status"] == "not_ready" and step["transport_status"] == "completed" for step in steps)
    assert "child_invocation_id" not in steps[0]
    assert [step["child_invocation_id"] for step in steps[1:]] == [post["id"] for post in posts]
    assert [step["child_invocation_url"] for step in steps[1:]] == [
        str(request.url) + "/" + json.loads(request.content)["id"] for request in chain.sent if request.method == "POST"]
    assert all(step["readback_verified"] for step in steps[1:])
    assert all(step["timestamp_source"] == "application_emit" for step in steps)
    assert all(started <= datetime.fromisoformat(step["recorded_at"]) <= datetime.now(timezone.utc) for step in steps)
    assert steps == [event for event in chain_events(chain, body["id"]) if event["type"] == "agent.step.completed"]
    for name, app in chain.apps.items():
        saved = json.dumps([asdict(event) for event in app._runtime.runtime_store.persisted_events])
        assert "credential-sentinel" not in saved
        identifier = body["id"] if name == "fleet" else posts[0 if name == "calibrate" else 1]["id"]
        assert chain.clients[name].get(f"/api/invocations/{identifier}", headers=headers("bob")).status_code == 404
    assert chain.clients["fleet"].post("/api/invocations", json=body, headers=headers()).status_code == 200
    assert len(chain.consumed) == 3


@pytest.mark.parametrize("tamper", [
    lambda part: part.pop("metadata"),
    lambda part: part["metadata"].pop("sha256"),
    lambda part: part["metadata"].update(sha256="0" * 64),
    lambda part: part["metadata"].update(secret="forged"),
    lambda part: part["data"].update(amount=800),
    lambda part: part.update(extra="forged"),
    lambda part: part["data"].update(status="x" * 65536),
    lambda part: part["data"].update(amount="5"),
])
def test_remote_invalid_output_never_publishes_success_or_calls_next_child(chain, tamper):
    chain.controls["tamper"] = tamper
    body = chain_request()
    response = chain.clients["fleet"].post("/api/invocations", json=body, headers=headers())
    assert response.status_code == 500
    assert [name for name, _ in chain.consumed] == ["intelligence", "calibrate"]
    steps = [event for event in chain_events(chain, body["id"]) if event["type"] == "agent.step.completed"]
    assert [step["step"] for step in steps] == ["intelligence"]


def test_three_leaf_chain_reports_declared_nested_status_for_local_and_remote_outputs(chain):
    chain.controls["nested_status"] = True
    declaration = LlmAgent(output_schema=Evidence, output_status_path=("result", "status"))
    for leaf in chain.root._agents:
        leaf._output_status_path = declaration._output_status_path
    body = chain_request()
    response = chain.clients["fleet"].post("/api/invocations", json=body, headers=headers())
    assert response.status_code == 200, response.text
    steps = [event for event in chain_events(chain, body["id"]) if event["type"] == "agent.step.completed"]
    assert [step["location"] for step in steps] == ["local", "remote", "remote"]
    assert all(step["domain_status"] == "evidence_gap" and step["transport_status"] == "completed" for step in steps)
    part = response.json()["output"]["messages"][-1]["additional_kwargs"]["apx_data_parts"][0]
    assert part == native_data_part(Evidence, part["data"], part)
    assert part["data"]["result"] == {"status": "evidence_gap"}


@pytest.mark.parametrize("failure", ["skip", "readback_fail", "timeout"])
def test_skipped_tool_and_failed_or_unknown_readback_never_publish_remote_success(chain, failure):
    chain.controls[failure] = True
    if failure == "timeout":
        bindings = chain.root._apx_remote_leaf_bindings
        chain.root._apx_remote_leaf_bindings = {**bindings, "calibrate": replace(bindings["calibrate"], timeout_s=0.01)}
    body = chain_request()
    response = chain.clients["fleet"].post("/api/invocations", json=body, headers=headers())
    assert response.status_code == 500
    events = chain_events(chain, body["id"])
    assert not any(event["type"] == "agent.step.completed" and event["location"] == "remote" for event in events)
    if failure == "skip":
        assert chain.consumed == [] and chain.sent == []
    if failure == "readback_fail":
        assert any(event["type"] == "agent.child.accepted" for event in events)
    if failure == "timeout":
        assert any(event["type"] == "agent.child.unknown" and event["transport_status"] == "unknown" for event in events)
        unknown = next(event for event in events if event["type"] == "agent.child.unknown")
        sent = next(request for request in chain.sent if request.method == "POST")
        assert unknown["child_invocation_url"] == str(sent.url) + "/" + json.loads(sent.content)["id"]


def test_remote_valid_custom_id_survives_in_preparation_and_event(chain):
    chain.controls["tamper"] = lambda part: part["metadata"].update(artifact_id="external:verified-custom")
    body = chain_request()
    assert chain.clients["fleet"].post("/api/invocations", json=body, headers=headers()).status_code == 200
    part = chain.prepared[1][ARTIFACTS]["calibrate"]
    assert part["metadata"]["artifact_id"] == "external:verified-custom"
    assert chain.consumed[2][1]["predecessor"] == part
    steps = [event for event in chain_events(chain, body["id"]) if event["type"] == "agent.step.completed"]
    assert steps[1]["output_artifact"] == part["metadata"]


def test_native_artifacts_are_fresh_for_each_request(chain):
    for _ in range(2):
        assert chain.clients["fleet"].post("/api/invocations", json=chain_request(), headers=headers()).status_code == 200
    assert set(chain.prepared[2][ARTIFACTS]) == {"intelligence"}


@pytest.mark.parametrize("payload", [{"amount": "4"}, {"amount": 4, "extra": True}, {"amount": 4, "day": "x" * 65536}])
def test_local_invalid_output_fails_before_publication(payload):
    with pytest.raises(ValueError):
        run_leaf(payload)


def test_preparation_requires_native_binding_and_valid_callable():
    from apx_agent._compile import compile_to_langgraph

    with pytest.raises(ValueError, match="callable"):
        LlmAgent(input_schema=Packet, input_key="request", input_preparation="code")
    agent = LlmAgent(input_schema=Packet, input_key="request", input_preparation=lambda state: {})
    with pytest.raises(ValueError, match="bound native"):
        compile_to_langgraph(agent, ws=MagicMock(), model="test")


@pytest.mark.parametrize("value", [None, {}, {"request": {}, "predecessor": None}])
def test_bad_preparation_fails_before_remote_call(chain, value):
    chain.root._agents[1]._input_preparation = lambda state: value
    response = chain.clients["fleet"].post("/api/invocations", json=chain_request(), headers=headers())
    assert response.status_code == 500
    assert [name for name, _ in chain.consumed] == ["intelligence"]
    assert chain.sent == []


def _emit_fields(path, payload):
    from apx_agent._native_remote import emit_native_step

    agent = LlmAgent(name="step", output_schema=Evidence, output_fields_path=path)
    part = {"kind": "data", "data": payload, "metadata": {"artifact_id": "actual", "sha256": "a" * 64}}
    events = []
    token = _NATIVE_INVOCATION.set({"id": "actual-parent", "sender": "fleet", "emit": events.append})
    try:
        emit_native_step(agent, part, None, None, "predecessor")
    finally:
        _NATIVE_INVOCATION.reset(token)
    return events[0]


def test_declared_fields_ride_on_the_step_event():
    # A step's small key/value summary (e.g. which slots are filled) is shown on its card.
    event = _emit_fields(("view",), {"view": {"size": "large", "color": "blue", "date": None, "n": 5}})
    assert event["fields"] == {"size": "large", "color": "blue", "date": None, "n": "5"}


@pytest.mark.parametrize("payload", [
    {"view": "not a dict"}, {"view": {}}, {}, {"view": {"bad key!": "x"}},
    {"view": {f"k{i}": "v" for i in range(9)}},
])
def test_unusable_fields_are_not_published(payload):
    assert "fields" not in _emit_fields(("view",), payload)


def test_field_values_are_bounded():
    event = _emit_fields(("view",), {"view": {"note": "x" * 100, "nested": {"a": 1}}})
    assert event["fields"]["note"] == "x" * 39 + "…"
    assert event["fields"]["nested"] is None  # only scalars are shown


def test_output_fields_path_is_validated():
    with pytest.raises(ValueError, match="output_fields_path"):
        LlmAgent(output_schema=Evidence, output_fields_path=("bad.name",))
    with pytest.raises(ValueError, match="output_fields_path"):
        LlmAgent(output_fields_path=("view",))
