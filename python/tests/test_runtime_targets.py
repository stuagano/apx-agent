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


@pytest.mark.parametrize("identity", ["service", "user", "memory"])
def test_native_manifest_uses_existing_authorization_contract(
    identity: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctk import Artifact, verify
    from databricks_agentbricks.agent_project import AgentProject
    from databricks_agentkit.runtime.auth import InvocationAuthPolicy
    from apx_agent import AgentConfig, ResourceSpec, require_user_api_scopes
    from apx_agent._apps_authorization import compile_authorization_plan
    from apx_agent._durable_agent import build_native_manifest
    from apx_agent._resources import attach_resources

    def lookup(ws: Dependencies.UserClient) -> str:
        """Use the authenticated user's SQL service."""
        return "ok"

    attach_resources(lookup, [ResourceSpec(kind="sql_warehouse", identifier="warehouse")])
    require_user_api_scopes(lookup, ["sql", "catalog.catalogs:read"])
    agent = LlmAgent(name="native", tools=[lookup] if identity == "user" else [])
    config = AgentConfig(name="native", target="durable_agent_server",
                         memory={"type": "managed", "store_name": "agent-memory"} if identity == "memory" else None)
    plan = compile_authorization_plan(agent, model=config.model)
    manifest = tmp_path / "agent.toml"
    manifest.write_text(build_native_manifest(config=config, authorization_plan=plan))
    verify(Artifact(str(manifest), must_contain="[auth.user]"))
    project = AgentProject.load(tmp_path)
    assert project.user_auth.required == (identity != "service")
    assert project.user_auth.additional_api_scopes == (plan.user_api_scopes if identity != "service" else ())
    if identity == "user":
        assert project.user_auth.additional_api_scopes.count("sql") == 1
        assert "catalog.catalogs:read" in project.user_auth.additional_api_scopes
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    assert InvocationAuthPolicy.from_manifest().requires_user == (identity != "service")


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
    from mlflow.pyfunc import ResponsesAgent

    assert isinstance(compiled, ResponsesAgent)
    result = compiled.predict({"input": [{"role": "user", "content": "record"}]})
    assert execution.effects == ["proof"]
    assert "Recorded proof" in result.model_dump_json()


def test_responses_target_resolves_declared_session(execution: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import AgentConfig, compile_agent

    monkeypatch.chdir(tmp_path)
    model = compile_agent(execution.agent, config=AgentConfig(name="proof", model="test", session={"type": "inmemory"}))
    model.predict({"input": [{"role": "user", "content": "record"}], "custom_inputs": {"thread_id": "declared"}})
    store = model.conversation_store
    assert store.get_conversation("declared") is not None
    assert "Recorded proof" in str(store.list_items("declared").data)
    assert execution.effects == ["proof"]


def test_mlflow_model_roundtrip_predict_and_stream(execution: Any, tmp_path: Path) -> None:
    import mlflow.pyfunc
    from ctk import Artifact, verify
    from apx_agent import compile_agent

    compiled = compile_agent(execution.agent, target="responses_agent", model="test-model")
    request = {"input": [{"role": "user", "content": "record"}]}
    # Save after use: cached runtime handlers must not leak into the artifact.
    compiled.predict(request)
    path = tmp_path / "model"
    mlflow.pyfunc.save_model(str(path), python_model=compiled, pip_requirements=[])
    verify(Artifact(str(path / "MLmodel"), min_bytes=100, must_contain="responses"))
    loaded = mlflow.pyfunc.load_model(str(path))
    import cloudpickle

    with (path / "python_model.pkl").open("rb") as source:
        restored = cloudpickle.load(source)
    assert restored._handlers is None
    assert "Recorded proof" in str(loaded.predict(request))
    events = list(loaded.predict_stream(request))
    assert any(event["type"] == "response.output_item.done" for event in events)
    assert "Recorded proof" in str(events)


def test_mlflow_initialization_is_shared_across_concurrent_callers(execution: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from apx_agent import compile_agent
    from apx_agent import _mlflow_model

    real_compile = _mlflow_model.compile_to_responses_agent
    calls: list[Any] = []

    def compile_once(*args: Any, **kwargs: Any) -> Any:
        result = real_compile(*args, **kwargs)
        calls.append(result)
        return result

    monkeypatch.setattr(_mlflow_model, "compile_to_responses_agent", compile_once)
    model = compile_agent(execution.agent, target="responses_agent", model="test")
    barrier = Barrier(4)

    def initialize() -> Any:
        barrier.wait(timeout=5)
        return model._compiled()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: initialize(), range(4)))
    assert len(calls) == 1
    assert all(result is results[0] for result in results)


def test_unsupported_requirement_fails_before_optional_import(execution: Any) -> None:
    from apx_agent import RuntimeRequirements, compile_agent

    with pytest.raises(ValueError, match="recovery"):
        compile_agent(
            execution.agent, target="durable_agent_server", model="test-model",
            requirements=RuntimeRequirements(recovery=True),
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


@pytest.mark.parametrize("requirement", ["sessions", "approvals", "long_term_memory", "recovery"])
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


def test_payload_session_cannot_bypass_native_session_queue(execution: Any) -> None:
    from apx_agent._durable_agent import compile_durable_handlers

    handlers = compile_durable_handlers(execution.agent, model="test", service_ws=execution.ws,
                                        checkpointer=InMemorySaver())
    context = SimpleNamespace(request_auth=None, session_id="native", is_recovery=False)
    with pytest.raises(ValueError, match="top-level"):
        asyncio.run(handlers.invoke({"session_id": "other", "messages": []}, context))
    assert execution.effects == []


@pytest.mark.parametrize("kwargs", [
    {"session_store": ""},
    {"session_store": "managed", "checkpointer": InMemorySaver()},
])
def test_conflicting_session_bindings_rejected(execution: Any, kwargs: Any) -> None:
    from apx_agent import compile_agent

    with pytest.raises(ValueError, match="session_store"):
        compile_agent(execution.agent, target="durable_agent_server", model="test", **kwargs)


def test_request_user_auth_never_falls_back_to_app(execution: Any) -> None:
    from apx_agent._durable_agent import compile_durable_handlers

    handlers = compile_durable_handlers(execution.agent, model="test-model", service_ws=execution.ws)
    context = SimpleNamespace(request_auth=SimpleNamespace(), session_id="thread", is_recovery=False)
    with pytest.raises(ValueError, match="user_identity"):
        asyncio.run(handlers.invoke({"messages": []}, context))
    assert execution.effects == []


def test_declared_user_tool_requires_native_auth(execution: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from databricks_agentkit.runtime.auth import RequestAuthContext
    from apx_agent import compile_agent
    from apx_agent._durable_agent import compile_durable_handlers

    received: list[Any] = []

    def record(value: str, ws: Dependencies.UserClient, principal: Dependencies.Principal) -> str:
        """Record with the current user's identity."""
        received.append((ws, principal))
        return value

    agent = LlmAgent(name="identity", tools=[record])
    handlers = compile_durable_handlers(agent, model="test", service_ws=execution.ws, checkpointer=InMemorySaver())
    context = SimpleNamespace(request_auth=None, session_id="same", is_recovery=False)
    with pytest.raises(ValueError, match="user_identity"):
        asyncio.run(handlers.invoke([{"role": "user", "content": "record"}], context))
    assert not received
    user = MagicMock()
    user.current_user.me.return_value.id = "user-a"
    monkeypatch.setattr(RequestAuthContext, "client_for", lambda self, mode: user)
    context.request_auth = RequestAuthContext(token=None, principal="user-a", local=True)
    asyncio.run(handlers.invoke([{"role": "user", "content": "record"}], context))
    assert received == [(user, "user-a")]
    assert received[0][0] is not execution.ws
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    app = compile_agent(agent, target="durable_agent_server", model="test", service_ws=execution.ws)
    assert app.auth_policy.requires_user


@pytest.mark.parametrize("dependency", [Dependencies.Headers, Dependencies.Request])
def test_raw_request_dependencies_remain_rejected(dependency: Any) -> None:
    from apx_agent import inspect_target

    def record(value: str, context: Any) -> str:
        """Needs the complete original request context."""
        return value

    record.__annotations__["context"] = dependency
    report = inspect_target(LlmAgent(tools=[record]), target="durable_agent_server")
    assert report.unsatisfied == ["user_identity"]


def test_native_http_auth_isolates_same_invocation_and_session_ids(execution: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from databricks_agentkit.runtime.auth import RequestAuthContext
    from fastapi.testclient import TestClient
    from apx_agent import compile_agent

    principals: list[str] = []

    def record(value: str, principal: Dependencies.Principal) -> str:
        """Record the principal resolved from the SDK user client."""
        principals.append(principal)
        return value

    def user_client(auth: Any, mode: str) -> Any:
        assert mode == "user"
        user = MagicMock()
        user.current_user.me.return_value.id = auth.namespace("principal", "verified")
        return user

    monkeypatch.setattr(RequestAuthContext, "client_for", user_client)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    app = compile_agent(LlmAgent(name="users", tools=[record]), target="durable_agent_server",
                        model="test", service_ws=execution.ws, checkpointer=InMemorySaver())
    # Use the real SDK ingress parser and namespacing with a local Runtime Store;
    # only the outbound user-client lookup is substituted above.
    monkeypatch.delenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL")
    monkeypatch.setenv("DATABRICKS_APP_NAME", "identity-proof")
    body = {"id": str(uuid.uuid4()), "session_id": "same-session",
            "input": [{"role": "user", "content": "record"}]}
    with TestClient(app) as client:
        assert client.post("/api/invocations", json=body).status_code == 401
        for principal in ["alice", "bob", "alice"]:
            headers = {"X-Forwarded-User": principal, "X-Forwarded-Access-Token": "synthetic-test-credential"}
            response = client.post("/api/invocations", json=body, headers=headers)
            assert response.status_code == 200, response.text
            assert len(response.json()["output"]["messages"]) == 3
            saved = client.get(f"/api/invocations/{body['id']}", headers=headers)
            assert saved.status_code == 200
            assert "synthetic-test-credential" not in saved.text
    assert len(principals) == 2 and principals[0] != principals[1]


def test_declared_memory_is_not_silently_ignored() -> None:
    from apx_agent import compile_agent

    with pytest.raises(ValueError, match="long_term_memory"):
        compile_agent(LlmAgent(memory="inmemory"), target="durable_agent_server", model="test")


def test_native_declared_managed_memory_writes_under_verified_principal(execution: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from databricks_agentkit.runtime.auth import RequestAuthContext
    from apx_agent import AgentConfig, compile_agent
    from apx_agent._memory import RecallOptions
    from apx_agent._durable_agent import compile_durable_handlers

    class MemoryModel(ToolModel):
        def _generate(self, messages: Any, **kwargs: Any) -> ChatResult:
            if isinstance(messages[-1], ToolMessage):
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="saved"))])
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[
                {"name": "remember", "args": {"content": "Prefers tea"}, "id": "memory-call"},
            ]))])

    records: dict[str, list[dict[str, Any]]] = {}

    def api(method: str, path: str, *, query: Any = None, body: Any = None) -> Any:
        if path.endswith("/entries") and method == "POST":
            entry = {**body, "name": "memory-stores/agent-memory/entries/entry-1"}
            records.setdefault(body["actor_id"], []).append(entry)
            return entry
        if path.endswith(":search"):
            return {"results": [{"managed_memory_entry": item, "score": 1.0}
                                for item in records.get(body["actor_id"], [])]}
        assert method == "GET" and path.endswith("agent-memory"), path
        return {"name": "memory-stores/agent-memory", "display_name": "agent-memory"}

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    monkeypatch.setattr("apx_agent._compile._build_chat_databricks", lambda *a, **k: MemoryModel())
    execution.ws.api_client.do.side_effect = api
    user = MagicMock()
    user.current_user.me.return_value.id = "alice"
    monkeypatch.setattr(RequestAuthContext, "client_for", lambda self, mode: user)
    config = AgentConfig(name="memory-proof", target="durable_agent_server", model="test",
                         memory={"type": "managed", "store_name": "agent-memory"})
    agent = LlmAgent(name="memory-proof")
    app = compile_agent(agent, config=config, service_ws=execution.ws)
    assert app.auth_policy.requires_user
    handlers = compile_durable_handlers(agent, model="test", service_ws=execution.ws)
    context = SimpleNamespace(request_auth=RequestAuthContext(token=None, principal="alice", local=True),
                              session_id="one", is_recovery=False)
    assert asyncio.run(handlers.invoke([{"role": "user", "content": "remember"}], context))["status"] == "completed"
    store = agent._apx_memory_store
    assert [r.memory.content for r in store.recall(RecallOptions(principal_id="alice", query="tea"))] == ["Prefers tea"]
    assert store.recall(RecallOptions(principal_id="bob", query="tea")) == []
    assert set(records) == {"alice"}


