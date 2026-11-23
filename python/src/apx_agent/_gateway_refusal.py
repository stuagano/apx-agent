"""Map a Unity AI Gateway DENY or MCP ASK onto a typed outcome and the audit.

Chat-model blocks arrive as HTTP 200. The assistant text names the policy, and
the structured detail is a top-level ``databricks_service_policy`` object.
MCP blocks arrive as a JSON-RPC error: the reason is ``error.message``, and
``error.data`` may carry the same object.

An MCP ASK is not that object. Databricks delivers it to an external agent as
MCP URL-mode elicitation (protocol 2025-11-25): JSON-RPC ``-32042``
(``URLElicitationRequiredError``) with ``error.data.elicitations`` entries of
``mode="url"``. The gateway owns the approval. This module surfaces the hold
and any http(s) approval URL, and tells the model to retry the same call
after the user approves. It does not record an apx ``Approval`` and it does
not retry the call itself. A model service cannot pause, so an ASK on the
model surface is a block.
An ASK on an MCP result phase is not a hold: Databricks lets that result
through, and hiding it would drop a call the gateway already allowed.

Attaching an external policy is still UI-only. Rechecked 2026-11-23: the
external-policies page (updated 2026-11-18) says REST and Terraform attachment
are unavailable, ``databricks-sdk`` 0.170.0 ``ModelService`` has no policy
field, and Terraform ``databricks_ai_gateway_model_service`` manages routing
only. This module does not declare ``guardrails``, does not call a guardrail
vendor, and does not report a policy as attached. ``doctor`` still abstains
on that read-back. A caller-visible verdict is not evidence the attachment
was read. The author steps are in
``docs/safety/unity-gateway-external-guardrails.md``.

This module does not call the gateway and does not build a vendor request.
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
_DEFAULT_ASK_REASON = "This call is paused until a person approves it."

# MCP 2025-11-25 URLElicitationRequiredError. Databricks names this as the
# delivery for an external agent's MCP ASK. It is the caller's error, not
# the vendor's request body.
_URL_ELICITATION_REQUIRED = -32042
_MAX_APPROVAL_URLS = 3
_MAX_APPROVAL_URL_CHARS = 2048

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
    """Return a typed DENY or MCP ASK when ``payload`` carries one.

    Writes the existing service-policy audit attributes before returning.
    Does not raise — callers raise the result so any other payload stays on
    its normal path. Returns None for ALLOW, for an MCP ASK on the result
    phase, and for anything that is not a gateway verdict.
    """

    if surface not in ("model", "mcp"):
        raise ValueError(f"gateway refusal surface must be 'model' or 'mcp', got {surface!r}")
    policy = _find_policy(payload)
    if policy is None:
        return None
    raw_action = policy.get("action")
    if not isinstance(raw_action, str) or raw_action not in ("DENY", "ASK"):
        return None
    action = raw_action
    phase = _phase(policy.get("phase"))
    # A tool-result ASK does not block. An elicitation error is still a hold:
    # the call has not produced a result yet.
    if (
        action == "ASK"
        and surface == "mcp"
        and phase == "on_result"
        and not _is_elicitation(payload)
    ):
        return None
    name = _short_text(policy.get("name") or policy.get("policy_name"), limit=256)
    kind = _short_text(policy.get("kind") or policy.get("type"), limit=64)
    rank = _rank(policy.get("rank"))
    mode = _short_text(policy.get("mode"), limit=32) or "enforce"
    reason = _reason(policy.get("reason"), action=action)
    urls = _approval_urls(payload) if action == "ASK" and surface == "mcp" else []
    fingerprint = hash_for_audit(json.dumps(
        {
            "name": name,
            "kind": kind,
            "phase": phase,
            "rank": rank,
            "mode": mode,
            "action": action,
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
        service_policy_action=action,
        service_policy_adapter="gateway",
        service_policy_fingerprint=fingerprint,
    )
    return GatewayRefusal(
        _message(action=action, surface=surface, name=name, reason=reason, urls=urls),
        policy_name=name,
        kind=kind,
        phase=phase,
        rank=rank,
        mode=mode,
        action=action,
        surface=surface,
    )


def _message(
    *,
    action: str,
    surface: _Surface,
    name: str | None,
    reason: str,
    urls: list[str],
) -> str:
    named = f" (policy {name})" if name else ""
    if action == "ASK" and surface == "mcp":
        return (
            f"The Unity AI Gateway is holding this MCP call for approval{named}: {reason}. "
            f"Tell the user this action needs their approval{_url_clause(urls)}. "
            "Retry the same call only after they approve it."
        )
    if action == "ASK":
        return (
            f"The Unity AI Gateway refused this model call{named}: {reason}. "
            "A model service cannot pause for approval."
        )
    target = "model call" if surface == "model" else "MCP call"
    if name:
        return f"The Unity AI Gateway refused this {target} (policy {name}): {reason}"
    return f"The Unity AI Gateway refused this {target}: {reason}"


def _url_clause(urls: list[str]) -> str:
    if not urls:
        return ""
    if len(urls) == 1:
        return f" and to open {urls[0]}"
    return " and to open these approval URLs: " + ", ".join(urls)


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
    if policy is None:
        return None
    return _accepted_policy(policy, object_present=True)


def _policy_from_error(error: Mapping[str, Any]) -> dict[str, Any] | None:
    message = error.get("message")
    data = error.get("data")
    nested_source = data if isinstance(data, Mapping) else None
    if nested_source is not None:
        nested = _policy_dict(nested_source.get("databricks_service_policy"))
        if nested is not None:
            accepted = _accepted_policy(nested, object_present=True)
            if accepted is not None:
                return _with_message_reason(accepted, message)
        copied = dict(nested_source)
        accepted = _accepted_policy(copied, object_present=False)
        if accepted is not None:
            return _with_message_reason(accepted, message)
    direct = _policy_dict(error.get("databricks_service_policy"))
    if direct is not None:
        accepted = _accepted_policy(direct, object_present=True)
        if accepted is not None:
            return _with_message_reason(accepted, message)
    if error.get("code") == _URL_ELICITATION_REQUIRED:
        held: dict[str, Any] = {"action": "ASK"}
        reason = message if isinstance(message, str) and message.strip() else _elicitation_reason(error)
        if isinstance(reason, str) and reason.strip():
            held["reason"] = reason
        return held
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
    try:
        code = error.code
    except AttributeError:
        code = None
    if isinstance(code, int) and not isinstance(code, bool):
        body["code"] = code
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


def _accepted_policy(policy: dict[str, Any], *, object_present: bool) -> dict[str, Any] | None:
    decision = _decision(policy, object_present=object_present)
    if decision is None:
        return None
    policy["action"] = decision
    return policy


def _decision(policy: Mapping[str, Any], *, object_present: bool) -> str | None:
    action = policy.get("action", policy.get("result"))
    if isinstance(action, str):
        normalized = action.strip().upper()
        if normalized in ("DENY", "ASK"):
            return normalized
        return None
    if object_present and action is None:
        return "DENY"
    return None


def _is_elicitation(payload: Any) -> bool:
    error = _error_body(payload)
    if error is None:
        return False
    return error.get("code") == _URL_ELICITATION_REQUIRED


def _elicitation_reason(error: Mapping[str, Any]) -> str | None:
    data = error.get("data")
    if not isinstance(data, Mapping):
        return None
    raw = data.get("elicitations")
    if not isinstance(raw, list):
        return None
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        mode = item.get("mode")
        if not isinstance(mode, str) or mode.strip().lower() != "url":
            continue
        text = _reason_text(item.get("message"))
        if text is not None:
            return text
    return None


def _approval_urls(payload: Any) -> list[str]:
    error = _error_body(payload)
    if error is None:
        return []
    data = error.get("data")
    if not isinstance(data, Mapping):
        return []
    raw = data.get("elicitations")
    if not isinstance(raw, list):
        return []
    urls: list[str] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        mode = item.get("mode")
        if not isinstance(mode, str) or mode.strip().lower() != "url":
            continue
        url = _public_http_url(item.get("url"))
        if url is None or url in urls:
            continue
        urls.append(url)
        if len(urls) == _MAX_APPROVAL_URLS:
            break
    return urls


def _public_http_url(value: Any) -> str | None:
    """An approval URL safe to show the user.

    MCP URL-mode elicitation forbids credentials in the URL. Anything that
    is not a plain http(s) URL, including a userinfo segment, is omitted.
    The hold still stands without it.
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_APPROVAL_URL_CHARS:
        return None
    if any(char.isspace() for char in text):
        return None
    if text.startswith("https://"):
        rest = text[len("https://"):]
    elif text.startswith("http://"):
        rest = text[len("http://"):]
    else:
        return None
    host = rest.split("/", 1)[0]
    if not host or "@" in host:
        return None
    return text


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


def _reason(value: Any, *, action: str) -> str:
    text = _reason_text(value)
    if text is None:
        return _DEFAULT_ASK_REASON if action == "ASK" else _DEFAULT_REASON
    if len(text) > 500:
        return text[:500]
    return text
