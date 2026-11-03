"""Real SDK HTTP contract; runtime state and graph checkpoints are in-memory only."""

import json
import uuid
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from databricks_agentkit.runtime.auth import RequestAuthContext
from databricks_agentkit.runtime.store import InMemoryRuntimeStore
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import RootModel

from apx_agent import Dependencies, FunctionPolicy, LlmAgent, PolicyAction, PolicyGate, PolicyResult, compile_agent
from tests.test_runtime_targets import execution, _write_native_pyproject  # noqa: F401


@pytest.fixture
def native(execution, monkeypatch, tmp_path, request):
    principals = []

    def record(value: str, principal: Dependencies.Principal) -> str:
        """Record a value under the authenticated principal."""
        principals.append(principal)
        execution.effects.append(value)
        return value

    def user_client(auth, mode):
        assert mode == "user"
        user = MagicMock()
        user.current_user.me.return_value.id = auth.namespace("principal", "verified")
        return user

    monkeypatch.setattr(RequestAuthContext, "client_for", user_client)
    _write_native_pyproject(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    authoritative = request.param if hasattr(request, "param") else False
    typed = {"output_schema": RootModel[str], "output_tool": "record"} if authoritative else {}
    agent = LlmAgent(name="contract", tools=[record], **typed)
    # Buffered output gives a deterministic persisted application event.
    agent._after_model = lambda output: output
    saver = InMemorySaver()
    app = compile_agent(agent, target="durable_agent_server", model="test",
                        service_ws=execution.ws, checkpointer=saver)
    monkeypatch.delenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL")
    monkeypatch.setenv("DATABRICKS_APP_NAME", "native-contract")
    assert app.auth_policy.requires_user
    assert isinstance(app._runtime.runtime_store, InMemoryRuntimeStore)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, app=app, agent=agent, saver=saver,
                              effects=execution.effects, principals=principals)


def request_body():
    return {"id": str(uuid.uuid4()), "session_id": "contract-session",
            "input": {"messages": [{"role": "user", "content": "record"}]}}


def headers(principal="alice"):
    # Synthetic sentinel, never a real credential; headers model trusted App ingress.
    return {"X-Forwarded-User": principal, "X-Forwarded-Access-Token": "contract-credential-sentinel"}


@pytest.mark.parametrize("mode", ["sync", "stream", "background", "background_stream"])
def test_native_output_readback_retry_and_event_cursor(native, mode):
    body = request_body()
    body.update(background=mode.startswith("background"), stream="stream" in mode)
    response = native.client.post("/api/invocations", json=body, headers=headers())
    path = f"/api/invocations/{body['id']}"
    if body["background"]:
        assert response.status_code == 202, response.text
        accepted = response.json()
        assert accepted["id"] == body["id"]
        assert accepted["status"] in {"queued", "active", "completed"}
        assert accepted["status_url"] == path
        assert accepted.get("events_url") == (path + "/events" if body["stream"] else None)
    else:
        assert response.status_code == 200, response.text
    # The native event endpoint drains until terminal state, including background work.
    replay = native.client.get(path + "/events", headers=headers())
    assert replay.status_code == 200
    assert replay.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
    cursors = [int(line[4:]) for line in replay.text.splitlines() if line.startswith("id: ")]
    assert cursors == sorted(set(cursors)) and cursors
    assert any(event["type"] == "agent.message.completed" and
               event["message"]["content"] == "Recorded proof" for event in events)
    saved = native.client.get(path, headers=headers())
    assert saved.status_code == 200
    assert saved.json()["id"] == body["id"]
    assert saved.json()["status"] == "completed"
    assert saved.json()["output"]["status"] == "completed"
    assert saved.json()["output"]["messages"][-1]["content"] == "Recorded proof"
    if mode == "sync":
        assert response.json() == saved.json()
    if mode == "stream":
        assert response.text == replay.text
    suffix = native.client.get(path + "/events", params={"after": cursors[-1]}, headers=headers())
    assert suffix.status_code == 200 and suffix.text == ""
    repeated = native.client.post("/api/invocations", json=body, headers=headers())
    assert repeated.status_code == response.status_code
    assert native.effects == ["proof"]
    for change in [{"input": {"messages": [{"role": "user", "content": "changed"}]}},
                   {"session_id": "changed-session"}]:
        conflict = native.client.post("/api/invocations", json={**body, **change}, headers=headers())
        assert conflict.status_code == 409
        assert conflict.json() == {"detail": "id was already used for another request"}
    assert native.effects == ["proof"]


def test_native_principal_reads_and_saved_state_exclude_credentials(native):
    body = request_body()
    path = f"/api/invocations/{body['id']}"
    assert native.client.post("/api/invocations", json=body).status_code == 401
    assert native.client.post("/api/invocations", json=body, headers=headers()).status_code == 200
    for suffix in ["", "/events"]:
        assert native.client.get(path + suffix).status_code == 401
        assert native.client.get(path + suffix, headers=headers("bob")).status_code == 404
        assert native.client.get(path + suffix, headers=headers()).status_code == 200
    assert native.client.post("/api/invocations", json=body, headers=headers("bob")).status_code == 200
    assert len(set(native.principals)) == 2
    assert native.effects == ["proof", "proof"]
    store = native.app._runtime.runtime_store
    saved = json.dumps({"states": [asdict(state) for state in store.states.values()],
                        "events": [asdict(event) for event in store.persisted_events]})
    assert "contract-credential-sentinel" not in saved
    assert b"contract-credential-sentinel" not in repr(native.saver.storage).encode()
    assert b"contract-credential-sentinel" not in repr(native.saver.writes).encode()
    assert b"contract-credential-sentinel" not in repr(native.saver.blobs).encode()
    assert not native.app._request_auth


