"""A2A v0.3.0 protocol models — JSON-RPC envelope + the task/message types.

These type the agent's A2A task-execution surface (``message/send``,
``tasks/get``, ``tasks/cancel``) served at ``POST /`` — the URL the discovery
card (``/.well-known/agent.json``) already advertises. Field names are the
A2A-spec camelCase (``messageId``, ``contextId``, ``artifactId`` …) so off-the-
shelf A2A clients interoperate without remapping. See
docs/design/a2a-tasks-surface.md.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter


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
    """A2A text content part."""

    kind: Literal["text"] = "text"
    text: str


class DataPart(BaseModel):
    """A2A JSON object. Optional schema hints belong in metadata, never code."""

    model_config = ConfigDict(allow_inf_nan=False)

    kind: Literal["data"] = "data"
    data: dict[str, JsonValue]
    metadata: dict[str, JsonValue] | None = None


# Retain TextPart's existing default-kind acceptance for legacy text callers.
Part = TextPart | DataPart


def data_parts(custom: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Validate the APX extension without interpreting schema IDs or control keys."""
    if not custom or "apx_data_parts" not in custom:
        return []
    try:
        parts = TypeAdapter(list[DataPart]).validate_python(custom["apx_data_parts"], strict=True)
        return [part.model_dump(mode="json", exclude_none=True) for part in parts]
    except ValueError:
        # Do not leak the invalid business payload through an error message.
        raise ValueError("Invalid apx_data_parts: expected a list of JSON DataParts") from None


def output_data_parts(messages: list[Any]) -> dict[str, Any]:
    """Only the final assistant's data is output; never echo input or stale steps."""
    for message in reversed(messages):
        if getattr(message, "type", None) == "ai":
            parts = data_parts(message.additional_kwargs)
            return {"apx_data_parts": parts} if parts else {}
    return {}


def with_input_data_parts(messages: list[Any], custom: dict[str, Any] | None) -> list[Any]:
    """Keep request-scoped structured input and give the model a JSON rendering."""
    from langchain_core.messages import HumanMessage

    parts = data_parts(custom)
    if not parts:
        return messages
    # A separate user message keeps data from acquiring system/tool authority.
    return [*messages, HumanMessage(
        content=json.dumps([part["data"] for part in parts], ensure_ascii=False, allow_nan=False),
        additional_kwargs={"apx_data_parts": parts},
    )]


class ControlSignal(BaseModel):
    """A serialized control-flow sentinel tool_call carried on a reply.

    The typed, structured shape of a ``finish_loop`` / ``transfer_to_<target>``
    sentinel so remote loop/handoff peers route identically to in-process ones —
    never emulated via reply text. Mirrors a LangChain tool_call (``name`` /
    ``args`` / ``id``); ``_extract_remote_control`` reconstructs the AIMessage the
    router already consumes. Absent ⇒ ordinary (non-control) reply."""

    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None


class Message(BaseModel):
    """An A2A message. ``role`` is ``user`` (inbound) or ``agent`` (the reply)."""

    model_config = ConfigDict(extra="ignore")

    role: str
    parts: list[Part]
    messageId: str
    taskId: str | None = None
    contextId: str | None = None
    kind: Literal["message"] = "message"

    def text(self) -> str:
        """Concatenate the text parts — the agent sees one user turn."""
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))


class Artifact(BaseModel):
    """A task output artifact containing text and/or structured data."""

    artifactId: str
    parts: list[Part]
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
