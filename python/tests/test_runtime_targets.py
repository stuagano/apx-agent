"""Peer compilation targets must execute tools and refuse unsupported contracts."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from apx_agent import Dependencies, LlmAgent


class ToolModel(BaseChatModel):
    """Deterministic model; the real graph must dispatch the requested tool."""

    @property
    def _llm_type(self) -> str:
        return "target-test"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ToolModel:
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None,
                  **kwargs: Any) -> ChatResult:
        if isinstance(messages[-1], ToolMessage):
            reply = AIMessage(content=f"Recorded {messages[-1].content}")
        else:
            reply = AIMessage(content="", tool_calls=[{
                "name": "record", "args": {"value": "proof"}, "id": "call-proof",
            }])
        reply.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        return ChatResult(generations=[ChatGeneration(message=reply)])


@pytest.fixture
def execution(monkeypatch: pytest.MonkeyPatch) -> Any:
    effects: list[str] = []

    def record(value: str) -> str:
        """Record a value and return it."""
        effects.append(value)
        return value

    monkeypatch.setattr("apx_agent._compile._build_chat_databricks", lambda *a, **k: ToolModel())
    ws = MagicMock()
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda **k: ws)
    return SimpleNamespace(agent=LlmAgent(name="proof", tools=[record]), effects=effects, ws=ws)


def test_responses_target_executes_existing_tool(execution: Any) -> None:
    from apx_agent import compile_agent

    compiled = compile_agent(execution.agent, target="responses_agent", model="test-model")
    result = compiled.non_streaming({"input": [{"role": "user", "content": "record"}]})
    assert execution.effects == ["proof"]
    assert "Recorded proof" in result.model_dump_json()


def test_unsupported_requirement_fails_before_optional_import(execution: Any) -> None:
    from apx_agent import RuntimeRequirements, compile_agent

    with pytest.raises(ValueError, match="user_identity"):
        compile_agent(
            execution.agent, target="durable_agent_server", model="test-model",
            requirements=RuntimeRequirements(user_identity=True),
        )
    assert execution.effects == []


def test_report_describes_binding_without_claiming_durability(execution: Any) -> None:
    from apx_agent import RuntimeRequirements, inspect_target

    report = inspect_target(
        execution.agent, target="durable_agent_server",
        requirements=RuntimeRequirements(sessions=True, recovery=True),
        checkpointer=InMemorySaver(),
    )
    assert report.capabilities["sessions"].supported
    assert not report.capabilities["recovery"].supported
    assert report.unsatisfied == ["recovery"]
    assert "restart" in report.capabilities["sessions"].detail


def test_unknown_target_is_rejected(execution: Any) -> None:
    from apx_agent import inspect_target

    with pytest.raises(ValueError, match="Unknown"):
        inspect_target(execution.agent, target="typo")


@pytest.mark.parametrize("requirement", ["sessions", "approvals", "long_term_memory", "recovery", "streaming"])
def test_unbound_durable_requirements_fail(execution: Any, requirement: str) -> None:
    from apx_agent import RuntimeRequirements, compile_agent

    with pytest.raises(ValueError, match=requirement):
        compile_agent(
            execution.agent, target="durable_agent_server", model="test-model",
            requirements=RuntimeRequirements(**{requirement: True}),
        )


def test_native_handler_uses_graph_without_responses(execution: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent._durable_agent import compile_durable_handlers

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Durable target called ResponsesAgent")

    monkeypatch.setattr("apx_agent._responses_agent.compile_to_responses_agent", forbidden)
    handlers = compile_durable_handlers(execution.agent, model="test-model", service_ws=execution.ws)
    context = SimpleNamespace(request_auth=None, session_id="session-one", is_recovery=False)
    result = asyncio.run(handlers.invoke({"messages": [{"role": "user", "content": "record"}]}, context))
    assert execution.effects == ["proof"]
    assert result["status"] == "completed"
    assert result["messages"][-1]["content"] == "Recorded proof"
    assert "output" not in result


def test_native_checkpoint_history_and_output_slicing(execution: Any) -> None:
    from apx_agent._durable_agent import compile_durable_handlers

    saver = InMemorySaver()
    handlers = compile_durable_handlers(
        execution.agent, model="test-model", service_ws=execution.ws, checkpointer=saver,
    )
    context = SimpleNamespace(request_auth=None, session_id="thread", is_recovery=False)
    payload = {"messages": [{"role": "user", "content": "record"}]}
    first = asyncio.run(handlers.invoke(payload, context))
    second = asyncio.run(handlers.invoke(payload, context))
    assert len(first["messages"]) == len(second["messages"]) == 3
    assert execution.effects == ["proof", "proof"]
    assert len(list(saver.list(None))) > 2


@pytest.mark.parametrize("payload", [
    {"messages": [], "resume": "approve"},
    {"messages": [], "custom_inputs": {"approval": "approve"}},
    {"messages": [], "user_token": "not-a-credential"},
])
def test_invalid_native_requests_do_not_execute(execution: Any, payload: Any) -> None:
    from apx_agent._durable_agent import compile_durable_handlers

    handlers = compile_durable_handlers(execution.agent, model="test-model", service_ws=execution.ws)
    context = SimpleNamespace(request_auth=None, session_id="thread", is_recovery=False)
    with pytest.raises(ValueError):
        asyncio.run(handlers.invoke(payload, context))
    assert execution.effects == []


def test_request_user_auth_never_falls_back_to_app(execution: Any) -> None:
    from apx_agent._durable_agent import compile_durable_handlers

    handlers = compile_durable_handlers(execution.agent, model="test-model", service_ws=execution.ws)
    context = SimpleNamespace(request_auth=SimpleNamespace(), session_id="thread", is_recovery=False)
    with pytest.raises(ValueError, match="user_identity"):
        asyncio.run(handlers.invoke({"messages": []}, context))
    assert execution.effects == []


def test_declared_user_tool_is_rejected_without_explicit_requirements() -> None:
    from apx_agent import compile_agent

    def identity(ws: Dependencies.UserClient) -> str:
        """Read the user's identity."""
        return ws.current_user.me().user_name

    with pytest.raises(ValueError, match="user_identity"):
        compile_agent(LlmAgent(tools=[identity]), target="durable_agent_server", model="test")


