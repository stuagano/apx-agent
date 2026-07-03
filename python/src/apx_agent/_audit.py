"""Audit log attributes — a stable span-attribute schema for every apx-agent trace.

Every framework-emitted MLflow span carries a consistent set of
``apx.*`` attributes so downstream consumers (watchdog, compliance
dashboards, ad-hoc SQL over the traces table) can query without
parsing agent-specific schemas.

The attribute keys live as constants on ``AuditAttrs`` — code that
writes attributes uses these constants, code that reads them queries
the same strings. No ad-hoc strings sprinkled through the codebase.

Where attributes get set:

  * ``predict`` / ``predict_stream`` (``_chat_agent.py``) — agent name,
    session id, user-token presence, message count, model endpoint.
  * Tool call lifecycle (``_callbacks.py``) — tool name, UC function
    name when set, input keys, output type, duration.
  * Model call lifecycle (``_callbacks.py``) — model endpoint,
    input/output token counts when the LLM returns them.
  * Watchdog decisions (``_watchdog.py``) — when a guard rejects or
    redacts, the watchdog action / policy_id / reason / domain land
    on the active span so the trace records *why* the call was gated.

The schema is intentionally narrow: audit attributes describe *what
happened* (operation, identity, scope, decision) without logging raw
inputs or outputs. Use ``hash_for_audit`` to record presence /
fingerprint without exfiltrating content.

Watchdog (and other consumers) can read these from system tables that
surface MLflow trace attributes — e.g. ``system.access.audit_logs``
or workspace-level trace tables — without needing to know anything
about specific agents.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from ._mlflow_tracing import set_span_attribute, set_trace_tags

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolOutputSummary:
    type_name: str
    size: int


# ---------------------------------------------------------------------------
# Standard attribute keys
# ---------------------------------------------------------------------------


class AuditAttrs:
    """Stable attribute-key constants for apx-agent audit logging.

    Grouped by domain. All keys live under the ``apx.`` namespace.
    Add new keys here; never use string literals in the codebase
    when an audit-relevant attribute is being set.
    """

    # Agent identity & scope
    AGENT_NAME = "apx.agent.name"
    AGENT_VERSION = "apx.agent.version"
    SESSION_ID = "apx.session.id"
    OPERATION = "apx.operation"  # predict | predict_stream | tool_call | model_call | sub_agent_call

    # User / OBO identity (no PII — presence flags + hashes only)
    USER_TOKEN_PROVIDED = "apx.user.token_provided"
    USER_HASH = "apx.user.hash"  # SHA-256(user_id) when known — for correlation without leaking ids

    # Tool calls
    TOOL_NAME = "apx.tool.name"
    TOOL_UC_FUNCTION = "apx.tool.uc_function"
    TOOL_INPUT_KEYS = "apx.tool.input_keys"  # comma-separated argument names
    TOOL_INPUT_HASH = "apx.tool.input_hash"
    TOOL_OUTPUT_TYPE = "apx.tool.output_type"
    TOOL_OUTPUT_SIZE = "apx.tool.output_size"
    TOOL_DURATION_MS = "apx.tool.duration_ms"

    # Model / LLM calls
    MODEL_ENDPOINT = "apx.model.endpoint"
    MODEL_INPUT_MESSAGES = "apx.model.input_messages"
    MODEL_INPUT_TOKENS = "apx.model.input_tokens"
    MODEL_OUTPUT_TOKENS = "apx.model.output_tokens"
    MODEL_STREAMING = "apx.model.streaming"

    # Sub-agent dispatch
    SUBAGENT_ENDPOINT = "apx.subagent.endpoint"
    SUBAGENT_NAME = "apx.subagent.name"

    # Resources declared on the agent at call time
    RESOURCE_KINDS = "apx.resources.kinds"
    RESOURCE_COUNT = "apx.resources.count"

    # Watchdog runtime decisions
    WATCHDOG_ACTION = "apx.watchdog.action"  # allow | reject | redact
    WATCHDOG_POLICY_ID = "apx.watchdog.policy_id"
    WATCHDOG_REASON = "apx.watchdog.reason"
    WATCHDOG_DOMAIN = "apx.watchdog.domain"

    # Deploy / version correlation (issue #404). Stamped from the container
    # env (APX_MODEL_VERSION / APX_GIT_SHA — injected at deploy time) so
    # `canary analyze --target apps` can attribute traces per version.
    MODEL_VERSION = "apx.model_version"
    GIT_SHA = "apx.git_sha"


# ---------------------------------------------------------------------------
# Short kwarg → standard key mapping
# ---------------------------------------------------------------------------


# Translates set_audit_attrs(tool_name="x") → span.set_attribute("apx.tool.name", "x").
# Keeps call sites readable while the on-wire schema is canonical.
_KWARG_TO_KEY: dict[str, str] = {
    "agent_name": AuditAttrs.AGENT_NAME,
    "agent_version": AuditAttrs.AGENT_VERSION,
    "session_id": AuditAttrs.SESSION_ID,
    "operation": AuditAttrs.OPERATION,
    "user_token_provided": AuditAttrs.USER_TOKEN_PROVIDED,
    "user_hash": AuditAttrs.USER_HASH,
    "tool_name": AuditAttrs.TOOL_NAME,
    "tool_uc_function": AuditAttrs.TOOL_UC_FUNCTION,
    "tool_input_keys": AuditAttrs.TOOL_INPUT_KEYS,
    "tool_input_hash": AuditAttrs.TOOL_INPUT_HASH,
    "tool_output_type": AuditAttrs.TOOL_OUTPUT_TYPE,
    "tool_output_size": AuditAttrs.TOOL_OUTPUT_SIZE,
    "tool_duration_ms": AuditAttrs.TOOL_DURATION_MS,
    "model_endpoint": AuditAttrs.MODEL_ENDPOINT,
    "model_input_messages": AuditAttrs.MODEL_INPUT_MESSAGES,
    "model_input_tokens": AuditAttrs.MODEL_INPUT_TOKENS,
    "model_output_tokens": AuditAttrs.MODEL_OUTPUT_TOKENS,
    "model_streaming": AuditAttrs.MODEL_STREAMING,
    "subagent_endpoint": AuditAttrs.SUBAGENT_ENDPOINT,
    "subagent_name": AuditAttrs.SUBAGENT_NAME,
    "resource_kinds": AuditAttrs.RESOURCE_KINDS,
    "resource_count": AuditAttrs.RESOURCE_COUNT,
    "watchdog_action": AuditAttrs.WATCHDOG_ACTION,
    "watchdog_policy_id": AuditAttrs.WATCHDOG_POLICY_ID,
    "watchdog_reason": AuditAttrs.WATCHDOG_REASON,
    "watchdog_domain": AuditAttrs.WATCHDOG_DOMAIN,
    "model_version": AuditAttrs.MODEL_VERSION,
    "git_sha": AuditAttrs.GIT_SHA,
}


# ---------------------------------------------------------------------------
# Version correlation (issue #404)
# ---------------------------------------------------------------------------


# Env vars the deploy path injects into the App container. The UC manifest
# version is registered AFTER the App is live, so APX_GIT_SHA (known before
# deploy) is the primary correlation key; APX_MODEL_VERSION is honored when a
# caller can resolve a version pre-deploy (e.g. a promote re-deploy).
ENV_MODEL_VERSION = "APX_MODEL_VERSION"
ENV_GIT_SHA = "APX_GIT_SHA"


def version_correlation_attrs() -> dict[str, str]:
    """Read APX_MODEL_VERSION / APX_GIT_SHA from the env → audit attrs.

    Returns ``{AuditAttrs.MODEL_VERSION: ..., AuditAttrs.GIT_SHA: ...}`` for
    whichever env vars are set and non-empty. Absent env → empty dict, so
    callers stamping these attrs are a zero-behavior-change no-op outside a
    deployed App.
    """
    import os

    attrs: dict[str, str] = {}
    for env_name, key in (
        (ENV_MODEL_VERSION, AuditAttrs.MODEL_VERSION),
        (ENV_GIT_SHA, AuditAttrs.GIT_SHA),
    ):
        value = os.environ.get(env_name)
        if value:
            attrs[key] = value
    return attrs


def stamp_version_correlation(span: Any) -> None:
    """Stamp version-correlation identity on the current trace (issue #404).

    Sets ``apx.model_version`` / ``apx.git_sha`` (when the corresponding env
    vars are present) both as span attributes on ``span`` (the audit schema)
    and as trace-level tags — the tags are what ``canary analyze`` reads with
    a metadata-only ``search_traces``. No env → no-op.
    """
    attrs = version_correlation_attrs()
    if not attrs:
        return
    for key, value in attrs.items():
        set_span_attribute(span, key, value)
    set_trace_tags(attrs)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def set_audit_attrs(span: Any, **fields: Any) -> None:
    """Set audit attributes on ``span`` using short kwarg names.

    Maps each kwarg to its canonical ``AuditAttrs`` key. Empty / ``None``
    values are skipped so callers can pass optional fields uniformly
    without scattering ``if x: ...`` checks.

    Unknown kwarg names raise ``ValueError`` — typos in attribute names
    are silent killers in real audit pipelines, so this fails loud.

    Args:
        span: The MLflow span to annotate. ``None`` is accepted as a
            no-op (matches the rest of ``_mlflow_tracing`` for the
            case where MLflow isn't installed or no run is active).
        **fields: One or more short-key kwargs. See ``_KWARG_TO_KEY``
            for the supported set.

    Example::

        with safe_span("tool_call") as span:
            set_audit_attrs(
                span,
                operation="tool_call",
                tool_name="classify_intent",
                tool_uc_function="main.tools.classify_intent",
                tool_input_keys="query",
            )
    """
    if span is None:
        return
    for kwarg, value in fields.items():
        if value is None or value == "":
            continue
        if kwarg not in _KWARG_TO_KEY:
            raise ValueError(
                f"set_audit_attrs: unknown kwarg {kwarg!r}. "
                f"Add the key to AuditAttrs and _KWARG_TO_KEY in _audit.py, "
                f"or use set_span_attribute() for one-off non-audit attributes."
            )
        set_span_attribute(span, _KWARG_TO_KEY[kwarg], value)


def hash_for_audit(value: Any, *, length: int = 16) -> str:
    """Hash a value for audit attributes — fingerprint without exfiltrating content.

    SHA-256 of the value's UTF-8 string representation, truncated to
    ``length`` hex chars (default 16 — 64 bits of collision space, enough
    for correlation across traces).

    Used for inputs (so a span records "what input shape was processed"
    without storing the raw input), for user identifiers (when raw user
    ids are too sensitive to log), etc.
    """
    text = str(value).encode("utf-8")
    return hashlib.sha256(text).hexdigest()[:length]


def output_summary(value: Any) -> ToolOutputSummary:
    """Return ``(type_name, size_estimate)`` for a tool output.

    Used to record ``apx.tool.output_type`` and ``apx.tool.output_size``
    without storing the raw output.
    """
    type_name = type(value).__name__
    try:
        size = len(value)  # works for str / list / dict
    except TypeError:
        size = len(str(value))
    return ToolOutputSummary(type_name=type_name, size=size)


def input_keys_summary(arguments: Any) -> str:
    """Return a comma-separated list of input argument names.

    For ``apx.tool.input_keys``. Records the *shape* of the input
    (which keys were passed) without the values themselves.
    """
    if isinstance(arguments, dict):
        return ",".join(sorted(arguments.keys()))
    if isinstance(arguments, str):
        return "<string>"
    return type(arguments).__name__
