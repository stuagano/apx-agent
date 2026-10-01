"""Structured findings survive compiled and served boundaries (#838)."""

import json
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from mlflow.types.responses import ResponsesAgentRequest
from pydantic import BaseModel

from apx_agent import Agent, compile_to_langgraph, compile_to_responses_agent
from apx_agent import _compile
from apx_agent._a2a_models import Message


class Finding(BaseModel):
    record_id: int
    evidence: list[str]


class DatedFinding(BaseModel):
    observed_on: date


DATA = {"record_id": 42, "evidence": ["record 42"]}
PART = {"kind": "data", "data": DATA}


class _Model(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def _model(monkeypatch):
    monkeypatch.setattr(
        _compile, "_build_chat_databricks",
        lambda *a, **kw: _Model(messages=iter([AIMessage(content=json.dumps(DATA))])),
    )


def test_native_message_keeps_data_separate_from_text():
    message = Message.model_validate({
        "role": "user", "messageId": "u1",
        "parts": [{"kind": "text", "text": "Inspect"}, PART],
    })
    assert message.text() == "Inspect"
    assert message.model_dump(exclude_none=True)["parts"][1] == PART


def test_existing_text_part_can_still_omit_default_kind():
    message = Message.model_validate({"role": "user", "messageId": "u1", "parts": [{"text": "Inspect"}]})
    assert message.text() == "Inspect"


def test_compiled_typed_output_has_structured_part(monkeypatch):
    _model(monkeypatch)
    graph = compile_to_langgraph(Agent(output_schema=Finding), ws=None, model="fake")
    result = graph.invoke({"messages": [HumanMessage(content="Find")]})
    assert result["messages"][-1].additional_kwargs["apx_data_parts"] == [PART]


@pytest.mark.parametrize("stream", [False, True])
def test_served_typed_output_emits_data_without_parsing_text(monkeypatch, stream):
    _model(monkeypatch)
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda: MagicMock())
    invoke, streaming = compile_to_responses_agent(Agent(output_schema=Finding), model="fake")
    request = ResponsesAgentRequest(input=[{"role": "user", "content": "Find"}])
    response = list(streaming(request))[-1] if stream else invoke(request)
    assert response.custom_outputs["apx_data_parts"] == [PART]


@pytest.mark.parametrize("stream", [False, True])
def test_served_input_preserves_data_for_the_receiving_model(monkeypatch, stream):
    _model(monkeypatch)
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda: MagicMock())
    seen = []
    agent = Agent(before_model=lambda messages: seen.append(messages))
    invoke, streaming = compile_to_responses_agent(agent, model="fake")
    request = ResponsesAgentRequest(
        input=[{"role": "user", "content": "Inspect"}],
        custom_inputs={"apx_data_parts": [PART]},
    )
    list(streaming(request)) if stream else invoke(request)
    inputs = [m for call in seen for batch in call for m in batch if m.type == "human"]
    assert inputs[-1].additional_kwargs["apx_data_parts"] == [PART]
    assert "record_id" in str(inputs[-1].content)


@pytest.mark.asyncio
async def test_remote_reply_keeps_business_data_out_of_control(monkeypatch):
    from apx_agent._remote import RemoteDatabricksAgent

    remote = RemoteDatabricksAgent("https://peer.example")
    monkeypatch.setattr(remote, "_init_quietly", AsyncMock())
    monkeypatch.setattr(remote, "_post_via_http", AsyncMock(return_value={
        "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "Found"},
        ]}],
        "custom_outputs": {"apx_data_parts": [PART]},
    }))
    reply = await remote.run_with_control([], {})
    assert reply.text == "Found"
    assert reply.data_parts == [PART]
    assert reply.control is None


@pytest.mark.parametrize("invalid", [None, {}, [42], [{"kind": "data", "data": []}],
                                      [{"kind": "data", "data": {"bad": float("nan")}}]])
def test_invalid_data_is_rejected_before_model(monkeypatch, invalid):
    _model(monkeypatch)
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda: MagicMock())
    seen = []
    invoke, _ = compile_to_responses_agent(Agent(before_model=lambda m: seen.append(m)), model="fake")
    with pytest.raises(ValueError, match="Invalid apx_data_parts"):
        invoke(ResponsesAgentRequest(
            input=[{"role": "user", "content": "Inspect"}],
            custom_inputs={"apx_data_parts": invalid},
        ))
    assert seen == []