def test_declared_memory_is_not_silently_ignored() -> None:
    from apx_agent import compile_agent

    with pytest.raises(ValueError, match="long_term_memory"):
        compile_agent(LlmAgent(memory="inmemory"), target="durable_agent_server", model="test")


def test_native_service_tool_receives_only_explicit_service_client(execution: Any) -> None:
    from apx_agent._durable_agent import compile_durable_handlers

    received: list[Any] = []

    def record(value: str, ws: Dependencies.Client) -> str:
        """Record using the application's service identity."""
        received.append(ws)
        return value

    handlers = compile_durable_handlers(
        LlmAgent(name="service-proof", tools=[record]), model="test", service_ws=execution.ws,
    )
    context = SimpleNamespace(request_auth=None, session_id="thread", is_recovery=False)
    result = asyncio.run(handlers.invoke([{"role": "user", "content": "record"}], context))
    assert result["status"] == "completed"
    assert received == [execution.ws]


def test_recovery_attempt_is_refused_before_tool_execution(execution: Any) -> None:
    from apx_agent._durable_agent import compile_durable_handlers

    handlers = compile_durable_handlers(execution.agent, model="test", service_ws=execution.ws)
    context = SimpleNamespace(request_auth=None, session_id="thread", is_recovery=True)
    with pytest.raises(ValueError, match="recovery"):
        asyncio.run(handlers.invoke([{"role": "user", "content": "record"}], context))
    assert execution.effects == []


def test_native_session_budget_is_preserved_across_turns(execution: Any) -> None:
    from apx_agent import SessionBudgetExceeded
    from apx_agent._durable_agent import compile_durable_handlers

    execution.agent._session_budget = {"tokens": 7}
    handlers = compile_durable_handlers(
        execution.agent, model="test", service_ws=execution.ws, checkpointer=InMemorySaver(),
    )
    context = SimpleNamespace(request_auth=None, session_id="budget-thread", is_recovery=False)
    payload = [{"role": "user", "content": "record"}]
    asyncio.run(handlers.invoke(payload, context))
    with pytest.raises(SessionBudgetExceeded):
        asyncio.run(handlers.invoke(payload, context))
    assert execution.effects == ["proof", "proof"]
    # Once at the cap, the next turn must refuse before another tool effect.
    with pytest.raises(SessionBudgetExceeded):
        asyncio.run(handlers.invoke(payload, context))
    assert execution.effects == ["proof", "proof"]


