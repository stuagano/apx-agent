"""Explicit native child invocations; the SDK owns records, retries and identity."""

from __future__ import annotations

import asyncio
import contextvars
import re
import uuid
from typing import Any

import httpx
from langchain_core.messages import AIMessage, HumanMessage

from ._a2a_models import data_parts
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


async def invoke_native_leaf(agent: Any, binding: _RemoteLeafBinding, state: dict[str, Any], headers: dict[str, str], predecessor: str | None) -> dict[str, Any]:
    scope = _NATIVE_INVOCATION.get()
    if scope is None or not headers.get("X-Forwarded-Access-Token"):
        raise ValueError("Native remote invocation requires an active request-user native invocation")
    if agent._input_schema is None or agent._output_schema is None:
        raise ValueError("Native remote leaf requires declared input and output schemas")
    payload = (state.get("state") or {}).get(agent._input_key)
    preserved = None
    for message in reversed(state["messages"]):
        parts = data_parts(getattr(message, "additional_kwargs", {}))
        if parts:
            if len(parts) != 1:
                raise ValueError("Native remote leaf requires exactly one input artifact")
            if payload is None:
                payload = parts[0]["data"]
            if parts[0]["data"] == payload and parts[0].get("metadata") is not None:
                preserved = parts[0]
            break
    part = native_data_part(agent._input_schema, payload, preserved)
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
            if len(data_parts(message.additional_kwargs)) != 1:
                raise ValueError("Native child requires one typed output DataPart")
            message.additional_kwargs["apx_native_invocation"] = {"id": child_id, **correlation}
            return {"messages": [message]}

    try:
        return await asyncio.wait_for(request(), timeout=binding.timeout_s)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        from ._step_contract import StepTimeoutError

        raise StepTimeoutError("Native child invocation deadline exceeded; child may still be running") from None
