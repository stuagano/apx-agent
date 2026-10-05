"""Recover real governed graphs using fresh SDK savers over persisted REST records."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from databricks.sdk.errors import NotFound
from databricks_agentkit.langgraph.session_store import DatabricksSessionStoreSaver
from langchain_core.messages import ToolMessage

from apx_agent import RuntimeRequirements, inspect_target
from apx_agent._durable_agent import compile_durable_handlers
from tests.test_runtime_targets import ToolModel, execution  # noqa: F401


@pytest.fixture
def checkpoints(execution: Any) -> Any:
    sessions: dict[str, Any] = {}
    items: dict[str, list[Any]] = {}
    root = "/api/2.0/agents/session-stores/recovery-proof/sessions"

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
    return lambda: DatabricksSessionStoreSaver("recovery-proof", workspace_client=execution.ws)


def context(invocation_id: str = "invocation-one", *, recovery: bool = False) -> Any:
    return SimpleNamespace(invocation_id=invocation_id, session_id="session-one",
                           request_auth=None, is_recovery=recovery)


def handlers(execution: Any, checkpoints: Any) -> Any:
    return compile_durable_handlers(execution.agent, model="test", service_ws=execution.ws,
                                    checkpointer=checkpoints(), recovery=True)


PAYLOAD = [{"role": "user", "content": "record"}]


def test_recovery_continues_after_committed_tool_without_repeating_it(
    execution: Any, checkpoints: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = False

    class InterruptedModel(ToolModel):
        def _generate(self, messages: Any, **kwargs: Any) -> Any:
            nonlocal failed
            if isinstance(messages[-1], ToolMessage) and not failed:
                failed = True
                raise ConnectionError("worker lost after tool checkpoint")
            return super()._generate(messages, **kwargs)

    execution.agent._session_budget = {"tokens": 10}
    monkeypatch.setattr("apx_agent._compile._build_chat_databricks", lambda *a, **k: InterruptedModel())
    with pytest.raises(ConnectionError, match="worker lost"):
        asyncio.run(handlers(execution, checkpoints).invoke(PAYLOAD, context()))
    assert execution.effects == ["proof"]

    # Rebuild the handler and SDK saver: no process-local checkpoint cache survives.
    result = asyncio.run(handlers(execution, checkpoints).invoke(PAYLOAD, context(recovery=True)))
    assert result["status"] == "completed"
    assert result["messages"][-1]["content"] == "Recorded proof"
    assert execution.effects == ["proof"]
    repeated = asyncio.run(handlers(execution, checkpoints).invoke(PAYLOAD, context(recovery=True)))
    assert repeated == result
    assert execution.effects == ["proof"]

    key = hashlib.sha256(json.dumps(["proof", "session-one"]).encode()).hexdigest()
    saved = checkpoints().get_tuple({"configurable": {"thread_id": key, "actor_id": key}})
    assert saved is not None
    values = saved.checkpoint["channel_values"]
    assert values["state"]["session_tokens"] == 4
    assert sum(message.type == "human" for message in values["messages"]) == 1
    with pytest.raises(ValueError, match="input differs"):
        asyncio.run(handlers(execution, checkpoints).invoke(
            [{"role": "user", "content": "changed"}], context(recovery=True),
        ))


def test_recovery_without_current_checkpoint_replays_input(
    execution: Any, checkpoints: Any,
) -> None:
    result = asyncio.run(handlers(execution, checkpoints).invoke(PAYLOAD, context(recovery=True)))
    assert result["status"] == "completed"
    assert execution.effects == ["proof"]
    asyncio.run(handlers(execution, checkpoints).invoke(PAYLOAD, context("invocation-two")))
    with pytest.raises(ValueError, match="newer session"):
        asyncio.run(handlers(execution, checkpoints).invoke(PAYLOAD, context(recovery=True)))
    assert execution.effects == ["proof", "proof"]


def test_recovery_preserves_approval_and_resume(execution: Any, checkpoints: Any) -> None:
    from apx_agent import FunctionPolicy, PolicyAction, PolicyGate, PolicyResult

    execution.agent._session_budget = {"tokens": 10}
    execution.agent._before_tool = PolicyGate([
        FunctionPolicy(lambda event: PolicyResult(action=PolicyAction.ASK, reason="approval required")),
    ])
    first = asyncio.run(handlers(execution, checkpoints).invoke(PAYLOAD, context()))
    assert first["status"] == "interrupted"
    recovered = asyncio.run(handlers(execution, checkpoints).invoke(PAYLOAD, context(recovery=True)))
    assert recovered["status"] == "interrupted"
    assert execution.effects == []
    approved = {"resume": "approve"}
    resumed = asyncio.run(handlers(execution, checkpoints).invoke(approved, context("approval-two")))
    assert resumed["status"] == "completed"
    repeated = asyncio.run(handlers(execution, checkpoints).invoke(
        approved, context("approval-two", recovery=True),
    ))
    assert repeated == resumed
    assert execution.effects == ["proof"]


def test_recovery_compatibility_and_registration(
    execution: Any, checkpoints: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    from apx_agent import AgentConfig, compile_agent
    from apx_agent._project_gen import generate_project
    import tomllib

    report = inspect_target(execution.agent, target="durable_agent_server", session_store="recovery-proof",
                            requirements=RuntimeRequirements(recovery=True))
    assert not report.unsatisfied and report.recovery_enabled
    user_report = inspect_target(execution.agent, target="durable_agent_server", session_store="recovery-proof",
                                 requirements=RuntimeRequirements(recovery=True, user_identity=True))
    assert "recovery" in user_report.unsatisfied
    for recovering in (False, True):
        user_context = context(recovery=recovering)
        user_context.request_auth = SimpleNamespace()
        with pytest.raises(ValueError, match="Request-user recovery"):
            asyncio.run(handlers(execution, checkpoints).invoke(PAYLOAD, user_context))
    assert execution.effects == []
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    config = AgentConfig(name="proof", target="durable_agent_server", model="test", recovery=True,
                         session={"type": "managed", "store_name": "recovery-proof"})
    app = compile_agent(execution.agent, config=config, service_ws=execution.ws)
    assert app._recovery_hook is not None
    generate_project(config, tmp_path / "generated")
    project = tomllib.loads((tmp_path / "generated" / "pyproject.toml").read_text())
    assert project["tool"]["apx"]["agent"]["recovery"] is True


def test_uncertain_tool_write_uses_the_tools_idempotency_contract(
    execution: Any, checkpoints: Any,
) -> None:
    from apx_agent import LlmAgent

    records: dict[str, str] = {}
    attempts: list[str] = []

    def record(value: str) -> str:
        """Write a fact idempotently using its stable business key."""
        attempts.append(value)
        if value not in records:
            records[value] = value
            raise ConnectionError("worker lost after external write, before checkpoint")
        return records[value]

    execution.agent = LlmAgent(name="proof", tools=[record])
    with pytest.raises(ConnectionError, match="before checkpoint"):
        asyncio.run(handlers(execution, checkpoints).invoke(PAYLOAD, context()))
    result = asyncio.run(handlers(execution, checkpoints).invoke(PAYLOAD, context(recovery=True)))
    assert result["status"] == "completed"
    assert attempts == ["proof", "proof"]
    assert records == {"proof": "proof"}


def test_budget_checkpoint_failure_propagates_and_recovers_without_recount(
    execution: Any, checkpoints: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution.agent._session_budget = {"tokens": 10}
    saver = checkpoints()
    original_put = saver.put

    def fail_budget(config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        if metadata.get("source") == "update":
            raise ConnectionError("budget checkpoint unavailable")
        return original_put(config, checkpoint, metadata, new_versions)

    monkeypatch.setattr(saver, "put", fail_budget)
    with pytest.raises(ConnectionError, match="budget checkpoint"):
        asyncio.run(handlers(execution, lambda: saver).invoke(PAYLOAD, context()))
    assert execution.effects == ["proof"]
    assert asyncio.run(handlers(execution, checkpoints).invoke(PAYLOAD, context(recovery=True)))["status"] == "completed"
    assert execution.effects == ["proof"]


def test_recovery_cannot_bypass_an_exceeded_budget(execution: Any, checkpoints: Any) -> None:
    from apx_agent import SessionBudgetExceeded

    execution.agent._session_budget = {"tokens": 3}
    for attempt in (context(), context(recovery=True), context("invocation-two")):
        with pytest.raises(SessionBudgetExceeded):
            asyncio.run(handlers(execution, checkpoints).invoke(PAYLOAD, attempt))
    assert execution.effects == ["proof"]


def test_native_sdk_recovery_dispatch_restores_completed_output(
    execution: Any, checkpoints: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    from apx_agent import compile_agent
    from databricks_agentkit.runtime.types import InvocationAttemptContext

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    events: list[Any] = []

    async def emit(event: Any) -> int:
        events.append(event)
        return len(events)

    results = []
    for attempt in (1, 2):
        app = compile_agent(execution.agent, target="durable_agent_server", model="test",
                            service_ws=execution.ws, checkpointer=checkpoints(),
                            requirements=RuntimeRequirements(recovery=True))
        results.append(asyncio.run(app._execute(
            {"input": PAYLOAD},
            InvocationAttemptContext(invocation_id="dispatch-one", session_id="session-one",
                                     attempt=attempt, _emit=emit),
        )))
    assert results[0] == results[1]
    assert results[1]["messages"][-1]["content"] == "Recorded proof"
    assert execution.effects == ["proof"]
