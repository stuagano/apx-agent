"""Declared native remote transport, using real SDK invocation records."""

import pytest
import json
import uuid
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import MagicMock
import httpx
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from fastapi.testclient import TestClient
from databricks_agentkit.runtime.auth import RequestAuthContext
from pydantic import BaseModel, ConfigDict

from apx_agent import AgentConfig, Dependencies, HandoffAgent, KeywordRouter, LlmAgent, LoopAgent, ParallelAgent, RouterAgent, SequentialAgent
from tests.test_runtime_targets import execution, _write_native_pyproject, ToolModel  # noqa: F401
from tests.test_native_invocation_contract import headers


class DomainInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: int


def test_native_input_and_binding_are_explicit_declarations():
    agent = LlmAgent(name="specialist", input_schema=DomainInput, input_key="domain_input")
    assert agent._input_schema is DomainInput
    config = AgentConfig(name="parent", bindings={"specialist": {
        "transport": "native", "url": "https://specialist.example", "timeout_s": 2,
    }})
    assert config.bindings["specialist"].transport == "native"


@pytest.mark.parametrize("key", ["messages", "state", "session_id", "auth", "_apx_input"])
def test_native_input_rejects_reserved_state_slots(key):
    with pytest.raises(ValueError):
        LlmAgent(input_schema=DomainInput, input_key=key)


@pytest.mark.parametrize("transport", ["native", "legacy"])
@pytest.mark.parametrize("depth", [0, 1, 2])
@pytest.mark.parametrize("control, wrap", [
    ("sequence", lambda child: child),
    ("parallel", lambda child: ParallelAgent([child])),
    ("loop", lambda child: LoopAgent(child)),
    ("router", lambda child: RouterAgent([child])),
    ("keyword_branch", lambda child: KeywordRouter([("route", child, ["match"])], LlmAgent(name="fallback"))),
    ("keyword_default", lambda child: KeywordRouter([("route", LlmAgent(name="fallback"), ["match"])], child)),
    ("handoff", lambda child: HandoffAgent([child])),
])
def test_binding_transport_preserves_control_ancestry(transport, depth, control, wrap):
    from apx_agent._wiring import _apply_remote_leaf_bindings

    root = LlmAgent(name="specialist", input_schema=DomainInput, input_key="domain_input")
    for index in range(depth):
        root = SequentialAgent([root], name=f"nested_{index}")
    root = wrap(root)
    reference = {"transport": "native", "url": "https://child.example"} if transport == "native" else "https://child.example/.well-known/agent.json"
    config = AgentConfig(name="parent", bindings={"specialist": reference})
    if transport == "native" and control != "sequence":
        with pytest.raises(ValueError, match="Native bindings require a sequential leaf"):
            _apply_remote_leaf_bindings(root, config.bindings)
        assert not hasattr(root, "_apx_remote_leaf_bindings")
    else:
        _apply_remote_leaf_bindings(root, config.bindings)
        assert root._apx_remote_leaf_bindings["specialist"].transport == ("native" if transport == "native" else "a2a")