def test_durable_approval_pauses_before_effect_and_resumes(execution: Any) -> None:
    from apx_agent import FunctionPolicy, PolicyAction, PolicyGate, PolicyResult
    from apx_agent._durable_agent import compile_durable_handlers

    execution.agent._before_tool = PolicyGate([
        FunctionPolicy(lambda event: PolicyResult(action=PolicyAction.ASK, reason="approval required")),
    ])
    handlers = compile_durable_handlers(
        execution.agent, model="test", service_ws=execution.ws, checkpointer=InMemorySaver(),
    )
    context = SimpleNamespace(request_auth=None, session_id="approval-thread", is_recovery=False)
    paused = asyncio.run(handlers.invoke({"messages": [{"role": "user", "content": "record"}]}, context))
    assert paused["status"] == "interrupted"
    assert paused["approval_required"]["tool_name"] == "record"
    assert execution.effects == []
    resumed = asyncio.run(handlers.invoke({"resume": "approve"}, context))
    assert resumed["status"] == "completed"
    assert execution.effects == ["proof"]


def test_real_durable_server_http_and_readback(execution: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from fastapi.testclient import TestClient
    from apx_agent import compile_agent

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    app = compile_agent(execution.agent, target="durable_agent_server", model="test", service_ws=execution.ws)
    invocation_id = str(uuid.uuid4())
    body = {"id": invocation_id, "input": {"messages": [{"role": "user", "content": "record"}]}}
    with TestClient(app) as client:
        response = client.post("/api/invocations", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["output"]["messages"][-1]["content"] == "Recorded proof"
        saved = client.get(f"/api/invocations/{invocation_id}")
        assert saved.status_code == 200, saved.text
        assert "Recorded proof" in saved.text
        # The runtime's idempotent invocation id must not execute the tool twice.
        repeated = client.post("/api/invocations", json=body)
        assert repeated.status_code == 200, repeated.text
    assert execution.effects == ["proof"]


def test_generated_entrypoint_selects_native_target(execution: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import sys
    from ctk import Artifact, verify
    from apx_agent import AgentConfig
    from apx_agent._project_gen import generate_project

    config = AgentConfig(name="target-proof", model="test")
    generate_project(config, tmp_path)
    entrypoint = tmp_path / "agent_server" / "start_managed.py"
    verify(Artifact(str(entrypoint), min_bytes=100, must_contain="compile_agent"))
    source = entrypoint.read_text()
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace(agent=execution.agent))
    monkeypatch.setattr("apx_agent._inspection._load_agent_config", lambda: config)
    monkeypatch.setattr("apx_agent._wiring.finalize_agent", lambda *a, **k: None)
    selected: dict[str, Any] = {}

    def compile_target(agent: Any, **kwargs: Any) -> Any:
        selected.update(kwargs)
        return "native-app"

    monkeypatch.setattr("apx_agent.compile_agent", compile_target)
    namespace: dict[str, Any] = {}
    exec(compile(source, str(entrypoint), "exec"), namespace)
    assert selected["target"] == "durable_agent_server"
    assert selected["service_ws"] is execution.ws
    assert namespace["app"] == "native-app"


def test_deploy_accepts_durable_host_without_appkit(tmp_path: Path) -> None:
    from apx_agent.cli import _stage_internal_appkit_host

    doc = {"resources": {"apps": {"a": {"config": {"env": [
        {"name": "APX_APPS_HOST", "value": "agentbricks"},
    ]}}}}}
    _stage_internal_appkit_host(tmp_path, module="agent:agent", doc=doc, bundle_key="a", log=lambda *a: None)
    assert not (tmp_path / ".build").exists()