def test_native_a2a_executes_and_persists_data_parts(monkeypatch):
    from fastapi.testclient import TestClient
    from apx_agent import AgentConfig, create_app

    _model(monkeypatch)
    monkeypatch.setattr("apx_agent._wiring._make_workspace_client", lambda: MagicMock())
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda: MagicMock())
    seen = []
    app = create_app(Agent(output_schema=Finding, before_model=lambda m: seen.append(m)),
                     config=AgentConfig(name="data-proof", model="fake"))
    with TestClient(app) as client:
        response = client.post("/", json={
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"role": "user", "messageId": "u1", "parts": [PART]}},
        })
        task = response.json()["result"]
        assert task["status"]["state"] == "completed"
        assert task["artifacts"][0]["parts"][-1]["data"] == DATA
        readback = client.post("/", json={
            "jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": task["id"]},
        }).json()["result"]
        assert readback == task
    inputs = [m for call in seen for batch in call for m in batch if m.type == "human"]
    assert inputs[-1].additional_kwargs["apx_data_parts"] == [PART]


@pytest.mark.parametrize("schema, payload", [(Finding, DATA), (DatedFinding, {"observed_on": "2026-09-30"})])
def test_bound_remote_validates_the_data_instead_of_the_prose(monkeypatch, tmp_path, schema, payload):
    from apx_agent import AgentConfig, SequentialAgent, finalize_agent
    from apx_agent._remote import RemoteDatabricksAgent, _RemoteReply

    part = {"kind": "data", "data": payload}

    async def reply(self, messages, incoming_headers):
        return _RemoteReply(text="A finding is attached", control=None, data_parts=[part])

    monkeypatch.setattr(RemoteDatabricksAgent, "run_with_control", reply)
    root = SequentialAgent([Agent(name="finding", output_schema=schema, output_key="finding")])
    finalize_agent(root, AgentConfig(name="proof", bindings={"finding": "https://peer.example"}),
                   pyproject_path=str(tmp_path / "missing.toml"))
    result = compile_to_langgraph(root, ws=None, model="fake").invoke({
        "messages": [HumanMessage(content="Find")],
    })
    assert result["state"]["finding"] == payload
    assert result["messages"][-1].additional_kwargs["apx_data_parts"] == [part]


@pytest.mark.asyncio
async def test_compiled_pipeline_round_trips_through_responses_http(monkeypatch, tmp_path):
    import asyncio
    import httpx
    from fastapi import FastAPI
    from apx_agent import AgentConfig, SequentialAgent, finalize_agent
    from apx_agent._remote import RemoteDatabricksAgent

    _model(monkeypatch)
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda: MagicMock())
    received = []
    invoke, _ = compile_to_responses_agent(Agent(output_schema=Finding), model="fake")
    peer = FastAPI()

    @peer.post("/responses")
    async def responses(body: dict):
        received.append(body)
        return (await asyncio.to_thread(invoke, ResponsesAgentRequest.model_validate(body))).model_dump()

    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.ASGITransport(app=peer)))
    monkeypatch.setattr(RemoteDatabricksAgent, "_init_quietly", AsyncMock())
    root = SequentialAgent([
        Agent(name="producer", output_schema=Finding),
        Agent(name="peer", output_schema=Finding, output_key="result"),
    ])
    finalize_agent(root, AgentConfig(name="proof", bindings={"peer": "https://peer.example"}),
                   pyproject_path=str(tmp_path / "missing.toml"))
    result = await compile_to_langgraph(root, ws=None, model="fake").ainvoke({
        "messages": [HumanMessage(content="Find")],
    })
    assert received[0]["custom_inputs"]["apx_data_parts"] == [PART]
    assert all("apx_data_parts" not in item for item in received[0]["input"])
    assert result["state"]["result"] == DATA
    assert result["messages"][-1].additional_kwargs["apx_data_parts"] == [PART]


def test_data_payload_cannot_bypass_output_guardrail(monkeypatch):
    model = _Model(messages=iter([AIMessage(
        content="Safe summary", additional_kwargs={"apx_data_parts": [PART]},
    )]))
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **kw: model)
    agent = Agent(output_guardrails=[lambda text: "Blocked" if "record_id" in text else None])
    result = compile_to_langgraph(agent, ws=None, model="fake").invoke({
        "messages": [HumanMessage(content="Find")],
    })
    assert result["messages"][-1].content == "Blocked"
    assert "apx_data_parts" not in result["messages"][-1].additional_kwargs


def test_data_only_reply_has_compatible_text_rendering():
    from apx_agent._remote import _reply_text

    reply = _reply_text({"output": [], "custom_outputs": {"apx_data_parts": [PART]}},
                        url="https://peer.example", agent_name="peer")
    assert json.loads(reply) == DATA


