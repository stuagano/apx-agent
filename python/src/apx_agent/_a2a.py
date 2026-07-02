"""A2A v0.3.0 task-execution surface — JSON-RPC over ``POST /``.

Backs the discovery card's claims (``/.well-known/agent.json``) with the actual
protocol: ``message/send`` runs the SAME agent the ``/invocations`` path runs and
returns a sync-complete ``Task``; ``tasks/get`` fetches it from an in-process
bounded store; ``tasks/cancel`` reports terminal tasks as non-cancelable (real
cancellation arrives with the async working-state phase). ``message/stream`` and
push notifications are deferred. See docs/design/a2a-tasks-surface.md.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ._a2a_models import (
    Artifact,
    JsonRpcErrorBody,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcSuccess,
    Message,
    MessageSendParams,
    Task,
    TaskIdParams,
    TaskQueryParams,
    TaskState,
    TaskStatus,
    TextPart,
)
from ._agents import BaseAgent
from ._models import AgentConfig
from ._mlflow_tracing import safe_span

logger = logging.getLogger(__name__)

MAX_TASKS = 100

# JSON-RPC 2.0 standard codes + A2A-specific codes.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603
_TASK_NOT_FOUND = -32001
_TASK_NOT_CANCELABLE = -32002


class TaskStore:
    """Per-process bounded ring of recent tasks, mirroring ``_trace_store``.

    Ephemeral and per-replica by design (MVP): ``tasks/get`` is a best-effort
    recent lookup, not durable storage. Thread-safe; evicts oldest over capacity.
    """

    def __init__(self, max_tasks: int = MAX_TASKS) -> None:
        self._max = max_tasks
        self._store: "OrderedDict[str, Task]" = OrderedDict()
        self._lock = threading.Lock()

    def put(self, task: Task) -> None:
        with self._lock:
            self._store[task.id] = task
            self._store.move_to_end(task.id)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._store.get(task_id)

    def reset(self) -> None:
        with self._lock:
            self._store.clear()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _reply_text(response: Any) -> str:
    """Pull the final assistant text out of a ``ChatAgentResponse``."""
    for msg in reversed(response.messages or []):
        if msg.role == "assistant" and msg.content:
            return str(msg.content)
    return ""


def _approval_ask_text(payload: Any) -> str:
    """A2A-appropriate approval prose — resume is via ``configuration.apx_resume``,
    NOT the ChatAgent's ``custom_inputs`` (which is meaningless over A2A)."""
    tool = payload.get("tool_name") if isinstance(payload, dict) else None
    reason = payload.get("reason") if isinstance(payload, dict) else None
    return (
        f"Approval required to run {tool!r}"
        + (f": {reason}" if reason else "")
        + ". Resume by sending message/send on the same contextId with "
        + "configuration={'apx_resume': 'approve' or 'deny'}."
    )


def _error_response(
    req_id: str | int | None, code: int, message: str, data: Any = None
) -> JSONResponse:
    """A JSON-RPC error — HTTP 200 with an ``error`` body, per the spec."""
    body = JsonRpcError(id=req_id, error=JsonRpcErrorBody(code=code, message=message, data=data))
    return JSONResponse(body.model_dump(mode="json"))


def _success_response(req_id: str | int | None, result: Any) -> JSONResponse:
    body = JsonRpcSuccess(id=req_id, result=result)
    return JSONResponse(body.model_dump(mode="json"))


