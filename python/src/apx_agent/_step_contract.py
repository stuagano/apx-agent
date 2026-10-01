"""Invocation-local deadlines and the fixed sequential escalation contract."""

from __future__ import annotations

import asyncio
import contextvars
import json
import re
import time
from typing import Any, Callable, Literal

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from typing_extensions import TypedDict

from ._a2a_models import data_parts
from ._cancellation import ToolCancelled, cancellable

_SCHEMA_ID = "urn:apx:sequential-escalation:v1"
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("step_deadline", default=None)


class StepTimeoutError(TimeoutError):
    """The invocation's own deadline expired."""


class StepUnavailableError(RuntimeError):
    """A structured tool result explicitly reported unavailable."""

    def __init__(self, capability: str = "agent") -> None:
        self.capability = capability if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", capability) else "agent"
        super().__init__("Step capability unavailable")


async def invoke_with_timeout(runnable: Any, state: dict[str, Any], timeout_s: float | None) -> dict[str, Any]:
    """Bound the full invocation; preserve cancellation and tool-raised timeouts."""
    inherited = _deadline.get()
    deadline = time.monotonic() + timeout_s if timeout_s is not None else inherited
    if deadline is not None and inherited is not None:
        deadline = min(deadline, inherited)
    token = _deadline.set(deadline)
    try:
        if deadline is None:
            return await runnable.ainvoke(state)
        task = asyncio.ensure_future(runnable.ainvoke(state))

        def consume_result(completed: asyncio.Future[Any]) -> None:
            if not completed.cancelled():
                completed.exception()

        try:
            done, _ = await asyncio.wait({task}, timeout=max(0, deadline - time.monotonic()))
            if not done:
                raise StepTimeoutError("Step deadline exceeded")
            result = task.result()
        finally:
            if not task.done():
                task.cancel()
                task.add_done_callback(consume_result)
        if time.monotonic() >= deadline:
            raise StepTimeoutError("Step deadline exceeded")
        return result
    finally:
        _deadline.reset(token)


def has_deadline() -> bool:
    """Whether a caller has established a deadline for this invocation."""
    return _deadline.get() is not None


def run_sync_tool(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Stop supervising at the deadline while a daemon worker may finish later."""
    deadline = _deadline.get()
    if deadline is None:
        return fn(*args, **kwargs)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise StepTimeoutError("Step deadline exceeded")
    context = contextvars.copy_context()
    expired = False

    def mark_expired() -> None:
        nonlocal expired
        expired = True

    def invoke() -> Any:
        return context.run(fn, *args, **kwargs)

    try:
        result = cancellable(invoke, timeout_s=remaining, poll_interval_s=min(0.01, remaining),
                             canceller=mark_expired)()
    except ToolCancelled:
        if expired:
            raise StepTimeoutError("Step deadline exceeded") from None
        raise
    if time.monotonic() >= deadline:
        raise StepTimeoutError("Step deadline exceeded")
    return result


def check_unavailable(value: Any, capability: str = "agent") -> None:
    """Only the explicit top-level structured signal controls execution."""
    if isinstance(value, dict) and value.get("availability") == "unavailable":
        declared = value.get("capability")
        if isinstance(declared, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", declared):
            capability = declared
        raise StepUnavailableError(capability)


def unavailable_middleware() -> AgentMiddleware:
    """Inspect actual tool results before the next model turn, on either API."""
    def inspect_result(result: Any, capability: str) -> Any:
        messages = result.update.get("messages", []) if isinstance(result, Command) and isinstance(result.update, dict) else [result]
        for message in messages:
            if not isinstance(message, ToolMessage):
                continue
            blocks = [message.content] if isinstance(message.content, str) else message.content
            for block in blocks:
                content = block.get("text") if isinstance(block, dict) and block.get("type") == "text" else block
                if not isinstance(content, str):
                    continue
                try:
                    payload = json.loads(content)
                except (ValueError, TypeError):
                    continue
                check_unavailable(payload, capability)
        return result

    class _UnavailableMiddleware(AgentMiddleware):
        def wrap_tool_call(self, request: Any, handler: Any) -> Any:
            return inspect_result(handler(request), request.tool_call["name"])

        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            return inspect_result(await handler(request), request.tool_call["name"])

    return _UnavailableMiddleware()


class StepEvidence(TypedDict):
    """Completed validated step output, with its path for nested propagation."""

    step: list[str]
    output_key: str | None
    data: JsonValue


class Escalation(BaseModel):
    """Fixed local schema: schema hints never resolve to executable schemas."""

    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)

    status: Literal["escalated"] = "escalated"
    availability: Literal["unavailable"] = "unavailable"
    capability: str
    error: str
    reason: Literal["timeout", "unavailable", "schema_miss"]
    failed_step: list[str] = Field(min_length=1)
    evidence: list[StepEvidence]


def escalation_message(payload: dict[str, Any]) -> AIMessage:
    """Publish matching JSON text and object-valued A2A data."""
    data = Escalation.model_validate(payload).model_dump(mode="json")
    return AIMessage(
        content=json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False),
        additional_kwargs={"apx_data_parts": [{"kind": "data", "data": data,
                                               "metadata": {"schema_id": _SCHEMA_ID}}]},
    )


def get_escalation(message: AIMessage) -> dict[str, Any] | None:
    """Validate marked DataParts using the fixed schema, never message prose."""
    parts = data_parts(message.additional_kwargs)
    escalation = None
    for part in parts:
        metadata = part.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("schema_id") != _SCHEMA_ID:
            continue
        try:
            if escalation is not None:
                raise ValueError("Multiple escalation packets")
            escalation = Escalation.model_validate(part.get("data")).model_dump(mode="json")
        except ValueError:
            raise ValueError("Invalid sequential escalation packet") from None
    return escalation
