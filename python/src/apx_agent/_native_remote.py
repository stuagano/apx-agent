"""Explicit native child invocations; the SDK owns records, retries and identity."""

from __future__ import annotations

import asyncio
import contextvars
import re
import uuid
from typing import Any

import httpx
from langchain_core.messages import AIMessage, HumanMessage

from ._native_input import native_data_part
from ._remote import _RemoteLeafBinding

_NATIVE_INVOCATION: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("apx_native_invocation", default=None)


def validate_correlation(value: Any) -> dict[str, Any] | None:
    """Application assertions only; never identity, SDK lineage or authorization."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"parent_invocation_id", "transport_sender", "logical_predecessor"}:
        raise ValueError("Invalid native application correlation")
    try:
        uuid.UUID(value["parent_invocation_id"])
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid native parent invocation ID") from None
    for key in ("transport_sender", "logical_predecessor"):
        item = value[key]
        if item is not None and (not isinstance(item, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", item) is None):
            raise ValueError("Invalid native application correlation name")
    return value


def native_leaf_input(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    """Reuse exact input identity when a declared payload matches its artifact."""
    keyed = state.get("state") or {}
    payload = keyed.get(agent._input_key)
    preserved = None
    for message in reversed(state["messages"]):
        parts = getattr(message, "additional_kwargs", {}).get("apx_data_parts")
        if parts:
            if len(parts) != 1:
                raise ValueError("Native remote leaf requires exactly one input artifact")
            if agent._input_key not in keyed:
                payload = parts[0]["data"]
            if parts[0]["data"] == payload and parts[0].get("metadata") is not None:
                preserved = parts[0]
            break
    return native_data_part(agent._input_schema, payload, preserved)


def emit_native_step(agent: Any, part: dict[str, Any], input_part: dict[str, Any] | None,
                     child: dict[str, Any] | None, predecessor: str | None) -> None:
    """Publish validated evidence; the native host timestamps it before persistence."""
    scope = _NATIVE_INVOCATION.get()
    if scope is None or scope["emit"] is None:
        return

    def bounded_name(value: Any) -> str | None:
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else None

    domain_status = part["data"]
    for key in getattr(agent, "_output_status_path", None) or ("status",):
        domain_status = domain_status.get(key) if isinstance(domain_status, dict) else None

    event = {"type": "agent.step.completed", "step": bounded_name(agent._name),
             "location": "remote" if child is not None else "local",
             "invocation_id": bounded_name(scope["id"]), "transport_sender": bounded_name(scope["sender"]),
             "logical_predecessor": bounded_name(predecessor), "transport_status": "completed",
             "schema_validated": True, "output_schema": bounded_name(agent._output_schema.__name__),
             "output_artifact": dict(part["metadata"]),
             "domain_status": bounded_name(domain_status)}
    if input_part is not None:
        event["input_schema"] = bounded_name(agent._input_schema.__name__)
        event["input_artifact"] = dict(input_part["metadata"])
    if child is not None:
        event.update(child_invocation_id=child["id"], parent_invocation_id=child["parent_invocation_id"], readback_verified=True)
        event["child_invocation_url"] = child["child_invocation_url"]
    scope["emit"](event)


async def invoke_native_leaf(agent: Any, binding: _RemoteLeafBinding, state: dict[str, Any], headers: dict[str, str], predecessor: str | None) -> dict[str, Any]:
    scope = _NATIVE_INVOCATION.get()
    if scope is None or not headers.get("X-Forwarded-Access-Token"):
        raise ValueError("Native remote invocation requires an active request-user native invocation")
    if agent._input_schema is None or agent._output_schema is None:
        raise ValueError("Native remote leaf requires declared input and output schemas")
    part = native_leaf_input(agent, state)
    calls = scope["calls"]
    ordinal = calls.get(binding.logical_name, 0)
    calls[binding.logical_name] = ordinal + 1
    child_id = str(uuid.uuid5(uuid.UUID(scope["id"]), f"{binding.logical_name}:{ordinal}"))
    correlation = validate_correlation({"parent_invocation_id": scope["id"], "transport_sender": scope["sender"],
                                        "logical_predecessor": predecessor})
    # User text remains user text; intermediate assistant/tool history never crosses ingress.
    messages = [{"role": "user", "content": m.content} for m in state["messages"]
                if isinstance(m, HumanMessage) and not m.additional_kwargs.get("apx_data_parts")]
    body = {"id": child_id, "session_id": child_id,
            "input": {"messages": messages, "apx_data_parts": [part], "correlation": correlation}}
    path = f"{binding.card_url.rstrip('/')}/api/invocations"
    child_url = f"{path}/{child_id}"

    async def request() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=binding.timeout_s, follow_redirects=False) as client:
            if scope["emit"] is not None:
                scope["emit"]({"type": "agent.child.requested", "id": child_id, "correlation": correlation})
            response = await client.post(path, json=body, headers=headers)
            response.raise_for_status()
            accepted = response.json()
            if not isinstance(accepted, dict) or accepted.get("id") != child_id:
                raise ValueError("Native child returned an inconsistent invocation ID")
            if scope["emit"] is not None:
                scope["emit"]({"type": "agent.child.accepted", "id": child_id, "correlation": correlation})
            saved = await client.get(f"{path}/{child_id}", headers=headers)
            saved.raise_for_status()
            record = saved.json()
            if record != accepted or record.get("status") != "completed":
                raise ValueError("Native child record is not a completed invocation")
            events = await client.get(f"{path}/{child_id}/events", headers=headers)
            events.raise_for_status()
            output = record.get("output")
            if not isinstance(output, dict) or output.get("status") != "completed" or output.get("correlation") != correlation:
                raise ValueError("Native child output contract mismatch")
            replies = output.get("messages")
            if not isinstance(replies, list) or not replies:
                raise ValueError("Native child returned no final message")
            final = replies[-1]
            if not isinstance(final, dict) or final.get("type") != "ai" or final.get("tool_calls"):
                raise ValueError("Native child returned no final assistant answer")
            message = AIMessage.model_validate(final)
            parts = message.additional_kwargs.get("apx_data_parts")
            if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
                raise ValueError("Native child requires one typed output DataPart")
            native_data_part(agent._output_schema, parts[0].get("data"), parts[0])
            message.additional_kwargs["apx_native_invocation"] = {"id": child_id, **correlation, "child_invocation_url": child_url}
            return {"messages": [message]}

    try:
        return await asyncio.wait_for(request(), timeout=binding.timeout_s)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        from ._step_contract import StepTimeoutError

        if scope["emit"] is not None:
            scope["emit"]({"type": "agent.child.unknown", "id": child_id, "correlation": correlation,
                           "transport_status": "unknown", "child_invocation_url": child_url})
        raise StepTimeoutError("Native child invocation deadline exceeded; child may still be running") from None