def mount_a2a_route(
    app: FastAPI,
    agent: BaseAgent,
    config: AgentConfig,
    conversation_store: Any | None = None,
    checkpointer: Any | None = None,
) -> bool:
    """Mount the A2A JSON-RPC surface at ``POST /`` onto ``app``.

    Mounts alongside ``mount_invocations_route`` and reuses the same agent: a
    single ``chat_agent_for(...)`` built here serves every ``message/send``.
    Returns ``False`` (logged) when the MLflow ChatAgent dep is missing, so a
    missing optional dep never breaks app startup — same posture as
    ``mount_invocations_route``.

    :param checkpointer: Optional durable LangGraph checkpointer (e.g. a Lakebase
        ``PostgresSaver``). Threaded into the ChatAgent so multi-turn A2A memory
        — and a pending mid-turn approval — survive a restart. ``None`` falls
        back to an in-process ``InMemorySaver``.

    Mid-turn approval over A2A: when a gated tool suspends, ``message/send``
    returns an ``input-required`` Task whose status message states the ask. The
    client resumes on the SAME ``contextId`` by sending ``message/send`` with
    ``configuration={"apx_resume": "approve"|"deny"}`` — approve runs the tool,
    deny blocks it. A message WITHOUT ``apx_resume`` (or on a context that isn't
    awaiting approval) is treated as a normal new turn, never as a resume.
    """
    try:
        from ._chat_agent import chat_agent_for
        from mlflow.types.agent import ChatAgentMessage
    except ImportError as exc:
        logger.warning(
            "Cannot mount A2A surface: %s. Install apx-agent[eval] to enable it.",
            exc,
        )
        return False

    chat_agent = chat_agent_for(
        agent, model=config.model, conversation_store=conversation_store,
        agent_id=config.name, checkpointer=checkpointer,
    )
    store = TaskStore()
    app.state.a2a_task_store = store

    def _run_message_send(params: MessageSendParams, request: Request) -> Task:
        """Run the agent to completion and return a terminal Task."""
        from ._obo import extract_obo_headers

        message = params.message
        context_id = message.contextId or _new_id()
        task_id = _new_id()

        custom_inputs: dict[str, Any] = dict(
            extract_obo_headers(custom_inputs={}, headers=request.headers)
        )
        # contextId → session_id so multi-turn threads through the conversation
        # store exactly as the /invocations context bridge does.
        custom_inputs.setdefault("session_id", context_id)

        # Resume only when the client sent apx_resume AND this thread is actually
        # paused awaiting a decision — checked against the DURABLE checkpointer, so
        # a resume works after a restart / on another replica, not just in-process.
        # Otherwise it's a normal new turn.
        cfg = params.configuration or {}
        apx_resume = cfg.get("apx_resume")
        resume = (
            apx_resume
            if apx_resume is not None
            and chat_agent.thread_interrupt(context_id, custom_inputs) is not None
            else None
        )

        inbound = message.model_copy(update={"taskId": task_id, "contextId": context_id})
        try:
            if resume is not None:
                custom_inputs["resume"] = resume
                predict_messages: list[Any] = []  # resume feeds Command, not input
            else:
                predict_messages = [ChatAgentMessage(role="user", content=message.text())]
            response = chat_agent.predict(
                predict_messages,
                custom_inputs=custom_inputs or None,
            )

            # Mid-turn approval: a gated tool suspended → input-required Task.
            approval = (response.custom_outputs or {}).get("approval_required")
            if approval is not None:
                ask_msg = Message(
                    role="agent",
                    parts=[TextPart(text=_approval_ask_text(approval))],
                    messageId=_new_id(),
                    taskId=task_id,
                    contextId=context_id,
                )
                task = Task(
                    id=task_id,
                    contextId=context_id,
                    status=TaskStatus(
                        state=TaskState.input_required, timestamp=_now(), message=ask_msg
                    ),
                    history=[inbound, ask_msg],
                )
                store.put(task)
                return task

            reply = _reply_text(response)
            agent_msg = Message(
                role="agent",
                parts=[TextPart(text=reply)],
                messageId=_new_id(),
                taskId=task_id,
                contextId=context_id,
            )
            task = Task(
                id=task_id,
                contextId=context_id,
                status=TaskStatus(state=TaskState.completed, timestamp=_now()),
                history=[inbound, agent_msg],
                artifacts=[Artifact(artifactId=_new_id(), parts=[TextPart(text=reply)])],
            )
        except Exception as exc:  # noqa: BLE001 — surface as a failed Task, not a 500
            logger.exception("A2A message/send execution failed")
            err_msg = Message(
                role="agent",
                parts=[TextPart(text=str(exc))],
                messageId=_new_id(),
                taskId=task_id,
                contextId=context_id,
            )
            task = Task(
                id=task_id,
                contextId=context_id,
                status=TaskStatus(
                    state=TaskState.failed, timestamp=_now(), message=err_msg
                ),
                history=[inbound],
            )
        store.put(task)
        return task

    @app.post("/", include_in_schema=False)
    async def a2a_jsonrpc(request: Request) -> Any:
        try:
            raw = await request.json()
        except Exception:
            return _error_response(None, _PARSE_ERROR, "Parse error: invalid JSON")
        if not isinstance(raw, dict):
            return _error_response(None, _INVALID_REQUEST, "Invalid Request")
        try:
            rpc = JsonRpcRequest(**raw)
        except ValidationError as exc:
            return _error_response(
                raw.get("id"), _INVALID_REQUEST, "Invalid Request", data=exc.errors()
            )

        req_id = rpc.id
        method = rpc.method
        params = rpc.params or {}
        # A notification is a request without an ``id`` member; per JSON-RPC 2.0
        # the server MUST run it for side effects but MUST NOT send a response.
        is_notification = "id" not in raw

        with safe_span(
            "POST / (A2A)",
            span_type="CHAIN",
            attributes={"apx.a2a_method": method, "apx.agent_name": config.name},
        ):
            if method == "message/send":
                try:
                    send_params = MessageSendParams(**params)
                except ValidationError as exc:
                    resp = _error_response(
                        req_id, _INVALID_PARAMS, "Invalid params", data=exc.errors()
                    )
                else:
                    task = _run_message_send(send_params, request)
                    resp = _success_response(req_id, task)
            elif method == "tasks/get":
                try:
                    q = TaskQueryParams(**params)
                except ValidationError as exc:
                    resp = _error_response(
                        req_id, _INVALID_PARAMS, "Invalid params", data=exc.errors()
                    )
                else:
                    task = store.get(q.id)
                    if task is None:
                        resp = _error_response(req_id, _TASK_NOT_FOUND, "Task not found")
                    else:
                        resp = _success_response(req_id, task)
            elif method == "tasks/cancel":
                try:
                    c = TaskIdParams(**params)
                except ValidationError as exc:
                    resp = _error_response(
                        req_id, _INVALID_PARAMS, "Invalid params", data=exc.errors()
                    )
                else:
                    task = store.get(c.id)
                    if task is None:
                        resp = _error_response(req_id, _TASK_NOT_FOUND, "Task not found")
                    else:
                        # Sync-complete tasks are already terminal — nothing to cancel.
                        resp = _error_response(
                            req_id, _TASK_NOT_CANCELABLE, "Task is not cancelable"
                        )
            else:
                resp = _error_response(
                    req_id, _METHOD_NOT_FOUND, f"Method not found: {method}"
                )

        # Notifications get no body; the work above (and its side effects) still ran.
        if is_notification:
            return Response(status_code=204)
        return resp

    logger.info("Mounted A2A task surface (POST /) for agent %r", config.name)
    return True
