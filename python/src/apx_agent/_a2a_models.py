"""A2A v0.3.0 protocol models — JSON-RPC envelope + the task/message types.

These type the agent's A2A task-execution surface (``message/send``,
``tasks/get``, ``tasks/cancel``) served at ``POST /`` — the URL the discovery
card (``/.well-known/agent.json``) already advertises. Field names are the
A2A-spec camelCase (``messageId``, ``contextId``, ``artifactId`` …) so off-the-
shelf A2A clients interoperate without remapping. See
docs/design/a2a-tasks-surface.md.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskState(str, Enum):
    """A2A task lifecycle states. The sync-complete MVP emits ``completed`` /
    ``failed``; the rest exist for protocol-faithful (de)serialization and the
    later async working-state phase."""

    submitted = "submitted"
    working = "working"
    input_required = "input-required"
    completed = "completed"
    canceled = "canceled"
    failed = "failed"
    rejected = "rejected"
    unknown = "unknown"


class TextPart(BaseModel):
    """A2A text content part. (File/data parts are not yet modelled — MVP is
    text in, text out.)"""

    kind: Literal["text"] = "text"
    text: str


class Message(BaseModel):
    """An A2A message. ``role`` is ``user`` (inbound) or ``agent`` (the reply)."""

    model_config = ConfigDict(extra="ignore")

    role: str
    parts: list[TextPart]
    messageId: str
    taskId: str | None = None
    contextId: str | None = None
    kind: Literal["message"] = "message"

    def text(self) -> str:
        """Concatenate the text parts — the agent sees one user turn."""
        return "".join(p.text for p in self.parts)


class Artifact(BaseModel):
    """A task output artifact — the agent's reply carried as text parts."""

    artifactId: str
    parts: list[TextPart]
    name: str | None = None


class TaskStatus(BaseModel):
    """The task's current state, with an optional status message (e.g. the error
    text on ``failed``)."""

    state: TaskState
    timestamp: str | None = None
    message: Message | None = None


class Task(BaseModel):
    """An A2A task — the unit ``message/send`` returns and ``tasks/get`` fetches."""

    id: str
    contextId: str
    status: TaskStatus
    history: list[Message] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    kind: Literal["task"] = "task"


# ── method params ─────────────────────────────────────────────────────────────


class MessageSendParams(BaseModel):
    """Params of ``message/send``. ``configuration`` is client-owned and passed
    through permissively (blocking/accepted-output-modes/etc. are not acted on in
    the MVP)."""

    model_config = ConfigDict(extra="ignore")

    message: Message
    configuration: dict[str, Any] | None = None


class TaskQueryParams(BaseModel):
    """Params of ``tasks/get``."""

    id: str
    historyLength: int | None = None


class TaskIdParams(BaseModel):
    """Params of ``tasks/cancel``."""

    id: str


# ── JSON-RPC 2.0 envelope ─────────────────────────────────────────────────────


class JsonRpcRequest(BaseModel):
    """An inbound JSON-RPC 2.0 request. ``id`` may be absent for notifications;
    ``params`` shape is validated per-method by the dispatcher."""

    model_config = ConfigDict(extra="ignore")

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] | None = None


class JsonRpcErrorBody(BaseModel):
    code: int
    message: str
    data: Any | None = None


class JsonRpcError(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    error: JsonRpcErrorBody


class JsonRpcSuccess(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    result: Any
