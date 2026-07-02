"""Tests for the A2A v0.3.0 task surface — ``_a2a.py`` + ``_a2a_models.py``.

The A2A JSON-RPC surface is mounted at ``POST /`` and backs the discovery card's
capability claims with the actual protocol: ``message/send`` runs the same agent
``/invocations`` runs, ``tasks/get`` reads it back, ``tasks/cancel`` reports
sync-complete tasks as terminal. See docs/design/a2a-tasks-surface.md.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("mlflow")

from fastapi.testclient import TestClient  # noqa: E402

from apx_agent import AgentConfig, LlmAgent, create_app  # noqa: E402
from apx_agent._a2a import TaskStore  # noqa: E402
from apx_agent._a2a_models import (  # noqa: E402
    Artifact,
    Message,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)


def _trivial_tool(query: str) -> str:
    """Return the query, echoing back."""
    return f"got: {query}"


@pytest.fixture
def a2a_client():
    """A ``create_app`` instance with the A2A surface mounted, plus the captured
    ChatAgent so tests can stub ``predict``. A2A mounts after /invocations, so
    ``captured['chat_agent']`` is the instance the A2A handler uses."""
    agent = LlmAgent(tools=[_trivial_tool])
    config = AgentConfig(name="a2a-test", model="databricks-claude-sonnet-4-6")

    captured: dict[str, Any] = {}
    from apx_agent import _chat_agent as _ca_module

    original_factory = _ca_module.chat_agent_for

    def _spy_factory(agent_arg, *, model, conversation_store=None, agent_id=None, checkpointer=None):
        ca = original_factory(agent_arg, model=model)
        captured["chat_agent"] = ca
        ca.predict = MagicMock(name="mock_predict")
        return ca

    with patch.object(_ca_module, "chat_agent_for", side_effect=_spy_factory), patch(
        "apx_agent._wiring._make_workspace_client"
    ) as mock_ws_factory:
        mock_ws_factory.return_value = MagicMock(name="sp_ws")
        app = create_app(agent, config=config)
        with TestClient(app) as client:
            yield client, captured


def _stub_reply(captured: dict[str, Any], text: str) -> None:
    from mlflow.types.agent import ChatAgentMessage, ChatAgentResponse

    captured["chat_agent"].predict.return_value = ChatAgentResponse(
        messages=[ChatAgentMessage(role="assistant", content=text, id="m1")]
    )


def _rpc(client: TestClient, method: str, params: dict | None = None, req_id: Any = 1):
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/", json=body)


def _send_params(text: str, context_id: str | None = None) -> dict:
    msg: dict[str, Any] = {
        "role": "user",
        "parts": [{"kind": "text", "text": text}],
        "messageId": "u1",
    }
    if context_id:
        msg["contextId"] = context_id
    return {"message": msg}


# ---------------------------------------------------------------------------
# Models + store units (no app)
# ---------------------------------------------------------------------------


class TestModels:
    def test_task_round_trips_camelcase(self):
        task = Task(
            id="t1",
            contextId="c1",
            status=TaskStatus(state=TaskState.completed),
            artifacts=[Artifact(artifactId="a1", parts=[TextPart(text="hi")])],
        )
        dumped = task.model_dump(mode="json")
        assert dumped["contextId"] == "c1"
        assert dumped["status"]["state"] == "completed"
        assert dumped["artifacts"][0]["artifactId"] == "a1"
        assert dumped["kind"] == "task"
        # Re-parse to prove the wire shape is self-consistent.
        assert Task(**dumped).id == "t1"

    def test_message_text_concatenates_parts(self):
        m = Message(
            role="user",
            parts=[TextPart(text="foo "), TextPart(text="bar")],
            messageId="u1",
        )
        assert m.text() == "foo bar"


class TestTaskStore:
    def test_put_get(self):
        store = TaskStore()
        task = Task(id="t1", contextId="c1", status=TaskStatus(state=TaskState.completed))
        store.put(task)
        assert store.get("t1") is task
        assert store.get("missing") is None

    def test_evicts_oldest_over_capacity(self):
        store = TaskStore(max_tasks=2)
        for i in range(3):
            store.put(
                Task(id=f"t{i}", contextId="c", status=TaskStatus(state=TaskState.completed))
            )
        assert store.get("t0") is None  # evicted
        assert store.get("t1") is not None
        assert store.get("t2") is not None


# ---------------------------------------------------------------------------
# message/send
# ---------------------------------------------------------------------------


class TestMessageSend:
    def test_returns_completed_task_with_reply(self, a2a_client):
        client, captured = a2a_client
        _stub_reply(captured, "the answer is 42")

        resp = _rpc(client, "message/send", _send_params("what is 6*7?"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 1
        task = body["result"]
        assert task["kind"] == "task"
        assert task["status"]["state"] == "completed"
        assert task["artifacts"][0]["parts"][0]["text"] == "the answer is 42"
        # History carries the inbound user turn + the agent reply.
        roles = [m["role"] for m in task["history"]]
        assert roles == ["user", "agent"]
        assert task["history"][1]["parts"][0]["text"] == "the answer is 42"

    def test_task_is_fetchable_via_tasks_get(self, a2a_client):
        client, captured = a2a_client
        _stub_reply(captured, "stored reply")

        sent = _rpc(client, "message/send", _send_params("hi")).json()["result"]
        task_id = sent["id"]

        got = _rpc(client, "tasks/get", {"id": task_id}).json()
        assert got["result"]["id"] == task_id
        assert got["result"]["status"]["state"] == "completed"

    def test_agent_received_message_text(self, a2a_client):
        client, captured = a2a_client
        _stub_reply(captured, "ok")
        _rpc(client, "message/send", _send_params("hello there"))
        # The predict call got a single user ChatAgentMessage with our text.
        call = captured["chat_agent"].predict.call_args
        msgs = call.args[0]
        assert msgs[0].role == "user"
        assert msgs[0].content == "hello there"

    def test_contextid_threads_to_session_id(self, a2a_client):
        client, captured = a2a_client
        _stub_reply(captured, "ok")
        _rpc(client, "message/send", _send_params("hi", context_id="ctx-7"))
        custom_inputs = captured["chat_agent"].predict.call_args.kwargs["custom_inputs"]
        assert custom_inputs["session_id"] == "ctx-7"

    def test_execution_failure_returns_failed_task_not_500(self, a2a_client):
        client, captured = a2a_client
        captured["chat_agent"].predict.side_effect = RuntimeError("model exploded")

        resp = _rpc(client, "message/send", _send_params("boom"))
        assert resp.status_code == 200
        task = resp.json()["result"]
        assert task["status"]["state"] == "failed"
        assert "model exploded" in task["status"]["message"]["parts"][0]["text"]


# ---------------------------------------------------------------------------
# tasks/get + tasks/cancel
# ---------------------------------------------------------------------------


class TestTasksGetCancel:
    def test_get_unknown_task_is_task_not_found(self, a2a_client):
        client, _ = a2a_client
        body = _rpc(client, "tasks/get", {"id": "nope"}).json()
        assert body["error"]["code"] == -32001

    def test_cancel_unknown_task_is_task_not_found(self, a2a_client):
        client, _ = a2a_client
        body = _rpc(client, "tasks/cancel", {"id": "nope"}).json()
        assert body["error"]["code"] == -32001

    def test_cancel_terminal_task_is_not_cancelable(self, a2a_client):
        client, captured = a2a_client
        _stub_reply(captured, "done")
        task_id = _rpc(client, "message/send", _send_params("hi")).json()["result"]["id"]

        body = _rpc(client, "tasks/cancel", {"id": task_id}).json()
        assert body["error"]["code"] == -32002


# ---------------------------------------------------------------------------
# JSON-RPC framing
# ---------------------------------------------------------------------------


class TestJsonRpcFraming:
    def test_invalid_json_is_parse_error(self, a2a_client):
        client, _ = a2a_client
        resp = client.post("/", content=b"{not json", headers={"content-type": "application/json"})
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == -32700

    def test_missing_method_is_invalid_request(self, a2a_client):
        client, _ = a2a_client
        resp = client.post("/", json={"jsonrpc": "2.0", "id": 5})
        assert resp.json()["error"]["code"] == -32600
        # JSON-RPC id echoes at the envelope top level, not inside `error`.
        assert resp.json()["id"] == 5

    def test_unknown_method_is_method_not_found(self, a2a_client):
        client, _ = a2a_client
        # message/stream is advertised but not yet implemented — must be -32601.
        body = _rpc(client, "message/stream", _send_params("hi")).json()
        assert body["error"]["code"] == -32601

    def test_bad_params_is_invalid_params(self, a2a_client):
        client, _ = a2a_client
        # message/send with no `message` field.
        body = _rpc(client, "message/send", {"not_message": 1}).json()
        assert body["error"]["code"] == -32602

    def test_notification_runs_side_effect_but_returns_no_body(self, a2a_client):
        client, captured = a2a_client
        _stub_reply(captured, "noti")
        # No `id` member → JSON-RPC notification: the server MUST run it but MUST
        # NOT reply. The agent still gets invoked; the HTTP response carries no body.
        resp = client.post(
            "/",
            json={"jsonrpc": "2.0", "method": "message/send", "params": _send_params("hi")},
        )
        assert resp.status_code == 204
        assert resp.content == b""
        captured["chat_agent"].predict.assert_called_once()

    def test_notification_for_error_method_still_has_no_body(self, a2a_client):
        client, _ = a2a_client
        # Even an error-producing method gets no response when sent as a notification.
        resp = client.post("/", json={"jsonrpc": "2.0", "method": "bogus/method"})
        assert resp.status_code == 204
        assert resp.content == b""

    def test_explicit_null_id_still_gets_a_response(self, a2a_client):
        client, _ = a2a_client
        # `id: null` is present (a request, discouraged but valid), not a
        # notification — the server still replies, echoing the null id.
        resp = client.post("/", json={"jsonrpc": "2.0", "id": None, "method": "bogus/method"})
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# Mid-turn approval over A2A: input-required Task → resume via
# configuration.apx_resume. Drives the REAL ChatAgent (gated tool + fake model
# + checkpointer) end-to-end through the JSON-RPC route.
# ---------------------------------------------------------------------------

from fastapi import FastAPI  # noqa: E402
from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from apx_agent import _compile  # noqa: E402
from apx_agent._a2a import mount_a2a_route  # noqa: E402
from apx_agent._policy import (  # noqa: E402
    FunctionPolicy,
    PolicyAction,
    PolicyGate,
    PolicyResult,
)


class _ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


_sent: list[str] = []  # side-effect log so tests observe whether the tool RAN
                        # (the A2A Task surfaces only the final reply, not tool output)


def _send_email(to: str) -> str:
    """Send an email to the given address."""
    _sent.append(to)
    return f"sent to {to}"


def _ask_gate() -> PolicyGate:
    return PolicyGate([
        FunctionPolicy(
            lambda ev: PolicyResult(action=PolicyAction.ASK, reason="needs human ok"),
            name="ask",
        )
    ])


@pytest.fixture
def a2a_approval_client(monkeypatch):
    """A2A route backed by a gated agent, a fake model that emits a tool call
    then a final answer, and a durable-style checkpointer."""
    model = _ToolFake(messages=iter([
        AIMessage(
            content="",
            tool_calls=[{"name": "_send_email", "args": {"to": "x@y.com"}, "id": "t1"}],
        ),
        AIMessage(content="all set"),
    ]))
    monkeypatch.setattr(
        _compile, "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: model,
    )
    _sent.clear()
    agent = LlmAgent(name="a", tools=[_send_email], before_tool=_ask_gate())
    config = AgentConfig(name="a2a-appr", model="m")
    ws = MagicMock()
    ws.config.host = "https://fake.cloud.databricks.com"
    app = FastAPI()
    with patch("apx_agent._defaults._make_workspace_client", return_value=ws):
        assert mount_a2a_route(app, agent, config, checkpointer=InMemorySaver())
        with TestClient(app) as client:
            yield client


def _send(client, text, context_id=None, apx_resume=None) -> dict:
    msg: dict[str, Any] = {
        "role": "user", "parts": [{"kind": "text", "text": text}], "messageId": "u1",
    }
    if context_id:
        msg["contextId"] = context_id
    params: dict[str, Any] = {"message": msg}
    if apx_resume is not None:
        params["configuration"] = {"apx_resume": apx_resume}
    return _rpc(client, "message/send", params).json()["result"]


class TestA2AApproval:
    def test_gated_tool_returns_input_required_then_resume_approve_runs_it(
        self, a2a_approval_client
    ):
        t1 = _send(a2a_approval_client, "email x@y.com")
        assert t1["status"]["state"] == "input-required"
        ask = t1["status"]["message"]["parts"][0]["text"]
        assert "_send_email" in ask and "apx_resume" in ask
        assert _sent == []  # tool NOT run while awaiting approval

        t2 = _send(
            a2a_approval_client, "", context_id=t1["contextId"], apx_resume="approve"
        )
        assert t2["status"]["state"] == "completed"
        assert _sent == ["x@y.com"]  # gated tool actually ran on resume

    def test_resume_deny_blocks_the_tool(self, a2a_approval_client):
        t1 = _send(a2a_approval_client, "email x@y.com")
        assert t1["status"]["state"] == "input-required"
        t2 = _send(
            a2a_approval_client, "", context_id=t1["contextId"], apx_resume="deny"
        )
        assert t2["status"]["state"] == "completed"
        assert _sent == []  # tool never ran

    def test_apx_resume_on_unpaused_context_is_not_misrouted(self, a2a_approval_client):
        # An apx_resume arriving on a context that is NOT awaiting a decision must
        # be treated as a normal new turn (fed as input, not Command(resume)) —
        # otherwise a stray resume would error / feed a decision as a question.
        t1 = _send(a2a_approval_client, "email x@y.com", apx_resume="approve")
        # First turn still gates → input-required, proving apx_resume was ignored
        # (not fed to a non-paused thread, which would fail the task).
        assert t1["status"]["state"] == "input-required"
        assert _sent == []


def test_a2a_resume_survives_a_restart(monkeypatch):
    # The resume routing is decided from the DURABLE checkpointer, not an
    # in-process hint — so a client can resume a paused approval over A2A after a
    # restart / on another replica. Fresh mount + fresh saver on the SAME backing
    # (the in-process state is gone); only the durable pause survives.
    _sent.clear()
    model = _ToolFake(messages=iter([
        AIMessage(
            content="",
            tool_calls=[{"name": "_send_email", "args": {"to": "x@y.com"}, "id": "t1"}],
        ),
        AIMessage(content="done"),
    ]))
    monkeypatch.setattr(
        _compile, "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: model,
    )
    agent = LlmAgent(name="a", tools=[_send_email], before_tool=_ask_gate())
    config = AgentConfig(name="a2a-appr", model="m")
    ws = MagicMock()
    ws.config.host = "https://fake.cloud.databricks.com"

    saver1 = InMemorySaver()
    with patch("apx_agent._defaults._make_workspace_client", return_value=ws):
        app1 = FastAPI()
        assert mount_a2a_route(app1, agent, config, checkpointer=saver1)
        with TestClient(app1) as c1:
            t1 = _send(c1, "email x@y.com")
        assert t1["status"]["state"] == "input-required"
        assert _sent == []  # tool not run; state written to saver1

        # Restart: fresh app + fresh saver on saver1's backing (in-process
        # awaiting-approval hint would be gone — only the durable state remains).
        saver2 = InMemorySaver()
        saver2.storage, saver2.writes, saver2.blobs = saver1.storage, saver1.writes, saver1.blobs
        app2 = FastAPI()
        assert mount_a2a_route(app2, agent, config, checkpointer=saver2)
        with TestClient(app2) as c2:
            t2 = _send(c2, "", context_id=t1["contextId"], apx_resume="approve")
    assert t2["status"]["state"] == "completed"
    assert _sent == ["x@y.com"]  # resumed the pending approval AFTER the restart


# ---------------------------------------------------------------------------
# Reality: the card's claims are now backed by a live method
# ---------------------------------------------------------------------------


class TestCardBackedByProtocol:
    def test_card_and_a2a_coexist_on_root(self, a2a_client):
        client, captured = a2a_client
        # GET / serves the chat UI; POST / is the A2A JSON-RPC endpoint.
        assert client.get("/").status_code == 200
        # The card advertises url==base and streaming/multiTurn capabilities…
        card = client.get("/.well-known/agent.json").json()
        assert card["capabilities"]["multiTurn"] is True
        # …and message/send (a core A2A method) now actually responds.
        _stub_reply(captured, "backed")
        task = _rpc(client, "message/send", _send_params("hi")).json()["result"]
        assert task["status"]["state"] == "completed"