@pytest.fixture
def two_hosts(execution, monkeypatch, tmp_path):  # noqa: F811
    consumed = []
    principals = []
    controls = {"invalid": False, "delay": False}

    class DomainModel(ToolModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if isinstance(messages[-1], ToolMessage):
                payload = {"amount": "invalid"} if controls["invalid"] else json.loads(messages[-1].content)
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Typed result", additional_kwargs={
                    "apx_data_parts": [{"kind": "data", "data": payload}],
                }))])
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    monkeypatch.setattr("apx_agent._compile._build_chat_databricks", lambda *a, **k: DomainModel())

    def record(value: str, state: Dependencies.State, principal: Dependencies.Principal) -> str:
        """Consume the declared domain input from governed graph state."""
        consumed.append(state["domain_input"])
        principals.append(principal)
        return json.dumps({"amount": state["domain_input"]["amount"] + 1})

    def user_client(auth, mode):
        assert mode == "user"
        user = MagicMock()
        user.current_user.me.return_value.id = auth.namespace("principal", "verified")
        user.config.authenticate.return_value = {"Authorization": f"Bearer {auth._token}"}
        return user

    monkeypatch.setattr(RequestAuthContext, "client_for", user_client)
    _write_native_pyproject(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    specialist = LlmAgent(name="specialist", tools=[record], input_schema=DomainInput, input_key="domain_input",
                          output_schema=DomainInput, output_key="domain_result")
    specialist._after_model = lambda output: output
    from apx_agent._durable_agent import compile_to_durable_agent_server
    saver = InMemorySaver()
    child = compile_to_durable_agent_server(specialist, model="test", service_ws=execution.ws, require_user=True, checkpointer=saver)
    root = SequentialAgent(name="parent", agents=[specialist])
    from apx_agent._wiring import _apply_remote_leaf_bindings
    config = AgentConfig(name="parent", bindings={"specialist": {"transport": "native", "url": "https://child.example"}})
    _apply_remote_leaf_bindings(root, config.bindings)
    parent = compile_to_durable_agent_server(root, model="test", service_ws=execution.ws)
    assert parent.auth_policy.requires_user
    monkeypatch.delenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL")
    monkeypatch.setenv("DATABRICKS_APP_NAME", "native-contract")
    sent = []
    original_client = httpx.AsyncClient
    with TestClient(child) as child_client, TestClient(parent) as parent_client:
        async def route(request):
            import asyncio
            sent.append(request)
            # Model trusted Apps ingress: authenticated bearer maps to this user.
            bearer = request.headers["Authorization"]
            assert bearer in {"Bearer contract-credential-sentinel", "Bearer bob-credential-sentinel"}
            principal = "bob" if bearer == "Bearer bob-credential-sentinel" else "alice"
            if controls["delay"]:
                await asyncio.sleep(1)
            response = await asyncio.to_thread(child_client.request, request.method, request.url.path, content=request.content,
                                              headers={**user_headers(principal), "Content-Type": "application/json"})
            assert response.status_code != 422, response.text
            return httpx.Response(response.status_code, content=response.content, headers=response.headers)

        monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: original_client(**{**kwargs, "transport": httpx.MockTransport(route)}))
        yield SimpleNamespace(parent=parent_client, child=child_client, parent_app=parent, child_app=child,
                              consumed=consumed, sent=sent, specialist=specialist, controls=controls,
                              principals=principals, root=root, saver=saver)


def user_headers(principal="alice"):
    result = headers(principal)
    if principal == "bob":
        result["X-Forwarded-Access-Token"] = "bob-credential-sentinel"
    return result


def domain_request(amount=7):
    from apx_agent._native_input import native_data_part

    return {"id": str(uuid.uuid4()), "input": {"messages": [{"role": "user", "content": "record"}],
            "apx_data_parts": [native_data_part(DomainInput, {"amount": amount})]}}


def test_two_native_hosts_execute_typed_input_once_and_retain_real_records(two_hosts):
    body = domain_request()
    part = body["input"]["apx_data_parts"][0]
    response = two_hosts.parent.post("/api/invocations", json=body, headers=headers())
    assert response.status_code == 200, response.text
    assert two_hosts.consumed == [{"amount": 7}]
    child_body = json.loads(next(r.content for r in two_hosts.sent if r.method == "POST"))
    child_id = child_body["id"]
    assert child_body["session_id"] == child_id
    child_record = two_hosts.child.get(f"/api/invocations/{child_id}", headers=headers()).json()
    coordinate = child_record["output"]["checkpoint"]
    assert set(coordinate) == {"thread_id"}
    checkpoint = two_hosts.saver.get_tuple({"configurable": coordinate})
    assert checkpoint is not None
    saved_answer = checkpoint.checkpoint["channel_values"]["messages"][-1]
    assert saved_answer.model_dump(mode="json") == child_record["output"]["messages"][-1]
    assert "checkpoint" not in response.json()["output"]
    assert child_id != body["id"]
    assert child_body["input"]["apx_data_parts"] == [part]
    assert child_body["input"]["correlation"]["parent_invocation_id"] == body["id"]
    for client, identifier in [(two_hosts.parent, body["id"]), (two_hosts.child, child_id)]:
        path = f"/api/invocations/{identifier}"
        assert client.get(path, headers=headers()).json()["status"] == "completed"
        assert client.get(path + "/events", headers=headers()).status_code == 200
        assert client.get(path, headers=headers("bob")).status_code == 404
    assert child_id in response.text
    assert response.json()["output"]["messages"][-1]["additional_kwargs"]["apx_data_parts"][0]["data"] == {"amount": 8}
    assert two_hosts.parent.post("/api/invocations", json=body, headers=headers()).status_code == 200
    assert two_hosts.consumed == [{"amount": 7}]
    persisted = repr((two_hosts.saver.storage, two_hosts.saver.writes, two_hosts.saver.blobs))
    assert "credential-sentinel" not in persisted
    assert "RequestAuthContext" not in persisted
    assert "WorkspaceClient" not in persisted
    for app in [two_hosts.parent_app, two_hosts.child_app]:
        store = app._runtime.runtime_store
        saved = json.dumps({"states": [asdict(s) for s in store.states.values()],
                            "events": [asdict(e) for e in store.persisted_events]})
        assert "contract-credential-sentinel" not in saved