def test_native_memory_missing_store_fails_closed(execution: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from apx_agent import AgentConfig, compile_agent

    monkeypatch.chdir(tmp_path)
    execution.ws.api_client.do.side_effect = ConnectionError("store unreachable")
    with pytest.raises(ValueError, match="long_term_memory"):
        compile_agent(LlmAgent(name="missing"), service_ws=execution.ws,
                      config=AgentConfig(name="missing", target="durable_agent_server", model="test",
                                         memory={"type": "managed", "store_name": "missing-memory"}))


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
        readiness = client.get("/readyz")
        assert readiness.status_code == 200
        assert readiness.json()["checks"]["runtime_store"] == "ok"
        response = client.post("/api/invocations", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["output"]["messages"][-1]["content"] == "Recorded proof"
        saved = client.get(f"/api/invocations/{invocation_id}")
        assert saved.status_code == 200, saved.text
        assert "Recorded proof" in saved.text
        # The runtime's idempotent invocation id must not execute the tool twice.
        repeated = client.post("/api/invocations", json=body)
        assert repeated.status_code == 200, repeated.text
        from unittest.mock import AsyncMock

        monkeypatch.setattr(app._runtime.runtime_store, "get", AsyncMock(side_effect=ConnectionError("offline")))
        assert client.get("/readyz").status_code == 503
    assert execution.effects == ["proof"]


def test_native_server_exposes_invoke_readback_and_replay_only(
    execution: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    """The installed SDK has no cancel, disconnect, or reconnect contract."""
    from fastapi.routing import APIRoute
    from fastapi.testclient import TestClient
    from apx_agent import compile_agent

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    app = compile_agent(
        execution.agent, target="durable_agent_server", model="test", service_ws=execution.ws,
    )
    routes = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert ("POST", "/api/invocations") in routes
    assert ("GET", "/api/invocations/{invocation_id}") in routes
    assert ("GET", "/api/invocations/{invocation_id}/events") in routes
    assert ("GET", "/readyz") in routes
    assert not any("cancel" in path or "reconnect" in path for _, path in routes)
    with TestClient(app) as client:
        # "cancel" matches the invocation-id read-back route, and that route is GET-only.
        missing = client.post("/api/invocations/cancel")
    assert missing.status_code == 405


@pytest.mark.parametrize("buffered", [False, True])
def test_native_stream_persists_chunks_and_replays_without_tool_effects(execution: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, buffered: bool) -> None:
    import json
    from fastapi.testclient import TestClient
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk
    from apx_agent import RuntimeRequirements, compile_agent

    class StreamingModel(ToolModel):
        def _stream(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
            reply = self._generate(messages).generations[0].message
            if reply.tool_calls:
                yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_calls=reply.tool_calls,
                                                                 usage_metadata=reply.usage_metadata))
            else:
                yield ChatGenerationChunk(message=AIMessageChunk(content="Recorded "))
                yield ChatGenerationChunk(message=AIMessageChunk(content="proof", usage_metadata=reply.usage_metadata))

    monkeypatch.setattr("apx_agent._compile._build_chat_databricks", lambda *a, **k: StreamingModel())
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    if buffered:
        execution.agent._after_model = lambda output: output
    app = compile_agent(execution.agent, target="durable_agent_server", model="test", service_ws=execution.ws,
                        requirements=RuntimeRequirements(streaming=True))
    invocation_id = str(uuid.uuid4())
    body = {"id": invocation_id, "stream": True,
            "input": {"messages": [{"role": "user", "content": "record"}]}}
    with TestClient(app) as client:
        response = client.post("/api/invocations", json=body)
        assert response.status_code == 200, response.text
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        chunks = [event["message"]["content"] for event in events if event.get("type") == "agent.message.delta"]
        if buffered:
            assert not chunks
            completed = [event for event in events if event.get("type") == "agent.message.completed"]
            assert len(completed) == 1 and completed[0]["message"]["content"] == "Recorded proof"
        else:
            assert "Recorded " in chunks and "proof" in chunks
        saved = client.get(f"/api/invocations/{invocation_id}").json()
        assert saved["output"]["messages"][-1]["content"] == "Recorded proof"
        replay = client.get(f"/api/invocations/{invocation_id}/events")
        assert replay.text == response.text
        assert client.post("/api/invocations", json=body).status_code == 200
    assert execution.effects == ["proof"]


def test_native_stream_preserves_budget_and_approval(execution: Any) -> None:
    from apx_agent import FunctionPolicy, PolicyAction, PolicyGate, PolicyResult, SessionBudgetExceeded
    from apx_agent._durable_agent import compile_durable_handlers

    events: list[Any] = []

    async def emit(event: Any) -> int:
        events.append(event)
        return len(events)

    execution.agent._session_budget = {"tokens": 3}
    execution.agent._before_tool = PolicyGate([
        FunctionPolicy(lambda event: PolicyResult(action=PolicyAction.ASK, reason="approval required")),
    ])
    handlers = compile_durable_handlers(execution.agent, model="test", service_ws=execution.ws,
                                        checkpointer=InMemorySaver())
    context = SimpleNamespace(request_auth=None, session_id="streamed", is_recovery=False, emit=emit)
    paused = asyncio.run(handlers.invoke([{"role": "user", "content": "record"}], context))
    assert paused["status"] == "interrupted"
    assert not execution.effects
    with pytest.raises(SessionBudgetExceeded):
        asyncio.run(handlers.invoke({"resume": "approve"}, context))
    assert execution.effects == ["proof"]
    with pytest.raises(SessionBudgetExceeded):
        asyncio.run(handlers.invoke([{"role": "user", "content": "again"}], context))
    assert execution.effects == ["proof"]


def test_native_stream_write_failure_stops_execution(execution: Any) -> None:
    from apx_agent._durable_agent import compile_durable_handlers

    async def emit(event: Any) -> int:
        raise ConnectionError("event store unavailable")

    context = SimpleNamespace(request_auth=None, session_id=None, is_recovery=False, emit=emit)
    handlers = compile_durable_handlers(execution.agent, model="test", service_ws=execution.ws)
    with pytest.raises(ConnectionError, match="event store"):
        asyncio.run(handlers.invoke([{"role": "user", "content": "record"}], context))
    assert not execution.effects


def test_native_output_hooks_prevent_premature_chunk_publication(execution: Any) -> None:
    from apx_agent import RuntimeRequirements, inspect_target
    from apx_agent._durable_agent import compile_durable_handlers

    def reject(output: Any) -> None:
        raise ValueError("output denied")

    execution.agent._after_model = reject
    report = inspect_target(execution.agent, target="durable_agent_server",
                            requirements=RuntimeRequirements(streaming=True))
    assert report.unsatisfied == []
    assert report.streaming_buffered
    events: list[Any] = []

    async def emit(event: Any) -> int:
        events.append(event)
        return len(events)

    context = SimpleNamespace(request_auth=None, session_id=None, is_recovery=False, emit=emit)
    handlers = compile_durable_handlers(execution.agent, model="test", service_ws=execution.ws)
    with pytest.raises(Exception, match="output denied"):
        asyncio.run(handlers.invoke([{"role": "user", "content": "record"}], context))
    assert not events
    assert not execution.effects


def test_native_checked_output_streams_only_after_validation(execution: Any) -> None:
    from apx_agent._durable_agent import compile_durable_handlers

    events: list[Any] = []
    checked: list[Any] = []

    def accept(output: Any) -> Any:
        assert not events
        checked.append(output)
        return output

    execution.agent._after_model = accept

    async def emit(event: Any) -> int:
        assert checked
        events.append(event)
        return len(events)

    context = SimpleNamespace(request_auth=None, session_id=None, is_recovery=False, emit=emit)
    handlers = compile_durable_handlers(execution.agent, model="test", service_ws=execution.ws)
    result = asyncio.run(handlers.invoke([{"role": "user", "content": "record"}], context))
    assert result["status"] == "completed"
    assert events == [{"type": "agent.message.completed", "message": result["messages"][-1]}]
    assert "Recorded proof" in events[0]["message"]["content"]
    assert execution.effects == ["proof"]


@pytest.mark.parametrize("declarative", [False, True])
def test_managed_session_binding_survives_new_server(execution: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, declarative: bool) -> None:
    """Use the actual SDK saver against a local REST fake; reconstruct both servers."""
    import copy
    import hashlib
    import json
    from databricks.sdk.errors import NotFound
    from databricks_agentkit.langgraph.session_store import DatabricksSessionStoreSaver
    from fastapi.testclient import TestClient
    from apx_agent import RuntimeRequirements, compile_agent

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    sessions: dict[str, Any] = {}
    items: dict[str, list[Any]] = {}
    root = "/api/2.0/agents/session-stores/managed-proof/sessions"

    def rest(method: str, path: str, *, body: Any = None, query: Any = None) -> Any:
        assert path.startswith(root)
        if method == "POST" and path == root:
            sid = query["session_id"]
            sessions[sid] = {"session_id": sid, **body}
            items.setdefault(sid, [])
            return copy.deepcopy(sessions[sid])
        sid = path.removeprefix(root + "/").split("/")[0]
        if sid not in sessions:
            raise NotFound("session missing")
        if method == "GET" and path.endswith("/items"):
            return {"session_items": copy.deepcopy(items[sid])}
        if method == "GET" and path == root + "/" + sid:
            return copy.deepcopy(sessions[sid])
        if method == "POST" and path.endswith("/items:append"):
            items[sid].extend(copy.deepcopy(body["items"]))
            return {}
        raise AssertionError(f"Unexpected SDK request: {method} {path}")

    execution.ws.api_client.do.side_effect = rest
    from apx_agent import AgentConfig

    options = {"config": AgentConfig(name="proof", target="durable_agent_server", model="test",
                                    session={"type": "managed", "store_name": "managed-proof"})} if declarative else {
        "target": "durable_agent_server", "model": "test", "session_store": "managed-proof",
    }
    for _ in range(2):
        app = compile_agent(
            execution.agent, **options, service_ws=execution.ws,
            requirements=RuntimeRequirements(sessions=True),
        )
        with TestClient(app) as client:
            response = client.post("/api/invocations", json={
                "id": str(uuid.uuid4()), "session_id": "conversation",
                "input": {"messages": [{"role": "user", "content": "record"}]},
            })
            assert response.status_code == 200, response.text
            assert response.json()["output"]["messages"][-1]["content"] == "Recorded proof"
    # A third fresh saver reads persisted graph state independently of the hosts.
    key = hashlib.sha256(json.dumps(["proof", "conversation"]).encode()).hexdigest()
    saver = DatabricksSessionStoreSaver("managed-proof", workspace_client=execution.ws)
    checkpoint = saver.get_tuple({"configurable": {"thread_id": key, "actor_id": key}})
    assert checkpoint is not None
    history = checkpoint.checkpoint["channel_values"]["messages"]
    assert sum(message.type == "human" for message in history) == 2
    assert execution.effects == ["proof", "proof"]
    assert any(item["data"]["event_type"] == "checkpoint" for saved in items.values() for item in saved)


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
    assert selected["config"] is config
    assert selected["service_ws"] is execution.ws
    assert namespace["app"] == "native-app"


def test_local_run_uses_declared_native_target(execution: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import sys
    from click.testing import CliRunner
    from apx_agent import AgentConfig
    from apx_agent._project_gen import generate_project
    from apx_agent.cli import main

    generate_project(AgentConfig(name="local", target="durable_agent_server",
                                session={"type": "managed", "store_name": "remote-sessions"}), tmp_path)
    monkeypatch.chdir(tmp_path)
    uvicorn = MagicMock()
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setattr("apx_agent.cli._preflight_databricks_auth", lambda: None)
    monkeypatch.setattr("apx_agent.cli._probe_import", lambda _: None)
    result = CliRunner().invoke(main, ["agents", "run", "--reload"])
    assert result.exit_code == 0, result.output
    assert uvicorn.run.call_args.args[0] == "apx_agent._serve:create_app"
    assert uvicorn.run.call_args.kwargs["factory"] is True
    assert uvicorn.run.call_args.kwargs["reload"] is True
    assert uvicorn.run.call_args.kwargs["app_dir"] == str(tmp_path)
    assert not (tmp_path / "agent_server").exists()
    from apx_agent._inspection import _load_agent_config

    assert _load_agent_config(pyproject_path=tmp_path / "pyproject.toml").session.store_name == "remote-sessions"


@pytest.mark.parametrize("target,entrypoint", [
    ("responses_agent", "agent_server.start_server:app"),
    ("durable_agent_server", "agent_server.start_managed:app"),
])
def test_generated_host_uses_declaration_without_environment_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str, entrypoint: str) -> None:
    from apx_agent import AgentConfig
    from apx_agent._project_gen import generate_project

    # Named-space Bundle projects retain the existing generated launchers.
    generate_project(AgentConfig(name="native", target=target,
                                deploy={"space": "existing-space"} if target == "durable_agent_server" else None), tmp_path)
    monkeypatch.setenv("APX_PYPROJECT", str(tmp_path / "pyproject.toml"))
    monkeypatch.delenv("APX_APPS_HOST", raising=False)
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8123")
    commands: list[Any] = []

    def launch(binary: str, args: list[str]) -> None:
        commands.append(args)
        raise SystemExit(0)

    monkeypatch.setattr("os.execvp", launch)
    source = tmp_path / "agent_server" / "start_host.py"
    namespace: dict[str, Any] = {"__name__": "selector"}
    exec(compile(source.read_text(), str(source), "exec"), namespace)
    with pytest.raises(SystemExit) as result:
        namespace["main"]()
    assert result.value.code == 0
    assert commands[0] == ["uvicorn", entrypoint, "--host", "0.0.0.0", "--port", "8123"]


def test_packaged_launcher_serves_native_invocations_without_generated_server(execution: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import sys
    from fastapi.testclient import TestClient
    from ctk import Artifact, verify
    from apx_agent import AgentConfig
    from apx_agent._project_gen import generate_project
    from apx_agent._serve import create_app

    generate_project(AgentConfig(name="proof", target="durable_agent_server", model="test"), tmp_path)
    verify(Artifact(str(tmp_path / "agent.py"), must_contain="LlmAgent"))
    assert not (tmp_path / "agent_server").exists()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace(agent=execution.agent))
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("AGENT_SESSION_STORE", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    monkeypatch.setenv("APX_APPS_HOST", "python")
    monkeypatch.setenv("APX_PYPROJECT", str(tmp_path / "unrelated.toml"))
    sessions: dict[str, Any] = {}
    items: dict[str, list[Any]] = {}
    from databricks.sdk.errors import NotFound

    def rest(method: str, path: str, *, body: Any = None, query: Any = None) -> Any:
        root = "/api/2.0/agents/session-stores/apx-proof-sessions/sessions"
        if not path.startswith(root):
            raise AssertionError(path)
        if method == "POST" and path == root:
            session_id = query["session_id"]
            sessions[session_id] = {"session_id": session_id, **body}
            items.setdefault(session_id, [])
            return dict(sessions[session_id])
        session_id = path.removeprefix(root + "/").split("/")[0]
        if session_id not in sessions:
            raise NotFound("session missing")
        if method == "GET" and path.endswith("/items"):
            return {"session_items": list(items[session_id])}
        if method == "GET" and path == root + "/" + session_id:
            return dict(sessions[session_id])
        if method == "POST" and path.endswith("/items:append"):
            items[session_id].extend(body["items"])
            return {}
        raise AssertionError(f"{method} {path}")

    execution.ws.api_client.do.side_effect = rest
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
        response = client.post("/api/invocations", json={
            "id": str(uuid.uuid4()), "session_id": "conversation",
            "input": {"messages": [{"role": "user", "content": "record"}]},
        })
        assert response.status_code == 200, response.text
        assert response.json()["output"]["messages"][-1]["content"] == "Recorded proof"
    assert execution.effects == ["proof"]


def test_packaged_launcher_preserves_remote_session_binding(execution: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import sys
    from apx_agent import AgentConfig
    from apx_agent._project_gen import generate_project
    from apx_agent._serve import create_app

    config = AgentConfig(name="proof", target="durable_agent_server", model="declared-model",
                         session={"type": "managed", "store_name": "remote-sessions"})
    generate_project(config, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace(agent=execution.agent))
    monkeypatch.setenv("APX_PYPROJECT", str(tmp_path / "pyproject.toml"))
    monkeypatch.setenv("APX_MODEL", "bound-model")
    monkeypatch.setenv("AGENT_SESSION_STORE", "remote-sessions")
    native_app = SimpleNamespace(state=SimpleNamespace())
    compiler = MagicMock(return_value=native_app)
    monkeypatch.setattr("apx_agent._runtime_targets.compile_agent", compiler)
    assert create_app() is native_app
    assert native_app.state.workspace_client is execution.ws
    compiler.assert_called_once_with(execution.agent, config=config, model="bound-model",
                                    service_ws=execution.ws, session_store="remote-sessions")


@pytest.mark.parametrize("declared", [False, True])
def test_packaged_launcher_rejects_missing_or_other_runtime_before_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, declared: bool) -> None:
    from apx_agent import AgentConfig
    from apx_agent._project_gen import generate_project
    from apx_agent._serve import create_app

    if declared:
        generate_project(AgentConfig(name="other"), tmp_path)
    monkeypatch.chdir(tmp_path)
    auth = MagicMock(side_effect=AssertionError("No auth for an incompatible runtime"))
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", auth)
    with pytest.raises(ValueError, match="native launcher requires"):
        create_app()
    auth.assert_not_called()


def test_packaged_deployment_launcher_uses_same_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from apx_agent._serve import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8123")
    launch = MagicMock()
    monkeypatch.setattr("uvicorn.run", launch)
    main()
    launch.assert_called_once_with("apx_agent._serve:create_app", factory=True,
                                   host="0.0.0.0", port=8123, app_dir=str(tmp_path))


def test_declared_native_project_detection_and_discovery_precede_filenames(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from apx_agent import AgentConfig
    from apx_agent._project_gen import generate_project
    from apx_agent.cli import _detect_target, _find_runnable_agents, _preflight_apps

    generate_project(AgentConfig(name="native", target="durable_agent_server"), tmp_path)
    # A stray legacy-looking file must not select model-serving.
    (tmp_path / "app.py").write_text("raise AssertionError('unused legacy module')\n")
    assert _detect_target(tmp_path) == ("apps", "declared durable_agent_server runtime")
    assert _find_runnable_agents(tmp_path) == [(tmp_path.name, tmp_path)]
    _preflight_apps(tmp_path, native=True)


@pytest.mark.parametrize("override", [
    {"session_store": "different-store"},
    {"checkpointer": InMemorySaver()},
    {"target": "responses_agent"},
])
def test_declared_managed_sessions_reject_conflicting_bindings(execution: Any, override: dict[str, Any]) -> None:
    from apx_agent import AgentConfig, compile_agent

    config = AgentConfig(name="proof", target="durable_agent_server", model="test",
                         session={"type": "managed", "store_name": "managed-proof"})
    with pytest.raises(ValueError):
        compile_agent(execution.agent, config=config, service_ws=execution.ws, **override)
    assert not execution.effects


@pytest.mark.parametrize("example", ["plg-discovery", "contract-parsing-agent"])
def test_example_native_app_serves_ui_without_intercepting_invocations(
    example: str, execution: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import runpy
    import sys
    from fastapi import APIRouter
    from fastapi.testclient import TestClient
    from apx_agent import compile_agent

    source = Path(__file__).parents[1] / "examples" / example / "app.py"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    native = compile_agent(execution.agent, target="durable_agent_server", model="test",
                           service_ws=execution.ws, checkpointer=InMemorySaver())
    monkeypatch.setattr("apx_agent._serve.create_app", lambda: native)
    # Business route dependencies are checked separately; exercise route ordering
    # against the real SDK ingress and graph here without a remote warehouse.
    router = APIRouter()

    @router.get("/api/business-proof")
    def business() -> dict[str, bool]:
        return {"ok": True}

    monkeypatch.setitem(sys.modules, "api", SimpleNamespace(router=router))
    dist = tmp_path / "client" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<h1>Native browser proof</h1>")
    (tmp_path / "private.txt").write_text("do not serve")
    app = runpy.run_path(str(source))["app"]
    assert app is native
    with TestClient(app) as client:
        assert "Native browser proof" in client.get("/").text
        assert client.get("/readyz").status_code == 200
        assert client.get("/%2e%2e/private.txt").status_code == 404
        if example == "contract-parsing-agent":
            assert client.get("/api/business-proof").json() == {"ok": True}
        for _ in range(2):
            response = client.post("/api/invocations", json={
                "id": str(uuid.uuid4()), "session_id": "browser-session",
                "input": {"messages": [{"role": "user", "content": "record"}]},
            })
            assert response.status_code == 200, response.text
            assert response.json()["output"]["messages"][-1]["content"] == "Recorded proof"
    assert execution.effects == ["proof", "proof"]


@pytest.mark.parametrize("example", ["plg-discovery", "contract-parsing-agent"])
def test_native_example_build_stages_sources_without_a_tool_bridge(example: str, tmp_path: Path) -> None:
    import ast
    import shutil
    import subprocess
    import yaml
    from ctk import Artifact, verify

    source = Path(__file__).parents[1] / "examples" / example
    project = tmp_path / example
    shutil.copytree(source, project, ignore=shutil.ignore_patterns("node_modules", ".venv", ".build", "dist", "__pycache__"))
    (project / "client" / "dist").mkdir()
    (project / "client" / "dist" / "index.html").write_text("browser proof")
    doc = yaml.safe_load((project / "databricks.yml").read_text())
    build = doc["artifacts"]["default"]["build"]
    # Wheel resolution/building is independent. Exercise the authored staging
    # commands with prebuilt wheel placeholders and real example source files.
    commands = [line for line in build.splitlines() if not line.startswith(("uv build", "basename"))]
    subprocess.run(["sh", "-ec", "\n".join(commands)], cwd=project, check=True)
    staged = project / ".build"
    for name in ("app.py", "agent.py"):
        verify(Artifact(str(staged / name), must_contain="apx_agent"))
        ast.parse((staged / name).read_text())
    verify(Artifact(str(staged / "client" / "dist" / "index.html"), must_contain="browser proof"))
    if example == "plg-discovery":
        assert (staged / "prompts" / "discovery_playbook.md").is_file()
        assert (staged / "server" / "grounding.py").is_file()
    else:
        assert (staged / "tools" / "query_portfolio.py").is_file()
        assert (staged / "api.py").is_file()
        assert (staged / "agent.config.yaml").is_file()
    assert not (staged / "apx_appkit_host").exists()