def test_final_plain_answer_does_not_publish_an_earlier_steps_data():
    from apx_agent._a2a_models import output_data_parts

    assert output_data_parts([
        AIMessage(content="Finding", additional_kwargs={"apx_data_parts": [PART]}),
        AIMessage(content="Summary"),
    ]) == {}


@pytest.mark.asyncio
async def test_sdk_response_preserves_schema_hint_and_control_separately(monkeypatch):
    from apx_agent._remote import RemoteDatabricksAgent

    remote = RemoteDatabricksAgent("https://peer.example")
    remote._app_name = "peer"
    part = {**PART, "metadata": {"schema_id": "urn:example:finding:v1"}}
    response = MagicMock()
    response.model_dump.return_value = {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "Done"}]},
            {"type": "function_call", "name": "finish_loop", "arguments": "{}", "call_id": "c1"},
        ],
        "custom_outputs": {"apx_data_parts": [part]},
    }
    monkeypatch.setattr(remote, "_init_quietly", AsyncMock())
    monkeypatch.setattr(remote, "_post_via_sdk", AsyncMock(return_value=response))
    reply = await remote.run_with_control([], {})
    assert reply.data_parts == [part]
    assert reply.control.name == "finish_loop"


@pytest.mark.asyncio
async def test_invalid_remote_data_does_not_retry_execution(monkeypatch):
    from apx_agent._remote import RemoteDatabricksAgent

    remote = RemoteDatabricksAgent("https://peer.example")
    remote._app_name = "peer"
    response = MagicMock()
    response.model_dump.return_value = {"output": [], "custom_outputs": {"apx_data_parts": [{"data": "secret"}]}}
    monkeypatch.setattr(remote, "_init_quietly", AsyncMock())
    sdk = AsyncMock(return_value=response)
    http = AsyncMock()
    monkeypatch.setattr(remote, "_post_via_sdk", sdk)
    monkeypatch.setattr(remote, "_post_via_http", http)
    with pytest.raises(ValueError, match="Invalid apx_data_parts") as error:
        await remote.run_with_control([], {})
    assert "secret" not in str(error.value)
    assert sdk.await_count == 1
    http.assert_not_awaited()


@pytest.mark.asyncio
async def test_sdk_text_api_renders_data_only_reply(monkeypatch):
    from apx_agent._remote import RemoteDatabricksAgent

    remote = RemoteDatabricksAgent("https://peer.example")
    response = MagicMock(output_text="")
    response.model_dump.return_value = {"output": [], "custom_outputs": {"apx_data_parts": [PART]}}
    monkeypatch.setattr(remote, "_post_via_sdk", AsyncMock(return_value=response))
    assert json.loads(await remote._call_via_sdk([], {})) == DATA


@pytest.mark.asyncio
async def test_sdk_request_uses_supported_custom_inputs(monkeypatch):
    import databricks_openai
    from apx_agent._remote import RemoteDatabricksAgent

    client = MagicMock()
    client.responses.create = AsyncMock()
    monkeypatch.setattr(databricks_openai, "AsyncDatabricksOpenAI", lambda: client)
    remote = RemoteDatabricksAgent("https://peer.example")
    remote._app_name = "peer"
    await remote._post_via_sdk([{"role": "user", "content": "Review", "apx_data_parts": [PART]}], {})
    sent = client.responses.create.call_args.kwargs
    assert sent["input"] == [{"role": "user", "content": "Review"}]
    assert sent["extra_body"] == {"custom_inputs": {"apx_data_parts": [PART]}}


@pytest.mark.asyncio
async def test_text_api_does_not_replay_a_malformed_sdk_data_reply(monkeypatch):
    from apx_agent._remote import RemoteDatabricksAgent

    remote = RemoteDatabricksAgent("https://peer.example")
    remote._app_name = "peer"
    response = MagicMock(output_text="")
    response.model_dump.return_value = {"output": [], "custom_outputs": {"apx_data_parts": [{"data": "bad"}]}}
    monkeypatch.setattr(remote, "_init_quietly", AsyncMock())
    monkeypatch.setattr(remote, "_post_via_sdk", AsyncMock(return_value=response))
    http = AsyncMock()
    monkeypatch.setattr(remote, "_call_via_http", http)
    with pytest.raises(ValueError, match="Invalid apx_data_parts"):
        await remote._run_with_incoming_headers([], {})
    http.assert_not_awaited()
