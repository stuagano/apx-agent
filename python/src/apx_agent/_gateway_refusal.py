"""Map a Unity AI Gateway DENY onto a typed refusal and the service-policy audit.

Chat-model blocks arrive as HTTP 200. The assistant text names the policy, and
the structured detail is a top-level ``databricks_service_policy`` object.
MCP blocks arrive as a JSON-RPC error: the reason is ``error.message``, and
``error.data`` may carry the same object. This module does not call the gateway.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from ._audit import hash_for_audit, set_audit_attrs
from ._errors import GatewayRefusal
from ._mlflow_tracing import current_active_span

# Databricks shows this when a blocking policy returns no reason.
_DEFAULT_REASON = (
    "Access denied: this request is not permitted by a policy on this service."
)

_PHASES = {
    "on_call": "on_call",
    "on_result": "on_result",
    "request": "on_call",
    "response": "on_result",
    "pre_call": "on_call",
    "post_call": "on_result",
}

_Surface = Literal["model", "mcp"]


def gateway_refusal(payload: Any, *, surface: _Surface) -> GatewayRefusal | None:
    """Return a refusal when ``payload`` is a gateway DENY; otherwise None.

    Writes the existing service-policy audit attributes before returning.
    Does not raise — callers raise the refusal so a non-DENY payload stays
    on its normal path.
    """

    if surface not in ("model", "mcp"):
        raise ValueError(f"gateway refusal surface must be 'model' or 'mcp', got {surface!r}")
    policy = _find_policy(payload)
    if policy is None:
        return None
    name = _short_text(policy.get("name") or policy.get("policy_name"), limit=256)
    kind = _short_text(policy.get("kind") or policy.get("type"), limit=64)
    phase = _phase(policy.get("phase"))
    rank = _rank(policy.get("rank"))
    mode = _short_text(policy.get("mode"), limit=32) or "enforce"
    reason = _reason(policy.get("reason"))
    fingerprint = hash_for_audit(json.dumps(
        {
            "name": name,
            "kind": kind,
            "phase": phase,
            "rank": rank,
            "mode": mode,
            "action": "DENY",
        },
        sort_keys=True,
    ))
    set_audit_attrs(
        current_active_span(),
        service_policy_name=name,
        service_policy_kind=kind,
        service_policy_phase=phase,
        service_policy_rank=rank,
        service_policy_mode=mode,
        service_policy_action="DENY",
        service_policy_adapter="gateway",
        service_policy_fingerprint=fingerprint,
    )
    target = "model call" if surface == "model" else "MCP call"
    if name:
        message = f"The Unity AI Gateway refused this {target} (policy {name}): {reason}"
    else:
        message = f"The Unity AI Gateway refused this {target}: {reason}"
    return GatewayRefusal(
        message,
        policy_name=name,
        kind=kind,
        phase=phase,
        rank=rank,
        mode=mode,
        action="DENY",
        surface=surface,
    )


def _find_policy(payload: Any) -> dict[str, Any] | None:
    error = _error_body(payload)
    if error is not None:
        found = _policy_from_error(error)
        if found is not None:
            return found
    body = _as_mapping(payload)
    if body is None:
        return None
    raw = body.get("databricks_service_policy")
    policy = _policy_dict(raw)
    if policy is not None and _is_deny(policy, object_present=True):
        return policy
    return None


def _policy_from_error(error: Mapping[str, Any]) -> dict[str, Any] | None:
    message = error.get("message")
    data = error.get("data")
    nested_source = data if isinstance(data, Mapping) else None
    if nested_source is not None:
        nested = _policy_dict(nested_source.get("databricks_service_policy"))
        if nested is not None and _is_deny(nested, object_present=True):
            return _with_message_reason(nested, message)
        if _is_deny(nested_source, object_present=False):
            return _with_message_reason(dict(nested_source), message)
    direct = _policy_dict(error.get("databricks_service_policy"))
    if direct is not None and _is_deny(direct, object_present=True):
        return _with_message_reason(direct, message)
    if message == _DEFAULT_REASON:
        return {"reason": _DEFAULT_REASON, "action": "DENY"}
    return None


def _with_message_reason(policy: dict[str, Any], message: Any) -> dict[str, Any]:
    if _reason_text(policy.get("reason")) is None and isinstance(message, str) and message.strip():
        policy["reason"] = message
    return policy


def _error_body(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            return dict(error)
        return None
    try:
        error = payload.error
    except AttributeError:
        return None
    try:
        message = error.message
    except AttributeError:
        return None
    try:
        data = error.data
    except AttributeError:
        data = None
    body: dict[str, Any] = {"message": message, "data": data}
    try:
        extra = error.model_extra
    except AttributeError:
        extra = None
    if isinstance(extra, Mapping):
        body.update(dict(extra))
    return body


def _as_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    extra: dict[str, Any] = {}
    try:
        raw_extra = value.model_extra
    except AttributeError:
        raw_extra = None
    if isinstance(raw_extra, Mapping):
        extra = dict(raw_extra)
    try:
        dumped = value.model_dump()
    except AttributeError:
        dumped = None
    if isinstance(dumped, dict):
        merged = dict(dumped)
        merged.update(extra)
        return merged
    try:
        data = vars(value)
    except TypeError:
        data = None
    if isinstance(data, dict):
        merged = {key: item for key, item in data.items() if not str(key).startswith("_")}
        merged.update(extra)
        return merged or None
    return extra or None


def _policy_dict(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, Mapping):
        return dict(raw)
    return None


def _is_deny(policy: Mapping[str, Any], *, object_present: bool) -> bool:
    action = policy.get("action", policy.get("result"))
    if isinstance(action, str):
        return action.upper() == "DENY"
    return object_present and action is None


def _phase(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return _PHASES.get(value.strip().lower())


def _rank(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _short_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > limit or "\n" in text:
        return None
    return text


def _reason_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text


def _reason(value: Any) -> str:
    text = _reason_text(value)
    if text is None:
        return _DEFAULT_REASON
    if len(text) > 500:
        return text[:500]
    return text