def test_two_hosts_missing_user_conflict_and_distinct_principals(two_hosts):
    body = domain_request()
    assert two_hosts.parent.post("/api/invocations", json=body).status_code == 401
    assert two_hosts.consumed == []
    assert two_hosts.parent.post("/api/invocations", json=body, headers=user_headers()).status_code == 200
    child_body = json.loads(next(r.content for r in two_hosts.sent if r.method == "POST"))
    changed = domain_request(8)
    changed["id"] = body["id"]
    assert two_hosts.parent.post("/api/invocations", json=changed, headers=user_headers()).status_code == 409
    changed_child = {**child_body, "input": {**child_body["input"], "messages": [{"role": "user", "content": "changed"}]}}
    assert two_hosts.child.post("/api/invocations", json=changed_child, headers=user_headers()).status_code == 409
    assert two_hosts.consumed == [{"amount": 7}]
    assert two_hosts.parent.post("/api/invocations", json=changed, headers=user_headers("bob")).status_code == 200
    assert two_hosts.consumed == [{"amount": 7}, {"amount": 8}]
    assert len(set(two_hosts.principals)) == 2
    for client, identifier in [(two_hosts.parent, body["id"]), (two_hosts.child, child_body["id"])]:
        alice = client.get(f"/api/invocations/{identifier}", headers=user_headers()).json()
        bob = client.get(f"/api/invocations/{identifier}", headers=user_headers("bob")).json()
        assert alice != bob
        if client is two_hosts.child:
            assert alice["output"]["checkpoint"] != bob["output"]["checkpoint"]
    for app in [two_hosts.parent_app, two_hosts.child_app]:
        store = app._runtime.runtime_store
        saved = json.dumps({"states": [asdict(s) for s in store.states.values()],
                            "events": [asdict(e) for e in store.persisted_events]})
        assert "credential-sentinel" not in saved


def test_managed_native_smoke_reads_real_sdk_checkpoint(two_hosts, monkeypatch):
    import io
    from apx_agent import cli

    monkeypatch.setattr(cli, "_run_databricks_cmd", lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"access_token":"test"}'))
    def invoke(req, timeout):
        response = two_hosts.child.post("/api/invocations", json=json.loads(req.data), headers=user_headers())
        assert response.status_code == 200, response.text
        return io.BytesIO(response.content)
    monkeypatch.setattr("urllib.request.urlopen", invoke)
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda **k: SimpleNamespace())
    monkeypatch.setattr("databricks_agentkit.langgraph.session_store.DatabricksSessionStoreSaver", lambda *a, **k: two_hosts.saver)
    smoke = cli._check_native_execution("https://child.example", profile="test", session_store="managed",
                                        smoke_input=domain_request()["input"], smoke_agent=two_hosts.specialist)
    assert smoke.completed and smoke.readback, smoke.detail


def test_native_invalid_specialist_output_fails_both_records(two_hosts):
    two_hosts.controls["invalid"] = True
    body = domain_request()
    assert two_hosts.parent.post("/api/invocations", json=body, headers=headers()).status_code == 500
    child_body = json.loads(next(r.content for r in two_hosts.sent if r.method == "POST"))
    for client, identifier in [(two_hosts.parent, body["id"]), (two_hosts.child, child_body["id"])]:
        assert client.get(f"/api/invocations/{identifier}", headers=headers()).json()["status"] == "failed"


def test_native_logical_predecessor_is_distinct_from_transport_sender(two_hosts):
    producer = LlmAgent(name="producer", tools=two_hosts.specialist._tool_fns,
                        input_schema=DomainInput, input_key="domain_input",
                        output_schema=DomainInput, output_key="domain_input")
    two_hosts.root._agents.insert(0, producer)
    response = two_hosts.parent.post("/api/invocations", json=domain_request(), headers=headers())
    assert response.status_code == 200, response.text
    assert two_hosts.consumed == [{"amount": 7}, {"amount": 8}]
    child_body = json.loads(next(r.content for r in two_hosts.sent if r.method == "POST"))
    assert child_body["input"]["correlation"]["logical_predecessor"] == "producer"
    assert child_body["input"]["correlation"]["transport_sender"] == "parent"