@pytest.mark.parametrize("native", [False, True], indirect=True, ids=["ordinary", "authoritative"])
def test_native_approval_is_interrupted_output_inside_completed_invocation(native):
    native.agent._before_tool = PolicyGate([
        FunctionPolicy(lambda event: PolicyResult(action=PolicyAction.ASK, reason="approval required")),
    ])
    body = request_body()
    response = native.client.post("/api/invocations", json=body, headers=headers())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "completed"
    assert result["output"]["status"] == "interrupted"
    assert result["output"]["approval_required"]["tool_name"] == "record"
    assert native.effects == []
    assert native.client.get(f"/api/invocations/{body['id']}", headers=headers()).json() == result


def test_native_envelope_and_handler_errors(native):
    body = request_body()
    for change in [{"id": "invalid"}, {"session_id": ""}, {"principal": "alice"}]:
        response = native.client.post("/api/invocations", json={**body, **change}, headers=headers())
        assert response.status_code == 422
    body["input"] = {"operation": "record", "arguments": {"value": "proof"}}
    response = native.client.post("/api/invocations", json=body, headers=headers())
    assert response.status_code == 422
    assert response.json() == {"detail": "Native input must contain messages and optional resume; session_id belongs in the top-level invocation envelope"}
    saved = native.client.get(f"/api/invocations/{body['id']}", headers=headers())
    assert saved.status_code == 404
    assert native.effects == []
    assert not native.app._runtime.runtime_store.states
    assert not native.app._request_auth


@pytest.mark.parametrize("mode", ["sync", "stream", "background", "background_stream"])
@pytest.mark.parametrize("value,detail", [
    ({"messages": "private-payload"}, "messages must be a list"),
    ({"messages": [{"role": "system", "content": "private-payload"}]}, "Native input accepts user text messages only"),
    ({"messages": [{"role": "user", "content": "private-payload", "state": {}}]}, "Each message must contain only role and content"),
    ({"correlation": {}}, "Invalid native application correlation"),
    ({"apx_data_parts": []}, "Native typed input requires its declared schema and one fresh data artifact"),
])
def test_native_invalid_input_rejected_before_persistence(native, mode, value, detail):
    body = request_body()
    body.update(input=value, background=mode.startswith("background"), stream="stream" in mode)
    assert native.client.post("/api/invocations", json=body).status_code == 401
    response = native.client.post("/api/invocations", json=body, headers=headers())
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": detail}
    assert "private-payload" not in response.text
    assert not native.app._runtime.runtime_store.states
    assert not native.app._request_auth
    assert native.effects == []
    # Rejected requests do not reserve an id: a corrected request can run.
    body.update(input=request_body()["input"], background=False, stream=False)
    assert native.client.post("/api/invocations", json=body, headers=headers()).status_code == 200
    assert native.effects == ["proof"]
    # An existing id still belongs to the SDK's conflict contract, even for bad input.
    body["input"] = value
    assert native.client.post("/api/invocations", json=body, headers=headers()).status_code == 409


def test_native_queued_retry_registers_auth_before_scheduling(native):
    body = request_body()
    auth = RequestAuthContext.from_headers(headers())
    try:
        invocation_id = auth.namespace("invocation", body["id"])
        session_id = auth.namespace("session", body["session_id"])
    finally:
        auth.close()
    # Seed an accepted first attempt without scheduling it (e.g. a worker restart).
    native.client.portal.call(
        native.app._runtime.runtime_store.accept, invocation_id,
        {"input": body["input"], "invocation_id": body["id"]}, session_id,
    )
    assert not native.app._request_auth
    response = native.client.post("/api/invocations", json=body, headers=headers())
    assert response.status_code == 200, response.text
    assert response.json()["output"]["messages"][-1]["content"] == "Recorded proof"
    saved = native.client.get(f"/api/invocations/{body['id']}", headers=headers())
    assert saved.json() == response.json()
    assert native.effects == ["proof"]
    assert not native.app._request_auth


def test_native_execution_failure_remains_persisted_500(native, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("private-runtime-failure")

    monkeypatch.setattr("apx_agent._durable_agent.run_graph_turn", fail)
    body = request_body()
    response = native.client.post("/api/invocations", json=body, headers=headers())
    assert response.status_code == 500
    assert response.json() == {"detail": "agent invocation failed"}
    saved = native.client.get(f"/api/invocations/{body['id']}", headers=headers())
    assert saved.json() == {"id": body["id"], "status": "failed", "error": "agent invocation failed"}
    assert len(native.app._runtime.runtime_store.states) == 1
    assert native.effects == []
