"""#841: remote and served boundaries retain typed terminal failures."""

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from mlflow.types.agent import ChatAgentMessage
from mlflow.types.responses import ResponsesAgentRequest
from pydantic import BaseModel

from apx_agent import (
    Agent, AgentConfig, OutputValidationError, SequentialAgent,
    compile_to_chat_agent, compile_to_langgraph, compile_to_responses_agent, finalize_agent,
)
from apx_agent import _compile
from apx_agent._remote import RemoteDatabricksAgent, _RemoteReply
from apx_agent._step_contract import escalation_message, get_escalation


class Finding(BaseModel):
    record_id: int


class _Model(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def _model(monkeypatch, replies):
    model = _Model(messages=iter(replies))
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **kw: model)


def _packet():
    return {
        "status": "escalated", "availability": "unavailable", "capability": "genie",
        "error": "Step capability unavailable", "reason": "unavailable",
        "failed_step": ["remote_review", "inspect"],
        "evidence": [{"step": ["remote_review", "gather"], "output_key": "remote_finding", "data": {"record_id": 9}}],
    }


def _bind_remote(monkeypatch, tmp_path, root, parts):
    async def reply(self, messages, incoming_headers):
        return _RemoteReply(text="Attached result", control=None, data_parts=parts)

    monkeypatch.setattr(RemoteDatabricksAgent, "run_with_control", reply)
    finalize_agent(root, AgentConfig(name="proof", bindings={"peer": "https://peer.example"}),
                   pyproject_path=str(tmp_path / "missing.toml"))


def test_remote_escalation_bypasses_success_publication_and_preserves_evidence(monkeypatch, tmp_path):
    _model(monkeypatch, [AIMessage(content='{"record_id": 42}')])
    called = []
    root = SequentialAgent([
        Agent(name="gather", output_schema=Finding, output_key="local_finding"),
        Agent(name="peer", output_schema=Finding, output_key="rejected", after_agent_callback=called.append),
        Agent(name="consumer", before_agent_callback=called.append),
    ], name="review", on_failure="escalate")
    _bind_remote(monkeypatch, tmp_path, root, escalation_message(_packet()).additional_kwargs["apx_data_parts"])
    result = compile_to_langgraph(root, ws=None, model="fake").invoke({"messages": [HumanMessage(content="go")]})
    packet = get_escalation(result["messages"][-1])
    assert packet == {
        **_packet(), "failed_step": ["review", "peer", "remote_review", "inspect"],
        "evidence": [
            {"step": ["review", "gather"], "output_key": "local_finding", "data": {"record_id": 42}},
            {"step": ["review", "peer", "remote_review", "gather"], "output_key": "remote_finding", "data": {"record_id": 9}},
        ],
    }
    assert result["state"] == {"local_finding": {"record_id": 42}}
    assert called == []


def test_remote_unavailability_precedes_success_schema_validation(monkeypatch, tmp_path):
    called = []
    root = SequentialAgent([
        Agent(name="peer", output_schema=Finding, output_key="rejected", after_agent_callback=called.append),
        Agent(name="consumer", before_agent_callback=called.append),
    ], name="review", on_failure="escalate")
    _bind_remote(monkeypatch, tmp_path, root, [{"kind": "data", "data": {
        "availability": "unavailable", "capability": "genie", "error": "private remote error",
    }}])
    result = compile_to_langgraph(root, ws=None, model="fake").invoke({"messages": [HumanMessage(content="go")]})
    packet = get_escalation(result["messages"][-1])
    assert packet["reason"] == "unavailable"
    assert packet["capability"] == "genie"
    assert packet["failed_step"] == ["review", "peer"]
    assert "private remote error" not in json.dumps(packet)
    assert "rejected" not in result.get("state", {})
    assert called == []


def test_remote_escalation_cannot_bypass_output_guardrail(monkeypatch, tmp_path):
    inspected = []

    def guardrail(text):
        inspected.append(text)
        return "blocked attached packet" if "remote_finding" in text else None

    root = SequentialAgent([
        Agent(name="peer", output_schema=Finding, output_guardrails=[guardrail]),
    ], name="review", on_failure="escalate")
    _bind_remote(monkeypatch, tmp_path, root, escalation_message(_packet()).additional_kwargs["apx_data_parts"])
    with pytest.raises(OutputValidationError, match="guardrail"):
        compile_to_langgraph(root, ws=None, model="fake").invoke({"messages": [HumanMessage(content="go")]})
    assert len(inspected) == 1
    assert "remote_finding" in inspected[0]


@pytest.mark.parametrize("entry", ["responses", "responses_stream", "chat_agent"])
def test_served_escalation_preserves_final_data_part(monkeypatch, entry):
    _model(monkeypatch, [AIMessage(content="private invalid typed output")])
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda: MagicMock())
    root = SequentialAgent([Agent(name="inspect", output_schema=Finding)], name="review", on_failure="escalate")
    if entry == "chat_agent":
        response = compile_to_chat_agent(root, model="fake").predict([
            ChatAgentMessage(role="user", content="go"),
        ])
    else:
        invoke, stream = compile_to_responses_agent(root, model="fake")
        request = ResponsesAgentRequest(input=[{"role": "user", "content": "go"}])
        response = list(stream(request))[-1] if entry == "responses_stream" else invoke(request)
    parts = response.custom_outputs["apx_data_parts"]
    assert len(parts) == 1
    assert parts[0]["metadata"]["schema_id"] == "urn:apx:sequential-escalation:v1"
    packet = parts[0]["data"]
    assert packet["reason"] == "schema_miss"
    assert packet["failed_step"] == ["review", "inspect"]
    assert packet["evidence"] == []
    assert "private invalid" not in json.dumps(packet)