def test_native_parent_checks_its_output_contract_after_child_completes(two_hosts):
    class OtherOutput(BaseModel):
        answer: str

    two_hosts.root._agents[0] = LlmAgent(name="specialist", input_schema=DomainInput,
                                        input_key="domain_input", output_schema=OtherOutput)
    body = domain_request()
    assert two_hosts.parent.post("/api/invocations", json=body, headers=headers()).status_code == 500
    child_body = json.loads(next(r.content for r in two_hosts.sent if r.method == "POST"))
    child = two_hosts.child.get(f"/api/invocations/{child_body['id']}", headers=headers()).json()
    assert child["status"] == "completed"
    events = two_hosts.parent.get(f"/api/invocations/{body['id']}/events", headers=headers()).text
    assert "agent.child.accepted" in events and child_body["id"] in events


def test_caller_correlation_does_not_replace_runtime_identity(two_hosts):
    body = domain_request()
    body["input"]["correlation"] = {"parent_invocation_id": str(uuid.uuid4()),
                                    "transport_sender": "asserted-sender", "logical_predecessor": "asserted-step"}
    assert two_hosts.parent.post("/api/invocations", json=body, headers=headers()).status_code == 200
    child_body = json.loads(next(r.content for r in two_hosts.sent if r.method == "POST"))
    assert child_body["input"]["correlation"] == {"parent_invocation_id": body["id"],
                                                "transport_sender": "parent", "logical_predecessor": None}


def test_native_remote_timeout_does_not_claim_child_completion(two_hosts):
    from dataclasses import replace
    bindings = two_hosts.root._apx_remote_leaf_bindings
    two_hosts.root._apx_remote_leaf_bindings = {"specialist": replace(bindings["specialist"], timeout_s=0.01)}
    two_hosts.controls["delay"] = True
    body = domain_request()
    assert two_hosts.parent.post("/api/invocations", json=body, headers=headers()).status_code == 500
    assert two_hosts.parent.get(f"/api/invocations/{body['id']}", headers=headers()).json()["status"] == "failed"
    assert two_hosts.consumed == []
    events = two_hosts.parent.get(f"/api/invocations/{body['id']}/events", headers=headers()).text
    assert "agent.child.requested" in events
    assert "agent.child.accepted" not in events


@pytest.mark.parametrize("change", ["extra", "wrong_type", "digest", "too_large", "metadata", "assistant", "state"])
def test_native_ingress_rejects_invalid_domain_input_before_child(two_hosts, change):
    body = domain_request()
    part = body["input"]["apx_data_parts"][0]
    if change == "extra":
        part["data"]["extra"] = True
    elif change == "wrong_type":
        part["data"]["amount"] = "7"
    elif change == "digest":
        part["metadata"]["sha256"] = "0" * 64
    elif change == "too_large":
        part["data"]["extra"] = "x" * 65536
    elif change == "metadata":
        part["metadata"]["auth"] = "untrusted"
    elif change == "assistant":
        body["input"]["messages"][0]["role"] = "assistant"
    else:
        body["input"]["state"] = {"domain_input": {"amount": 8}}
    assert two_hosts.parent.post("/api/invocations", json=body, headers=headers()).status_code == 500
    assert two_hosts.consumed == []
    assert two_hosts.sent == []


def test_native_binding_roundtrips_generated_configuration(tmp_path):
    import tomllib
    from apx_agent._project_gen import generate_project

    config = AgentConfig(name="parent", bindings={"specialist": {
        "transport": "native", "url": "$SPECIALIST_URL", "timeout_s": 12,
    }})
    generate_project(config, tmp_path)
    generated = tomllib.loads((tmp_path / "pyproject.toml").read_text())["tool"]["apx"]["agent"]
    assert AgentConfig.model_validate(generated).bindings == config.bindings


def test_native_input_bounds_and_undeclared_input(two_hosts):
    from apx_agent._native_input import native_data_part

    class OpenDomain(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: dict

    nested = {}
    for _ in range(18):
        nested = {"nested": nested}
    for payload in [{"value": nested}, {"value": {str(i): i for i in range(257)}}]:
        with pytest.raises(ValueError, match="limits"):
            native_data_part(OpenDomain, payload)
    two_hosts.specialist._input_schema = None
    response = two_hosts.child.post("/api/invocations", json=domain_request(), headers=headers())
    assert response.status_code == 500
    assert two_hosts.consumed == []
